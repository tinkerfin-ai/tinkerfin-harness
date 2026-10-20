"""本机工作文件与来源会话附件之间的明确导入和交付"""

import json
from typing import Annotated

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import Field, JsonValue

from tinkerfin_contracts import FilePath
from tinkerfin_remote import ToolContextArg, WorkspaceRequest
from tinkerfin_sandbox import LocalError, LocalSession

from .cloud import CloudError, StudioClient


def file_tools(cloud: StudioClient, request: WorkspaceRequest) -> tuple[BaseTool, ...]:
    """只绑定当前云端会话，目录和附件读取均由各自授权边界核验"""

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def deliver_file(
        file_path: Annotated[
            str, FilePath("read"), Field(min_length=1, max_length=4096)
        ],
        name: Annotated[str, Field(min_length=1, max_length=255)],
        runtime: ToolContextArg[LocalSession],
    ) -> list[dict[str, JsonValue]]:
        """把本机工作文件保存为原会话的附件。

        Args:
            file_path: 待交付本机文件的绝对路径、~/ 路径或相对项目目录的路径
            name: 用户下载时使用的文件名及真实扩展名

        Returns:
            已持久保存的附件引用；仅返回引用后才可确认交付成功

        Raises:
            ToolException: 文件不存在、超限、权限已撤销或云端未确认保存
        """
        try:
            (downloaded,) = await runtime.workspace.adownload_files([file_path])
            if downloaded.error is not None or downloaded.content is None:
                raise ToolException("工作文件不存在或不可读取，尚未交付")
            attachment = await cloud.deliver(
                request, runtime.scope.invocation_id, name, downloaded.content
            )
            return [attachment.content_block()]
        except (CloudError, httpx.RequestError, LocalError, ValueError) as error:
            raise ToolException(
                "文件尚未确认交付，请检查连接、文件内容和 10 MiB 大小限制"
            ) from error

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def import_attachment(
        attachment_id: Annotated[str, Field(min_length=1, max_length=128)],
        file_path: Annotated[
            str, FilePath("write"), Field(min_length=1, max_length=4096)
        ],
        runtime: ToolContextArg[LocalSession],
    ) -> str:
        """把当前会话中的附件复制到指定本机路径。

        Args:
            attachment_id: 真实附件列表中的身份
            file_path: 本机目标绝对路径、~/ 路径或相对项目目录的路径，已有文件会被替换

        Returns:
            已保存文件的本机绝对路径和字节数

        Raises:
            ToolException: 附件不可访问、目标只读或云端连接不可用
        """
        try:
            attachment = next(
                (
                    item
                    for item in await cloud.attachments(request)
                    if item.id == attachment_id
                ),
                None,
            )
            if attachment is None:
                raise ToolException("附件不在当前云端会话中")
            content = await cloud.read(request, attachment)
            (saved,) = await runtime.workspace.aupload_files([(file_path, content)])
            if saved.error is not None:
                raise ToolException("本机文件未保存，请检查目标目录和写入权限")
            return json.dumps(
                {
                    "file_path": runtime.workspace.to_shell_path(file_path),
                    "size_bytes": len(content),
                },
                ensure_ascii=False,
            )
        except (CloudError, httpx.RequestError, LocalError, ValueError) as error:
            raise ToolException("附件尚未导入，请检查云端连接和目标目录权限") from error

    for entry in (deliver_file, import_attachment):
        entry.handle_tool_error = True
    return deliver_file, import_attachment
