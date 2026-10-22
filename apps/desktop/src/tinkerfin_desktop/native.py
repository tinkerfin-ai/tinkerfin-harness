"""原生客户端独占管道中的设备初始化、连接状态和桌面操作请求"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from .configuration import CompanionConfiguration
from .lifecycle import settle


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeReadiness(_Message):
    """原生宿主当前可用的系统权限，只在进程内更新"""

    accessibility: bool
    screen_capture: bool


class ReadinessChanged(_Message):
    """系统权限变化，不改变用户保存的工具授权"""

    kind: Literal["native_readiness"]
    readiness: NativeReadiness


class Initialize(_Message):
    """只在进程启动时接收一次凭证和本机授权"""

    kind: Literal["initialize"]
    configuration: CompanionConfiguration
    readiness: NativeReadiness


class Stop(_Message):
    kind: Literal["stop"]


class MaintenancePrepare(_Message):
    """同一更新请求暂停新工作，等待已接收操作完成"""

    kind: Literal["maintenance_prepare"]
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class MaintenanceCancel(_Message):
    kind: Literal["maintenance_cancel"]
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class MaintenanceCommit(_Message):
    """已就绪的更新请求结束宿主；退出完成前不恢复接纳"""

    kind: Literal["maintenance_commit"]
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class DesktopCommand(_Message):
    """桌面操作只能引用原生宿主已经观察到的应用、窗口和元素"""

    action: Literal["apps", "snapshot", "press", "set_value"]
    application: str | None = Field(default=None, min_length=1, max_length=256)
    snapshot_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    element_id: int | None = Field(default=None, ge=0, le=199)
    value: str | None = Field(default=None, max_length=8192)
    include_image: bool = False


class OpenURLCommand(_Message):
    """在系统默认浏览器打开网页，独立于桌面控制与录屏权限"""

    action: Literal["open_url"] = "open_url"
    url: str = Field(
        min_length=1,
        max_length=8192,
        description="不含内嵌凭证的 HTTP 或 HTTPS 网页地址",
    )
    include_image: Literal[False] = False

    @field_validator("url")
    @classmethod
    def website(cls, value: str) -> str:
        """只交付无内嵌凭证的 HTTP(S) 网页地址"""
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("网页地址不能包含控制字符或首尾空白")
        try:
            parsed = httpx.URL(value)
            parts = urlsplit(value)
        except (httpx.InvalidURL, ValueError) as error:
            raise ValueError("网页地址无效") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parts.username is not None
            or parts.password is not None
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError("请提供不含登录凭证的 HTTP 或 HTTPS 网页地址")
        return str(parsed)


class DesktopReply(_Message):
    """不把本机系统异常或任意原生对象放入模型上下文"""

    kind: Literal["desktop_result"]
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result: dict[str, JsonValue] = Field(default_factory=dict)
    image: str | None = Field(
        default=None,
        max_length=3 * 1024 * 1024,
        description="include_image 为 true 且已授予屏幕录制权限时返回的窗口截图，PNG 的 base64 编码",
    )
    error: str | None = Field(default=None, max_length=512)


Incoming = Annotated[
    Initialize
    | Stop
    | DesktopReply
    | ReadinessChanged
    | MaintenancePrepare
    | MaintenanceCancel
    | MaintenanceCommit,
    Field(discriminator="kind"),
]
incoming = TypeAdapter(Incoming)


class NativeChannel:
    """拥有有界原生请求；取消时请求停止操作，迟到答复不恢复执行"""

    def __init__(
        self,
        send: Callable[[dict[str, JsonValue]], Awaitable[None]],
        *,
        readiness: NativeReadiness,
    ) -> None:
        self._send = send
        self._readiness = readiness
        self._pending: dict[str, asyncio.Future[DesktopReply]] = {}
        self._closed = False

    @property
    def readiness(self) -> NativeReadiness:
        """读取最近一次原生系统检查结果，不把配置开关当作系统权限"""
        return self._readiness

    def update_readiness(self, readiness: NativeReadiness) -> bool:
        """替换状态并报告是否变化，网络上报由宿主管理"""
        changed = self._readiness != readiness
        self._readiness = readiness
        return changed

    def receive(self, reply: DesktopReply) -> None:
        """只接受当前等待中的原生答复，重复或已取消答复没有副作用"""
        future = self._pending.get(reply.request_id)
        if future is not None and not future.done():
            future.set_result(reply)

    async def request(
        self, workspace_scope_id: str, command: DesktopCommand | OpenURLCommand
    ) -> DesktopReply:
        """把一项具体操作交给原生客户端执行，最多同时保存一个待决请求"""
        if self._closed:
            raise RuntimeError("本机客户端已关闭")
        if self._pending:
            raise ValueError("另一项本机操作正在等待完成，请顺序操作")
        request_id = uuid4().hex
        future: asyncio.Future[DesktopReply] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        answered = False
        cancelled: asyncio.CancelledError | None = None
        try:
            await self._send(
                {
                    "kind": "desktop_request",
                    "request_id": request_id,
                    "workspace_scope_id": workspace_scope_id,
                    "command": command.model_dump(mode="json"),
                }
            )
            async with asyncio.timeout(180):
                result = await future
                answered = True
                return result
        except asyncio.CancelledError as error:
            cancelled = error
            raise
        finally:
            if not future.done():
                future.cancel()
            try:
                if not self._closed and not answered:
                    # 撤回写入仍受管道背压约束；管道断开由进程主循环关闭全部任务
                    try:
                        await settle(
                            self._send(
                                {"kind": "desktop_cancel", "request_id": request_id}
                            )
                        )
                    except Exception as error:
                        if cancelled is not None:
                            raise cancelled from error
                        raise
            finally:
                self._pending.pop(request_id, None)

    def close(self) -> None:
        """原生管道断开后立即终止所有待决操作"""
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.cancel()


def native_tools(
    channel: NativeChannel | None, workspace_scope_id: str
) -> tuple[BaseTool, ...]:
    """已连接客户端提供网页打开能力，无需桌面或浏览器自动化开关"""
    if channel is None:
        return ()

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def open_url(
        url: Annotated[
            str,
            Field(
                min_length=1,
                max_length=8192,
                description="完整 HTTP 或 HTTPS 网页地址，可使用本机开发地址",
            ),
        ],
    ) -> dict[str, JsonValue]:
        """在使用者的系统默认浏览器中打开一个网页。

        Args:
            url: 要打开的完整网页地址，支持本机开发页面

        Returns:
            系统是否接受打开请求；接受不表示页面已经加载，也不提供页面内容

        Raises:
            ToolException: 网页地址无效、本机连接不可用或系统未能打开默认浏览器
        """
        try:
            command = OpenURLCommand(url=url)
        except ValueError as error:
            raise ToolException(
                "请提供不含登录凭证的 HTTP 或 HTTPS 网页地址"
            ) from error
        try:
            reply = await channel.request(workspace_scope_id, command)
        except (ValueError, RuntimeError, OSError, TimeoutError) as error:
            raise ToolException(
                "网页打开结果未确认，请检查本机客户端和浏览器"
            ) from error
        if reply.error:
            raise ToolException(reply.error)
        return reply.result

    open_url.handle_tool_error = True
    return (open_url,)


class StdioChannel:
    """按行传送有界 JSON，写入串行且保留背压，不把凭证写到日志"""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._writing = asyncio.Lock()

    async def receive(self) -> Incoming | None:
        """读到 EOF 表示原生宿主已离开，不再接受后台执行"""
        line = await self.reader.readline()
        if not line:
            return None
        if len(line) > 4 * 1024 * 1024 or not line.endswith(b"\n"):
            raise ValueError("本机通信消息不完整或超过容量")
        return incoming.validate_json(line)

    async def send(self, payload: dict[str, JsonValue]) -> None:
        """有界写入保持消息完整，管道失败交由宿主生命周期处理"""
        content = TypeAdapter(dict[str, JsonValue]).dump_json(payload)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("本机通信消息超过容量")
        async with self._writing:
            async with asyncio.timeout(5):
                self.writer.write(content + b"\n")
                await self.writer.drain()
