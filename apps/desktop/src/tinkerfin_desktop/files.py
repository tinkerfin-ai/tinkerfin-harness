"""本机工作文件与来源会话附件之间的明确导入和交付"""

import asyncio
import hashlib
import json
from typing import Annotated

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from tinkerfin_contracts import FilePath
from tinkerfin_contracts.media import Attachment
from tinkerfin_remote import ToolContextArg, WorkspaceRequest
from tinkerfin_sandbox import LocalError, LocalSession

from .cloud import CloudError, StudioClient


class DeliveryFile(BaseModel):
    """本次交付的本机原件与用户下载名称"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    file_path: Annotated[str, FilePath("read")] = Field(
        min_length=1,
        max_length=4096,
        description="待交付本机文件路径，按当前工作区解析",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="下载文件名，保留真实扩展名，不包含目录",
    )

    @field_validator("name")
    @classmethod
    def download_name(cls, name: str) -> str:
        if name in {".", ".."} or any(
            c in {"/", "\\"} or ord(c) < 32 or ord(c) == 127 for c in name
        ):
            raise ValueError("下载文件名不能包含目录或控制字符")
        return name


def file_tools(cloud: StudioClient, request: WorkspaceRequest) -> tuple[BaseTool, ...]:
    """只绑定当前云端会话，目录和附件读取均由各自授权边界核验"""

    capacity = asyncio.Semaphore(2)

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def deliver_files(
        files: Annotated[list[DeliveryFile], Field(min_length=1, max_length=100)],
        runtime: ToolContextArg[LocalSession],
    ) -> list[dict[str, JsonValue]]:
        """将本机文件批量交付为当前会话附件

        保存当前原始字节，保留源文件；单项失败不撤销成功附件，只重试失败项。
        同一来源、名称和内容重传时复用附件。

        Args:
            files: 一至一百个文件及下载名，单文件也使用列表，原件非空且不超过 10 MiB

        Returns:
            按输入顺序的逐项交付结果及成功附件引用；只有成功项可以宣称已交付

        Raises:
            ValueError: 文件参数或工作区路径无效
        """
        authorization_lost = False
        selected = [
            (item, runtime.workspace.to_shell_path(item.file_path)) for item in files
        ]

        async def deliver(
            item: DeliveryFile,
            canonical_path: str,
        ) -> tuple[dict[str, JsonValue], Attachment | None]:
            nonlocal authorization_lost
            result: dict[str, JsonValue] = {
                "file_path": item.file_path,
                "name": item.name,
            }
            async with capacity:
                if authorization_lost:
                    return {
                        **result,
                        "status": "failed",
                        "error": "设备授权已失效，文件未发送",
                    }, None
                try:
                    (downloaded,) = await runtime.workspace.adownload_files(
                        [item.file_path]
                    )
                    if downloaded.error is not None or downloaded.content is None:
                        raise ValueError("工作文件不存在、不可读取或超过本机读取限制")
                    content = downloaded.content
                    if not content:
                        raise ValueError("附件不能为空")
                    if len(content) > 10 * 1024 * 1024:
                        raise ValueError("单个附件不能超过 10 MiB")
                    key = hashlib.sha256(
                        json.dumps(
                            [
                                canonical_path,
                                item.name,
                                hashlib.sha256(content).hexdigest(),
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    attachment = await cloud.deliver(
                        request,
                        runtime.scope.invocation_id,
                        item.name,
                        content,
                        file_key=key,
                    )
                    return {
                        **result,
                        "status": "delivered",
                        "attachment_id": attachment.id,
                    }, attachment
                except CloudError as error:
                    unauthorized = error.status in {401, 403}
                    authorization_lost = authorization_lost or unauthorized
                    message = (
                        "设备授权已失效，文件未发送"
                        if unauthorized
                        else error.detail or "服务尚未确认保存，可重试此文件"
                    )
                except httpx.RequestError:
                    message = "连接中断，交付结果尚未确认；重试此文件不会重复发布"
                except (LocalError, OSError):
                    message = "工作文件不可读取，请检查本机文件权限"
                except ValueError as error:
                    message = str(error)
                return {**result, "status": "failed", "error": message}, None

        tasks: dict[
            tuple[str, str],
            asyncio.Task[tuple[dict[str, JsonValue], Attachment | None]],
        ] = {}
        async with asyncio.TaskGroup() as group:
            for item, canonical_path in selected:
                key = (canonical_path, item.name)
                if key not in tasks:
                    tasks[key] = group.create_task(deliver(item, canonical_path))
        results: list[JsonValue] = []
        attachments: dict[str, Attachment] = {}
        for item, canonical_path in selected:
            result, attachment = tasks[(canonical_path, item.name)].result()
            results.append({**result, "file_path": item.file_path})
            if attachment is not None:
                attachments.setdefault(attachment.id, attachment)
        return [
            {
                "type": "text",
                "text": json.dumps({"results": results}, ensure_ascii=False),
            },
            *(attachment.content_block() for attachment in attachments.values()),
        ]

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

    for entry in (deliver_files, import_attachment):
        entry.handle_tool_error = True
    return deliver_files, import_attachment
