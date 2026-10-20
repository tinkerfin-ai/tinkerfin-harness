"""本机工具宿主；设备连接和已接纳操作拥有独立生命周期"""

from __future__ import annotations

import asyncio
import fcntl
import os
from asyncio import sleep
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from websockets.asyncio.client import process_exception
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tinkerfin_contracts import SkillSource
from tinkerfin_remote import ToolExecutor, ToolWorker, WorkspaceRequest
from tinkerfin_sandbox import LocalAccess, LocalWorkspace

from .assets import SkillInputs
from .browser import BrowserBinding, BrowserInstallation, BrowserManager
from .browser_install import BrowserProvider
from .cloud import CloudError, DeviceCapabilities, StudioClient
from .configuration import CompanionConfiguration
from .desktop import desktop_tools
from .files import file_tools
from .lifecycle import settle
from .mcp import mcp_tools
from .native import NativeChannel, native_tools
from .projects import ManagedProjects

ConnectionState = Literal["connecting", "online", "offline", "unauthorized", "stopped"]


class CompanionHost:
    """拥有本机工具、工作区和持久状态，网络断开仅结束当前传输

    Args:
        configuration: 原生宿主提供的当前身份及授权
        state_directory: 应用私有目录，工作进程始终不可访问
        sandbox_executable: 应用随附的固定原生沙箱入口
        report: 向原生宿主发送安全连接状态，必须有界并支持取消
        node_executable: 随应用分发的 Node；npm 和 npx 位于同一目录
        browser: 随包浏览器工具，仅供本机启用浏览器的工作区使用
        native: 借用原生客户端独占通道，用于独立桌面授权和操作
        runtime_directory: 禁止工具修改的完整应用资源目录，不因此授予读取权限
    """

    def __init__(
        self,
        configuration: CompanionConfiguration,
        *,
        state_directory: Path,
        sandbox_executable: Path,
        report: Callable[[ConnectionState], Awaitable[None]],
        node_executable: Path | None = None,
        browser: BrowserInstallation | None = None,
        native: NativeChannel | None = None,
        runtime_directory: Path | None = None,
    ) -> None:
        self.configuration = configuration
        self._directory = state_directory
        self._sandbox = sandbox_executable
        self._report = report
        self._node = node_executable
        self._browser = browser
        self._native = native
        self._runtime_directory = runtime_directory
        self._stack = AsyncExitStack()
        self._cloud: StudioClient | None = None
        self._worker: ToolWorker | None = None
        self._opened = False
        self._closed = False
        self._closing: asyncio.Task[None] | None = None
        self._browsers: BrowserManager | None = None
        self._capabilities_changed = asyncio.Event()

    async def __aenter__(self) -> Self:
        """独占本机状态后恢复执行收据；不重新执行已受理但结果缺失的命令"""
        if self._opened or self._closed:
            raise RuntimeError("本机宿主不能重复打开")
        self._opened = True
        try:

            def lock() -> tuple[Path, int]:
                self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory = self._directory.resolve(strict=True)
                if directory == Path("/"):
                    raise ValueError("应用状态不能保存在系统根目录")
                os.chmod(directory, 0o700)
                fd = os.open(
                    directory / "host.lock",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(fd)
                    raise
                return directory, fd

            # 单个启动操作使用一个工作线程；取消也等待拿到句柄后归还所有权
            allocated: list[int] = []

            def allocate() -> Path:
                directory, fd = lock()
                allocated.append(fd)
                return directory

            try:
                directory = await settle(asyncio.to_thread(allocate))
            finally:
                for fd in allocated:
                    self._stack.callback(os.close, fd)
            device_directory = directory / self.configuration.device_id
            inputs_directory = directory.with_name(directory.name + "-inputs")
            await settle(
                asyncio.to_thread(
                    inputs_directory.mkdir, parents=True, exist_ok=True, mode=0o700
                )
            )
            inputs = SkillInputs(inputs_directory)
            await settle(
                asyncio.to_thread(
                    device_directory.mkdir, parents=True, exist_ok=True, mode=0o700
                )
            )
            projects = ManagedProjects(self.configuration, device_directory)
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{device_directory / 'executions.sqlite'}",
                pool_size=4,
                max_overflow=0,
                connect_args={"timeout": 10},
            )
            self._stack.push_async_callback(engine.dispose)
            transport = httpx.AsyncHTTPTransport(
                retries=0,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
            client = await self._stack.enter_async_context(
                httpx.AsyncClient(
                    transport=transport,
                    timeout=600,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"Accept-Encoding": "identity"},
                )
            )
            cloud = StudioClient(self.configuration, client)
            self._cloud = cloud
            self._stack.push_async_callback(self._close_browsers)

            def commands_and_libraries() -> tuple[dict[str, Path], tuple[Path, ...]]:
                commands: dict[str, Path] = {}
                libraries: list[Path] = []
                if self._node is not None:
                    libraries.append(self._node.parent.parent)
                    commands = {
                        "node": self._node,
                        "npm": self._node.with_name("npm"),
                        "npx": self._node.with_name("npx"),
                    }
                return commands, tuple(libraries)

            cache = directory.parent / "cache" / "browsers"
            await settle(
                asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True, mode=0o700)
            )
            aliases = directory.parent / "run"
            await settle(
                asyncio.to_thread(
                    aliases.mkdir, parents=True, exist_ok=True, mode=0o700
                )
            )
            if self._browser is not None:
                self._browsers = BrowserManager(
                    self._browser,
                    BrowserProvider(cache),
                    sandbox=self._sandbox,
                    projects_root=self.configuration.workspace_root,
                    protected_paths=(
                        directory,
                        directory.parent / "settings.json",
                        inputs_directory,
                    ),
                    aliases=aliases,
                    read_only_paths=(
                        aliases,
                        cache,
                        *(
                            (self._runtime_directory,)
                            if self._runtime_directory
                            else ()
                        ),
                    ),
                )

            @asynccontextmanager
            async def open_workspace(
                request: WorkspaceRequest,
            ) -> AsyncIterator[ToolExecutor]:
                """核验业务授权并借出本机能力，框架拥有工具调用与准备资源的关闭"""
                async with AsyncExitStack() as preparation:
                    try:
                        selected = await cloud.configuration_for(request)
                        grant = await projects.permissions_for(
                            selected.workspace_id, request.workspace_scope_id
                        )
                        skill_directory = await preparation.enter_async_context(
                            inputs.prepare(cloud, request, selected.skills)
                        )
                        if grant.desktop and self._native is None:
                            raise ValueError("桌面已授权，但原生操作通道不可用")
                        commands, libraries = commands_and_libraries()
                        if grant.browser and self._browser is None:
                            raise ValueError("应用缺少随附的浏览器程序")
                        project = await projects.prepare(selected.workspace_id)
                        binding: BrowserBinding | None = None
                        if grant.browser and self._browsers is not None:
                            profile = await projects.browser_profile(
                                selected.workspace_id, request.identity.thread_id
                            )
                            binding = await preparation.enter_async_context(
                                BrowserBinding(
                                    self._browsers,
                                    directory=project,
                                    thread=request.identity.thread_id,
                                    profile=profile,
                                    permissions=grant,
                                )
                            )
                            commands["tinkerfin-browser"] = Path(__file__).with_name(
                                "browser_command.py"
                            )
                        workspace = await preparation.enter_async_context(
                            LocalWorkspace(
                                project,
                                sandbox_executable=self._sandbox,
                                access=LocalAccess(
                                    filesystem="user",
                                    isolated_roots=(
                                        self.configuration.workspace_root,
                                        inputs_directory,
                                    ),
                                    protected_paths=(
                                        directory,
                                        directory.parent / "settings.json",
                                    ),
                                    read_only_paths=(
                                        inputs_directory,
                                        aliases,
                                        cache,
                                        *(
                                            (
                                                project
                                                / ".tinkerfin"
                                                / "browser-connections",
                                            )
                                            if binding
                                            else ()
                                        ),
                                        *libraries,
                                        *(
                                            (self._runtime_directory,)
                                            if self._runtime_directory
                                            else ()
                                        ),
                                    ),
                                    read_directories=(
                                        *libraries,
                                        *(
                                            (skill_directory,)
                                            if skill_directory
                                            else ()
                                        ),
                                    ),
                                    network=grant.network,
                                    allowed_domains=grant.allowed_domains,
                                    unix_socket_paths=()
                                    if binding is None
                                    else (project / ".tinkerfin/browser-connections",),
                                ),
                                commands=commands,
                                environment={}
                                if binding is None
                                else {
                                    "TINKERFIN_BROWSER_SOCKET": str(binding.socket),
                                    "TINKERFIN_BROWSER_TOKEN": binding.token,
                                },
                                on_command_abort=None
                                if binding is None
                                else binding.abort,
                            )
                        )
                    except (
                        CloudError,
                        httpx.RequestError,
                        ValueError,
                        OSError,
                    ) as error:
                        raise ValueError(
                            "本机未能准备工具，请检查连接、文件权限和技能配置"
                        ) from error

                    yield ToolExecutor(
                        workspace=workspace,
                        tools=(
                            *file_tools(cloud, request),
                            *native_tools(self._native, request.workspace_scope_id),
                            *mcp_tools(grant.mcp_servers),
                            *desktop_tools(
                                self._native if grant.desktop else None,
                                request.workspace_scope_id,
                            ),
                        ),
                        skill_sources=()
                        if skill_directory is None
                        else (SkillSource(str(skill_directory) + "/"),),
                    )

            self._worker = await self._stack.enter_async_context(
                ToolWorker(
                    engine=engine,
                    namespace=self.configuration.namespace,
                    target=self.configuration.device_id,
                    open_workspace=open_workspace,
                )
            )
            return self
        except BaseException as error:
            await settle(self.aclose(), primary_error=error)
            raise

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """退出宿主时取消自己受理的执行并完整关闭持久连接"""
        await settle(self.aclose(), primary_error=_error)

    def _capabilities(self) -> DeviceCapabilities:
        readiness = self._native.readiness if self._native is not None else None
        desktop = self.configuration.permissions.desktop
        return DeviceCapabilities(
            browser=self.configuration.permissions.browser
            and self._browser is not None,
            desktop=desktop and readiness is not None and readiness.accessibility,
            desktop_capture=desktop
            and readiness is not None
            and readiness.accessibility
            and readiness.screen_capture,
            open_url=self._native is not None,
            mcp=bool(self.configuration.permissions.mcp_servers),
        )

    async def refresh_capabilities(self) -> None:
        """合并系统权限变化，由当前连接串行上报，不阻塞原生操作回执"""
        self._capabilities_changed.set()

    async def connect_once(self) -> None:
        """同步一条连接直到断开；调用者取消只结束此连接"""
        if self._closed or self._cloud is None or self._worker is None:
            raise RuntimeError("本机宿主未打开")
        await self._report("connecting")
        self._capabilities_changed.clear()
        capabilities = self._capabilities()
        async with self._cloud.connect(capabilities) as socket:
            await self._report("online")

            async def receive() -> str:
                response = await socket.recv()
                if not isinstance(response, str):
                    raise CloudError(502)
                return response

            async def publish_capabilities() -> None:
                while True:
                    await self._capabilities_changed.wait()
                    self._capabilities_changed.clear()
                    if self._cloud is not None:
                        await self._cloud.update_capabilities(self._capabilities())

            # 能力更新不占用原生消息读取，握手期间发生的变化也会在连接后补报
            tasks = (
                asyncio.create_task(
                    self._worker.serve(send=socket.send, receive=receive)
                ),
                asyncio.create_task(publish_capabilities()),
            )
            try:
                completed, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in completed:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await settle(asyncio.gather(*tasks, return_exceptions=True))

    async def run(self) -> None:
        """连接中断后重连，认证拒绝则结束执行并等待本机重新配对"""
        delay = 1
        while not self._closed:
            try:
                await self.connect_once()
            except Exception as error:
                rejected = (
                    (isinstance(error, CloudError) and error.status in {401, 403})
                    or (
                        isinstance(error, InvalidStatus)
                        and error.response.status_code in {401, 403}
                    )
                    or (
                        isinstance(error, ConnectionClosed)
                        and error.rcvd is not None
                        and error.rcvd.code == 1008
                    )
                )
                if rejected:
                    await self.aclose()
                    await self._report("unauthorized")
                    return
                # 握手前断流由 websockets 包装，沿用其公开分类保留协议错误
                if (
                    not isinstance(error, CloudError | ConnectionClosed)
                    and process_exception(error) is not None
                ):
                    raise
            await self._report("offline")
            await sleep(delay)
            delay = min(delay * 2, 30)

    async def idle(self) -> None:
        """等待已收到的已接纳操作结算，供显式停止或诊断使用"""
        if self._worker is None or self._closed:
            raise RuntimeError("本机宿主未打开")
        await self._worker.wait_idle()

    async def _close_browsers(self) -> None:
        if self._browsers is not None:
            await self._browsers.aclose()

    async def aclose(self) -> None:
        """归还全部自有资源；用户文件与执行收据保留"""
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._stack.aclose())
        await settle(self._closing)
