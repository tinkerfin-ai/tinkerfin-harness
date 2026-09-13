"""用户 Sandbox 内的截图与已有文件交付"""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit
from uuid import uuid4

from langchain_core.tools import BaseTool, tool
from pydantic import JsonValue

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
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


def build_sandbox_attachment_tools(
    *,
    service: AttachmentService,
    user_id: int,
    thread_id: str,
) -> list[BaseTool]:
    """声明文件交付工具，执行时取得当前会话工作区，保存成功后返回附件引用"""

    async def save_file(
        sandbox: RootedOpenSandboxBackend, path: str, name: str
    ) -> list[dict[str, JsonValue]]:
        # 框架负责工作区路径校验、有界读取和取消时的资源释放
        data = await sandbox.aread_bytes(path, max_bytes=MAX_FILE_BYTES)
        file = await service.upload(
            user_id=user_id,
            name=name,
            chunks=byte_chunks(data),
            thread_id=thread_id,
            source="tool",
        )
        return [file.content_block()]

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def deliver_file(
        file_path: str, name: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> list[dict[str, JsonValue]]:
        """把用户工作区中的文件保存为会话附件

        Args:
            file_path: 用户工作区内的图片、Markdown、PDF、DOCX 或 XLSX 路径
            name: 下载文件名，扩展名须与内容一致

        Returns:
            已验证并保存的附件引用

        Raises:
            BusinessException: 会话不可用或文件不符合附件要求
            ValueError: 文件路径或参数无效
            OpenSandboxError: 工作区读取失败或文件超过大小限制
            OSError: 文件不存在、不可读或不是普通文件"""
        return await save_file(runtime.workspace, file_path, name)

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def capture_browser(
        url: str, runtime: ToolRuntime[None, RootedOpenSandboxBackend]
    ) -> list[dict[str, JsonValue]]:
        """在用户 Sandbox 打开网页并交付视口截图

        截图作为独立文件保留在工作区，便于后续读取；会话附件单独保存。
        运行取消或连接失败时不会报告交付成功，工作区仍保留已生成的产物。

        Args:
            url: 不含账户信息的 HTTP 或 HTTPS 网页地址

        Returns:
            已保存的 PNG 视口截图引用

        Raises:
            BusinessException: 会话不可用或截图不符合附件要求
            ValueError: 地址不合法、截图失败或大小超限
            OpenSandboxError: 工作区不可用或浏览器依赖缺失"""
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
        attachments = await save_file(sandbox, path, "浏览器截图.png")
        return [{"type": "text", "text": f"截图保留在工作区：{path}"}, *attachments]

    return [deliver_file, capture_browser]
