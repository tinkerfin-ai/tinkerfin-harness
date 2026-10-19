"""由原生客户端在独立桌面授权和系统权限范围内执行操作"""

import base64
import json
from typing import Annotated

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import Field, JsonValue

from .native import DesktopCommand, NativeChannel


def desktop_tools(
    channel: NativeChannel | None,
    workspace_scope_id: str,
) -> tuple[BaseTool, ...]:
    """只有本机单独授予桌面能力时才由宿主提供此通道"""
    if channel is None:
        return ()

    async def request(command: DesktopCommand) -> list[dict[str, JsonValue]]:
        try:
            reply = await channel.request(workspace_scope_id, command)
            if reply.error:
                raise ToolException(reply.error)
            blocks: list[dict[str, JsonValue]] = [
                {"type": "text", "text": json.dumps(reply.result, ensure_ascii=False)}
            ]
            if reply.image:
                content = base64.b64decode(reply.image, validate=True)
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("原生截图不是 PNG")
                blocks.append(
                    {
                        "type": "image",
                        "base64": reply.image,
                        "mime_type": "image/png",
                    }
                )
            return blocks
        except (ValueError, RuntimeError, OSError, TimeoutError) as error:
            raise ToolException(
                "桌面操作未确认完成，请检查本机客户端、系统权限和当前窗口"
            ) from error

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def desktop_apps() -> list[dict[str, JsonValue]]:
        """列出本机运行中的桌面应用。

        Returns:
            应用名称和不透明标识；不授予读取窗口或执行动作的权限

        Raises:
            ToolException: 本机客户端已关闭或桌面授权不可用
        """
        return await request(DesktopCommand(action="apps"))

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def desktop_snapshot(
        application: Annotated[str, Field(min_length=1, max_length=256)],
        include_image: bool = False,
    ) -> list[dict[str, JsonValue]]:
        """读取指定应用的当前窗口及可操作元素。

        Args:
            application: desktop_apps 返回的真实应用标识
            include_image: 是否同时返回 PNG 窗口截图，需要本机屏幕录制权限

        Returns:
            窗口快照标识、元素标识和已授权的窗口截图；后续动作必须引用本次快照

        Raises:
            ToolException: 桌面授权或系统权限不可用、锁屏、应用失效或目标窗口不可读取
        """
        return await request(
            DesktopCommand(
                action="snapshot", application=application, include_image=include_image
            )
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def desktop_press(
        snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
        element_id: Annotated[int, Field(ge=0, le=199)],
    ) -> list[dict[str, JsonValue]]:
        """点击当前执行作用域最近窗口快照中的一个可操作元素。

        Args:
            snapshot_id: 最近 desktop_snapshot 返回的快照标识
            element_id: 同一快照中允许点击的元素标识

        Returns:
            原生可访问性操作是否被系统接受；需要重新读取窗口核验业务结果

        Raises:
            ToolException: 桌面授权或系统权限不可用、锁屏、快照失效、窗口变化或元素失效
        """
        return await request(
            DesktopCommand(
                action="press", snapshot_id=snapshot_id, element_id=element_id
            )
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def desktop_set_value(
        snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
        element_id: Annotated[int, Field(ge=0, le=199)],
        value: Annotated[str, Field(max_length=8192)],
    ) -> list[dict[str, JsonValue]]:
        """替换当前执行作用域窗口快照中一个可编辑元素的内容。

        Args:
            snapshot_id: 最近 desktop_snapshot 返回的快照标识
            element_id: 同一快照中可编辑且非密码输入框的元素标识
            value: 要填写的完整内容，会替换该元素现有值

        Returns:
            原生写入是否被系统接受；需要重新读取窗口核验业务结果

        Raises:
            ToolException: 桌面授权或系统权限不可用、锁屏、快照失效、窗口变化或元素失效
        """
        return await request(
            DesktopCommand(
                action="set_value",
                snapshot_id=snapshot_id,
                element_id=element_id,
                value=value,
            )
        )

    for entry in (desktop_apps, desktop_snapshot, desktop_press, desktop_set_value):
        entry.handle_tool_error = True
    return desktop_apps, desktop_snapshot, desktop_press, desktop_set_value
