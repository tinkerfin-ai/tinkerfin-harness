"""供 Studio Agent 使用的附件阅读和结果交付工具"""

from __future__ import annotations

import base64
import json
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import JsonValue, TypeAdapter

from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.generation import generate_image_bytes
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.models.schemas import AgentModelConfig


def build_attachment_tools(
    *,
    service: AttachmentService,
    processor: DocumentProcessor,
    user_id: int,
    thread_id: str,
    image_model: AgentModelConfig | None,
    supports_images: bool,
    model_allowed_origins: tuple[str, ...] = (),
) -> list[BaseTool]:
    """把当前用户和会话权限固定到文件工具，不接受模型传入归属信息"""

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_attachments() -> str:
        """列出当前会话保存的附件，可在历史内容压缩后重新定位文件

        Returns:
            包含附件标识、名称、类型和大小的 JSON 列表

        Raises:
            BusinessException: 会话不存在或当前用户无权访问"""
        files = await service.list_thread(user_id=user_id, thread_id=thread_id)
        return json.dumps(
            [file.model_dump(mode="json") for file in files], ensure_ascii=False
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def read_attachment(
        attachment_id: str, start: int = 1, count: int = 20, sheet: str | None = None
    ) -> str:
        """读取附件中的 Markdown 行、PDF 页、Word 段落或 Excel 行

        Args:
            attachment_id: 当前会话附件标识
            start: 从 1 开始的页、段落或行号，Markdown 按原文行号读取
            count: 读取数量，最多 100 项，PDF 单次最多 10 页
            sheet: Excel 工作表名称，留空读取首张表

        Returns:
            包含读取内容及位置的 JSON 对象

        Raises:
            BusinessException: 附件不可用或当前用户无权访问
            ValueError: 格式、读取范围或文档内容不符合要求
            TimeoutError: 文档处理超过 30 秒"""
        file, data = await service.read(
            attachment_id, user_id=user_id, thread_id=thread_id
        )
        result = await processor.run(
            {
                "operation": "read",
                "kind": "md"
                if file.mime_type == "text/markdown"
                else file.name.rsplit(".", 1)[-1].lower(),
                "data": base64.b64encode(data).decode("ascii"),
                "start": start,
                "count": count,
                "sheet": sheet,
            }
        )
        return json.dumps(result, ensure_ascii=False)

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def view_image(attachment_id: str) -> list[dict[str, JsonValue]]:
        """重新查看会话中保存的图片

        Args:
            attachment_id: 当前会话图片附件标识

        Returns:
            供当前视觉模型读取的持久化图片引用

        Raises:
            BusinessException: 附件不可用或当前用户无权访问
            ValueError: 当前模型不支持图片或附件不是图片"""
        if not supports_images:
            raise ValueError("当前模型不支持图片，请切换支持看图的模型")
        file = await service.get(attachment_id, user_id=user_id, thread_id=thread_id)
        if not file.mime_type.startswith("image/"):
            raise ValueError("所选附件不是图片")
        return [file.content_block()]

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def create_file(
        name: str,
        kind: Literal["xlsx", "pdf", "png", "md"],
        text: str = "",
        rows: list[list[str | int | float | bool | None]] | None = None,
        values: list[float] | None = None,
    ) -> list[dict[str, JsonValue]]:
        """生成并交付 Markdown、Excel、中文 PDF 或柱状图

        Args:
            name: 下载文件名，扩展名须与 kind 一致
            kind: 生成格式，支持 md、xlsx、pdf 或 png
            text: Markdown 或 PDF 正文，最多 50000 字符；Markdown 保留原文换行
            rows: Excel 单元格，最多 1000 行、每行 50 列
            values: PNG 柱状图的 1 到 30 个非负数，顺序对应横轴编号

        Returns:
            已保存且可下载的附件引用

        Raises:
            BusinessException: 会话不可用或文件不符合附件要求
            ValueError: 文件名、生成参数或文件内容不符合要求
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
        file = await service.upload(
            user_id=user_id,
            name=name,
            chunks=byte_chunks(data),
            thread_id=thread_id,
            source="tool",
        )
        return [file.content_block()]

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def generate_image(prompt: str) -> list[dict[str, JsonValue]]:
        """使用本人默认生图服务生成并保存图片，不自动重复收费请求

        Args:
            prompt: 图片描述，长度为 1 到 4000 字符

        Returns:
            已保存且可预览的图片附件引用

        Raises:
            BusinessException: 会话不可用或文件不符合附件要求
            ValueError: 未设置默认生图服务、描述或供应商响应不合法
            httpx.HTTPError: 生图或下载发生网络错误"""
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
        file = await service.upload(
            user_id=user_id,
            name=f"生成图片.{extension}",
            chunks=byte_chunks(data),
            thread_id=thread_id,
            source="tool",
        )
        return [file.content_block()]

    for read_tool in (list_attachments, read_attachment, view_image):
        read_tool.metadata = {"read_only": True}
    return [list_attachments, read_attachment, view_image, create_file, generate_image]
