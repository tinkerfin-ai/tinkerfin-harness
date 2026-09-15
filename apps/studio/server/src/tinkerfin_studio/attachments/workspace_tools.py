"""用户工作区文件描述、截图、生成产物保存与附件交付"""

from __future__ import annotations

import base64
import json
import shlex
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from deepagents.backends.sandbox import MAX_BINARY_BYTES
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import JsonValue

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES, MIME_TYPES
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks

_BROWSER_SCRIPT = """import asyncio,os,sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            page=await browser.new_page(viewport={'width':1280,'height':800})
            await page.goto(sys.argv[1],wait_until='domcontentloaded',timeout=30000)
            data=await page.screenshot(type='png',timeout=10000)
            with os.fdopen(os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as stream:
                stream.write(data)
        finally:
            await browser.close()
asyncio.run(main())
"""


@dataclass(frozen=True, slots=True)
class WorkFile:
    """当前用户工作区中的文件，交付时复制该路径当时的内容

    file_path 是框架工作区路径，不是宿主机路径或附件下载地址。
    size_bytes 表示保存时的字节数，后续编辑不更新此工具结果。
    preview_file_path 是大图片保存时的缩略图，仅供看图；交付始终选择原文件。
    """

    file_path: str
    name: str
    mime_type: str
    size_bytes: int
    preview_file_path: str | None = None

    def tool_result(self) -> str:
        """返回模型可直接用于读取和交付的工作文件描述"""
        return json.dumps(
            {key: value for key, value in asdict(self).items() if value is not None},
            ensure_ascii=False,
        )


async def describe_work_file(
    workspace: RootedOpenSandboxBackend,
    processor: DocumentProcessor,
    *,
    path: str,
    name: str,
    data: bytes,
) -> WorkFile:
    """为已保存产物补充可供视觉模型读取的有界预览

    原图始终保留；缩略图仅写入工作区，生成失败不返回成功描述。
    图片处理由可取消的文档子进程完成，不占用异步事件循环。

    Args:
        workspace: 当前用户工作区
        processor: 应用拥有的有界文档处理器
        path: 已保存原文件的工作区路径
        name: 原文件显示名称，支持图片、Markdown、PDF、DOCX、XLSX 和 PPTX
        data: 原文件内容

    Returns:
        原文件描述，大图片还包含独立 JPEG 预览路径

    Raises:
        ValueError: 图片无效或预览不符合大小限制
        OSError: 预览保存失败
        OpenSandboxError: 工作区不可用或传输失败
        TimeoutError: 预览生成超时
    """
    mime = MIME_TYPES[name.rsplit(".", 1)[-1].lower()]
    preview_path = None
    if mime.startswith("image/") and len(data) > MAX_BINARY_BYTES:
        result = await processor.run(
            {
                "operation": "preview_image",
                "data": base64.b64encode(data).decode("ascii"),
                "max_bytes": MAX_BINARY_BYTES,
            }
        )
        encoded = result.get("data")
        if not isinstance(encoded, str):
            raise ValueError("图片处理未返回预览")
        preview = base64.b64decode(encoded, validate=True)
        if not preview.startswith(b"\xff\xd8\xff") or len(preview) > MAX_BINARY_BYTES:
            raise ValueError("图片预览不符合读取限制")
        preview_path = f"/preview-{uuid4().hex}.jpg"
        results = await workspace.aupload_files([(preview_path, preview)])
        if (
            len(results) != 1
            or results[0].path != preview_path
            or results[0].error is not None
        ):
            raise OSError("图片预览保存失败")
    return WorkFile(path, name, mime, len(data), preview_path)


async def save_work_file(
    workspace: RootedOpenSandboxBackend,
    processor: DocumentProcessor,
    *,
    name: str,
    data: bytes,
) -> WorkFile:
    """将有界产物保存到独立工作文件，保存失败或取消时不报告成功

    工作区由运行环境提供，框架负责路径约束、异步传输与资源释放。
    每次使用独立路径，避免同名产物相互覆盖；交付时再验证文件内容。

    Args:
        workspace: 当前用户工作区
        processor: 应用拥有的有界文档处理器
        name: 文件名称，不包含目录，扩展名必须是支持的文件格式
        data: 非空且不超过单文件限制的原始内容

    Returns:
        已保存的工作文件描述

    Raises:
        ValueError: 名称、格式或内容大小不符合要求
        OSError: 工作区未成功保存文件
        OpenSandboxError: 工作区不可用或传输失败
        TimeoutError: 图片预览生成超时
    """
    extension = name.rsplit(".", 1)[-1].lower()
    if (
        not name
        or len(name) > 255
        or any(char in name for char in ("/", "\\", "\0", "\r", "\n"))
        or extension not in MIME_TYPES
    ):
        raise ValueError("文件名须为不含目录的受支持格式名称")
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("工作文件不能为空，且单个不能超过 10 MiB")
    path = f"/artifact-{uuid4().hex}.{extension}"
    results = await workspace.aupload_files([(path, data)])
    if len(results) != 1 or results[0].path != path or results[0].error is not None:
        raise OSError("工作文件保存失败")
    return await describe_work_file(
        workspace, processor, path=path, name=name, data=data
    )


def build_sandbox_attachment_tools(
    *,
    service: AttachmentService,
    user_id: int,
    thread_id: str | None = None,
    collection_id: str | None = None,
) -> list[BaseTool]:
    """声明工作区截图与显式交付工具，固定当前会话或自动化附件归属"""

    if (thread_id is None) == (collection_id is None):
        raise ValueError("文件工具必须绑定一个会话或自动化附件集合")

    async def save_file(
        sandbox: RootedOpenSandboxBackend, path: str, name: str
    ) -> list[dict[str, JsonValue]]:
        # 框架负责工作区路径校验、有界读取和取消时的资源释放
        try:
            data = await sandbox.aread_bytes(path, max_bytes=MAX_FILE_BYTES)
        except FileNotFoundError as error:
            raise ToolException(
                "工作文件不存在，未交付附件。请使用生成工具返回的 file_path，或先查找实际文件"
            ) from error
        file = await service.upload(
            user_id=user_id,
            name=name,
            chunks=byte_chunks(data),
            thread_id=thread_id,
            collection_id=collection_id,
            source="tool",
        )
        return [file.content_block()]

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def deliver_file(
        file_path: str, name: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> list[dict[str, JsonValue]]:
        """把用户工作区中的文件保存为会话附件

        仅在决定向用户交付时调用。复制文件当前内容，原工作文件继续保留；
        后续编辑不会改变已交付附件，附件保存成功后才返回可展示的引用。

        Args:
            file_path: 用户工作区内的图片、Markdown、PDF、DOCX、XLSX 或 PPTX 路径
            name: 下载文件名，扩展名须与内容一致

        Returns:
            已保存的附件引用；源文件不存在时返回工具失败结果，不创建附件

        Raises:
            BusinessException: 会话不可用或文件不符合附件要求
            ValueError: 文件路径或参数无效
            OpenSandboxError: 工作区读取失败或文件超过大小限制
            OSError: 文件不可读或不是普通文件
        """
        return await save_file(runtime.workspace, file_path, name)

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def capture_browser(
        url: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> str:
        """在用户工作区生成网页视口截图，不自动交付

        仅视觉模型可看图，优先用 read_file 读取 preview_file_path，否则读取 file_path。
        决定交给用户时再调用 deliver_file；截图始终作为独立工作文件保留。
        运行取消或连接失败时不报告成功，工作区可能保留已生成的产物。

        Args:
            url: 不含账户信息的 HTTP 或 HTTPS 网页地址

        Returns:
            工作文件描述；大图片另含 preview_file_path，交付使用 file_path 原图

        Raises:
            ValueError: 地址不合法、截图失败或大小超限
            OpenSandboxError: 工作区不可用、读取失败或文件超过大小限制
            OSError: 截图不存在、不可读或不是普通文件
        """
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("网页地址须为不含账户信息的 HTTP 或 HTTPS URL")
        sandbox = runtime.workspace
        path = f"/browser-capture-{uuid4().hex}.png"
        command = (
            "python -c "
            + shlex.quote(_BROWSER_SCRIPT)
            + " "
            + shlex.quote(url)
            + " "
            + shlex.quote(sandbox.to_shell_path(path))
        )

        result = await sandbox.aexecute(command, timeout=45)
        if result.exit_code != 0:
            raise ValueError("网页截图失败，请检查浏览器依赖和网页是否可用")
        data = await sandbox.aread_bytes(path, max_bytes=MAX_FILE_BYTES)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("网页截图未生成有效的 PNG 文件")
        saved = await describe_work_file(
            sandbox, service.documents, path=path, name="浏览器截图.png", data=data
        )
        return saved.tool_result()

    deliver_file.handle_tool_error = True
    return [deliver_file, capture_browser]
