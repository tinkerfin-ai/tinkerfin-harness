"""用户工作区文件描述、截图、生成产物保存与附件交付"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import struct
from dataclasses import asdict, dataclass
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
from deepagents.backends.sandbox import MAX_BINARY_BYTES
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import Field, JsonValue

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.processing import (
    MAX_FILE_BYTES,
    MAX_PIXELS,
    MIME_TYPES,
)
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks

_BROWSER_SCRIPT = r"""import asyncio
import io
import json
import os
import sys
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000

async def main():
    request_path, output_path = sys.argv[1:]
    with os.fdopen(os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW), 'r') as stream:
        request = json.loads(stream.read(6 * MAX_BYTES + 65536))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            async with asyncio.timeout(40):
                context = await browser.new_context(
                    viewport={'width': request['width'], 'height': request['height']},
                    device_scale_factor=1, service_workers='block', accept_downloads=False,
                )
                async def route_resource(route):
                    if urlsplit(route.request.url).scheme in {'http', 'https', 'data', 'blob', 'about'}:
                        await route.continue_()
                    else:
                        await route.abort()
                await context.route('**/*', route_resource)
                page = await context.new_page()
                if request['url'] is not None:
                    await page.goto(request['url'], wait_until='domcontentloaded', timeout=30000)
                else:
                    await page.set_content(request['html'], wait_until='domcontentloaded', timeout=30000)
                await page.evaluate('''async () => {
                    await document.fonts.ready;
                    await Promise.all(Array.from(document.images, image => image.decode().catch(() => undefined)));
                }''')
                bounds = await page.evaluate('''() => ({
                    width: Math.max(innerWidth, document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                    height: Math.max(innerHeight, document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
                })''')
                pixels = bounds['width'] * bounds['height'] if request['full_page'] else request['width'] * request['height']
                if pixels > MAX_PIXELS:
                    raise ValueError('截图像素超过限制')
                data = await page.screenshot(type='png', full_page=request['full_page'], animations='disabled', timeout=10000)
                if not data or len(data) > MAX_BYTES:
                    raise ValueError('截图超过大小限制')
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > MAX_PIXELS:
                        raise ValueError('截图像素超过限制')
                    image.verify()
                with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                    stream.write(data)
        finally:
            await browser.close()


asyncio.run(main())
"""


@dataclass(frozen=True, slots=True)
class WorkFile:
    """当前用户工作区中的文件，交付时复制该路径当时的内容

    file_path 是框架工作区路径，不是宿主机路径或附件下载地址。
    shell_path 是同一文件在工作区命令中的相对路径，含空格时须在命令中加引号。
    size_bytes 表示保存时的字节数，后续编辑不更新此工具结果。
    preview_file_path 是大图片保存时的缩略图，仅供看图；交付始终选择原文件。
    """

    file_path: str
    shell_path: str
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
        name: 原文件显示名称，支持图片、Markdown、PDF、DOCX、XLSX、PPTX 和技能 ZIP
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
    return WorkFile(
        file_path=path,
        shell_path=workspace.to_shell_path(path),
        name=name,
        mime_type=mime,
        size_bytes=len(data),
        preview_file_path=preview_path,
    )


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


async def _discard_browser_request(
    workspace: RootedOpenSandboxBackend, path: str
) -> None:
    """删除本次截图的参数副本，完成清理后继续传播取消

    框架负责命令取消与资源归还；这里只拥有本次创建的临时文件。
    删除任务由当前调用等待，重复取消不会把后台清理遗留给下一次运行。

    Args:
        workspace: 当前调用借用的用户工作区
        path: 本次创建且不交付给用户的独立参数文件

    Raises:
        OSError: 公开删除操作未确认成功
        CancelledError: 调用已取消，清理结束后继续传播
    """

    async def remove() -> None:
        result = await workspace.adelete(path)
        if result.error is not None:
            raise OSError("未能确认截图参数文件已清理")

    cleanup = asyncio.create_task(remove(), name="discard-browser-request")
    cancelled: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
            except Exception:  # noqa: BLE001 - 删除失败由任务结果继续传播
                break
    try:
        cleanup.result()
    except BaseException as error:
        if cancelled is not None and error is not cancelled:
            raise cancelled from error
        raise
    if cancelled is not None:
        raise cancelled


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
            file_path: 用户工作区内的图片、Markdown、PDF、DOCX、XLSX、PPTX 或技能 ZIP 路径
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
        runtime: ToolRuntime[None, RootedOpenSandboxBackend],
        url: str | None = None,
        file_path: str | None = None,
        viewport_width: Annotated[int, Field(strict=True, gt=0, le=MAX_PIXELS)] = 1280,
        viewport_height: Annotated[int, Field(strict=True, gt=0, le=MAX_PIXELS)] = 800,
        full_page: Annotated[bool, Field(strict=True)] = False,
    ) -> str:
        """将网页或工作区 HTML 渲染为 PNG，不自动交付

        如有 preview_file_path，可先读取预览；也可使用工作区工具处理原图。
        决定交给用户时再调用 deliver_file；截图始终作为独立工作文件保留。
        运行取消或连接失败时不报告成功，工作区可能保留已生成的产物。

        Args:
            url: HTTP 或 HTTPS 网页地址，不含账户信息，与 file_path 恰好填写一项
            file_path: 当前工作区 HTML 文件路径，样式和图片须内嵌或使用 HTTP(S)，不读取本地子资源
            viewport_width: 视口宽度，单位 CSS 像素，与高度乘积不超过 4000 万
            viewport_height: 视口高度，单位 CSS 像素，截图使用 1 倍像素比例
            full_page: 是否截取完整页面；完整内容也受 4000 万像素和 10 MiB 限制

        Returns:
            工作文件描述；大图片另含 preview_file_path，交付使用 file_path 原图

        Raises:
            ValueError: 地址不合法、截图失败或大小超限
            OpenSandboxError: 工作区不可用、读取失败或文件超过大小限制
            OSError: 截图不存在、不可读或不是普通文件
            TimeoutError: 图片预览生成超时
        """
        if (url is None) == (file_path is None):
            raise ValueError("网页地址与 HTML 路径必须恰好填写一项")
        if viewport_width * viewport_height > MAX_PIXELS:
            raise ValueError("截图像素超过 4000 万")
        if url is not None:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("网页地址须为不含账户信息的 HTTP 或 HTTPS URL")
        sandbox = runtime.workspace
        html = None
        if file_path is not None:
            if not file_path.lower().endswith((".html", ".htm")):
                raise ValueError("请指定工作区中的 HTML 文件")
            html = (
                await sandbox.aread_bytes(file_path, max_bytes=MAX_FILE_BYTES)
            ).decode("utf-8")
            if not html.strip():
                raise ValueError("HTML 文件不能为空")
        path = f"/browser-capture-{uuid4().hex}.png"
        request_path = f"/browser-request-{uuid4().hex}.json"
        payload = json.dumps(
            {
                "url": url,
                "html": html,
                "width": viewport_width,
                "height": viewport_height,
                "full_page": full_page,
            },
            ensure_ascii=False,
        ).encode()
        primary: BaseException | None = None
        try:
            uploaded = await sandbox.aupload_files([(request_path, payload)])
            if (
                len(uploaded) != 1
                or uploaded[0].path != request_path
                or uploaded[0].error is not None
            ):
                raise OSError("截图参数保存失败")
            command = " ".join(
                shlex.quote(part)
                for part in (
                    "python",
                    "-c",
                    _BROWSER_SCRIPT,
                    sandbox.to_shell_path(request_path),
                    sandbox.to_shell_path(path),
                )
            )

            result = await sandbox.aexecute(command, timeout=45)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await _discard_browser_request(sandbox, request_path)
            except BaseException as cleanup_error:
                if primary is not None and (
                    not isinstance(primary, Exception)
                    or not isinstance(cleanup_error, asyncio.CancelledError)
                ):
                    raise primary from cleanup_error
                raise
        if result.exit_code != 0:
            raise ValueError("网页截图失败，请检查浏览器依赖和网页是否可用")
        data = await sandbox.aread_bytes(path, max_bytes=MAX_FILE_BYTES)
        if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("网页截图未生成有效的 PNG 文件")
        width, height = struct.unpack(">II", data[16:24])
        if not width or not height or width * height > MAX_PIXELS:
            raise ValueError("网页截图尺寸不合法或超过像素限制")
        saved = await describe_work_file(
            sandbox, service.documents, path=path, name="浏览器截图.png", data=data
        )
        return saved.tool_result()

    deliver_file.handle_tool_error = True
    return [deliver_file, capture_browser]
