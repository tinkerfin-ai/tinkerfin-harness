"""供 Studio Agent 使用的附件导入、文件生成和结果交付工具"""

from __future__ import annotations

import base64
import json
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import JsonValue, TypeAdapter

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.generation import generate_image_bytes
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.attachments.workspace_tools import save_work_file
from tinkerfin_studio.models.schemas import AgentModelConfig


def build_attachment_tools(
    *,
    service: AttachmentService,
    processor: DocumentProcessor,
    user_id: int,
    thread_id: str | None = None,
    collection_id: str | None = None,
    image_model: AgentModelConfig | None,
    model_allowed_origins: tuple[str, ...] = (),
) -> list[BaseTool]:
    """把当前用户和会话权限固定到文件工具，不接受模型传入归属信息"""

    if (thread_id is None) == (collection_id is None):
        raise ValueError("文件工具必须绑定一个会话或自动化附件集合")

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_attachments() -> str:
        """列出当前会话保存的附件，可在历史内容压缩后重新定位文件

        Returns:
            包含附件标识、名称、类型和大小的 JSON 列表

        Raises:
            BusinessException: 会话不存在或当前用户无权访问"""
        if collection_id is not None:
            files = await service.list_collection(
                user_id=user_id, collection_id=collection_id
            )
        else:
            assert thread_id is not None
            files = await service.list_thread(user_id=user_id, thread_id=thread_id)
        return json.dumps(
            [file.model_dump(mode="json") for file in files], ensure_ascii=False
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def import_attachment(
        attachment_id: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> str:
        """把已有附件复制到工作区，供读取、检查或编辑，不交付新附件

        file_path 供文件工具使用；命令及脚本中的文件读取参数都使用 shell_path。
        file_path 是虚拟路径，不能当作容器中的绝对路径。
        图片如有 preview_file_path，可先读取预览；文档按实际格式选择脚本处理。
        修改工作文件不会改变原附件，需要交付修改结果时调用 deliver_file。

        Args:
            attachment_id: 当前会话或自动化附件集合中的文件标识

        Returns:
            工作文件描述；大图片另含 preview_file_path，交付使用 file_path 原图

        Raises:
            BusinessException: 附件不可用或当前用户无权访问
            ValueError: 文件名或文件大小不符合要求
            OSError: 工作文件保存失败
            OpenSandboxError: 工作区不可用或传输失败
            TimeoutError: 图片预览生成超时"""
        file, data = await service.read(
            attachment_id,
            user_id=user_id,
            thread_id=thread_id,
            collection_id=collection_id,
        )
        saved = await save_work_file(
            runtime.workspace, processor, name=file.name, data=data
        )
        return saved.tool_result()

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def create_file(
        name: str,
        kind: Literal["xlsx", "pdf", "png", "md"],
        runtime: ToolRuntime[None, RootedOpenSandboxBackend],
        text: str = "",
        rows: list[list[str | int | float | bool | None]] | None = None,
        values: list[float] | None = None,
    ) -> str:
        """在工作区生成 Markdown、Excel、中文 PDF 或柱状图，不自动交付

        可以先读取或修改工作文件；决定交给用户时调用 deliver_file。

        Args:
            name: 工作文件名称，扩展名须与 kind 一致
            kind: 生成格式，支持 md、xlsx、pdf 或 png
            text: Markdown 或 PDF 正文，最多 50000 字符；Markdown 保留原文换行
            rows: Excel 单元格，最多 1000 行、每行 50 列
            values: PNG 柱状图的 1 到 30 个非负数，顺序对应横轴编号

        Returns:
            工作文件描述；大图片另含 preview_file_path，交付使用 file_path 原图

        Raises:
            ValueError: 文件名、生成参数或文件内容不符合要求
            OSError: 工作文件保存失败
            OpenSandboxError: 工作区不可用或传输失败
            TimeoutError: 文件生成超过 30 秒"""
        extension = name.rsplit(".", 1)[-1].lower()
        if extension not in ({"md", "markdown"} if kind == "md" else {kind}):
            raise ValueError("文件名扩展名必须与生成类型一致")
        payload = TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                "operation": "generate",
                "kind": kind,
                "text": text,
                "rows": rows,
                "values": values,
            }
        )
        result = await processor.run(payload)
        encoded = result.get("data")
        if not isinstance(encoded, str):
            raise TypeError("生成服务没有返回文件")
        data = base64.b64decode(encoded, validate=True)
        saved = await save_work_file(runtime.workspace, processor, name=name, data=data)
        return saved.tool_result()

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def generate_image(
        prompt: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> str:
        """使用本人默认生图服务生成工作图片，不自动交付或重复收费请求

        检查图片时可先使用 preview_file_path，交付时使用 file_path 原图。

        Args:
            prompt: 图片描述，长度为 1 到 4000 字符

        Returns:
            工作文件描述；大图片另含 preview_file_path，交付使用 file_path 原图

        Raises:
            ValueError: 未设置默认生图服务、描述或供应商响应不合法
            OSError: 工作文件保存失败
            OpenSandboxError: 工作区不可用或传输失败
            httpx.HTTPError: 生图或下载发生网络错误
            TimeoutError: 图片预览生成超时"""
        data = await generate_image_bytes(
            image_model, prompt, allowed_origins=model_allowed_origins
        )
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            extension = "webp"
        else:
            raise ValueError("生图服务须返回 PNG、JPEG 或 WebP 图片")
        saved = await save_work_file(
            runtime.workspace, processor, name=f"生成图片.{extension}", data=data
        )
        return saved.tool_result()

    return [
        list_attachments,
        import_attachment,
        create_file,
        generate_image,
    ]
