"""按会话托管浏览器工具进程，不解释网页动作或复制浏览器协议"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from tinkerfin_sandbox import LocalAccess, LocalProcess, LocalWorkspace

from .browser_install import BrowserProvider
from .configuration import WorkspacePermissions
from .lifecycle import settle


@dataclass(frozen=True, slots=True)
class BrowserInstallation:
    """随应用提供的 Node 与官方浏览器适配入口，Chrome 按需准备"""

    node: Path
    service: Path


class _SocketDirectory:
    """通信文件保存在项目内，短别名只解决系统 Unix 路径长度限制"""

    def __init__(self, project: Path, aliases: Path | None) -> None:
        self.project = project
        self.aliases = aliases
        self.real: Path | None = None
        self.holder: Path | None = None
        self.path: Path | None = None

    async def open(self) -> Path:
        def allocate() -> None:
            descriptor = os.open(
                self.project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                for name in (".tinkerfin", "browser-connections"):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = child
                name = secrets.token_hex(12)
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                self.real = self.project / ".tinkerfin" / "browser-connections" / name
                self.holder = Path(
                    tempfile.mkdtemp(prefix="tf-browser-", dir=self.aliases)
                )
                self.path = self.holder / "s"
                self.path.symlink_to(self.real, target_is_directory=True)
            finally:
                os.close(descriptor)

        try:
            await settle(asyncio.to_thread(allocate))
            if self.path is None:
                raise RuntimeError("浏览器通信目录未能创建")
            return self.path
        except BaseException as error:
            await settle(self.close(), primary_error=error)
            raise

    async def close(self) -> None:
        """独立清理别名和真实目录，保留失败路径以便重试，取消等待全部尝试结束"""

        def remove() -> None:
            failures: list[Exception] = []
            for kind, path in (("holder", self.holder), ("real", self.real)):
                if path is None:
                    continue
                try:
                    shutil.rmtree(path)
                except Exception as error:  # noqa: BLE001 - 单个目录失败不能跳过另一个目录
                    failures.append(error)
                else:
                    if kind == "holder":
                        self.holder = self.path = None
                    else:
                        self.real = None
            if len(failures) == 1:
                raise failures[0]
            if failures:
                raise ExceptionGroup("浏览器通信目录未能全部清理", failures)

        await settle(asyncio.to_thread(remove))


class BrowserSession:
    """拥有一个会话的前台守护进程及同组浏览器，关闭后保留网站资料"""

    def __init__(self) -> None:
        self.environment: dict[str, str] = {}
        self._resources = AsyncExitStack()
        self._process: LocalProcess | None = None
        self._readers: list[asyncio.Task[None]] = []
        self._socket_directory: _SocketDirectory | None = None
        self._closing: asyncio.Task[None] | None = None
        self._ready = False

    @property
    def available(self) -> bool:
        """进程仍由当前会话持有时才允许后续命令连接"""
        return (
            self._closing is None
            and self._ready
            and self._process is not None
            and self._process.returncode is None
        )

    async def start(
        self,
        *,
        installation: BrowserInstallation,
        executable: Path,
        directory: Path,
        profile: Path,
        access: LocalAccess,
        sandbox: Path,
        aliases: Path | None = None,
    ) -> Self:
        """以独立资源作用域启动守护进程，等待显式就绪信号后发布命令环境"""

        self._socket_directory = _SocketDirectory(directory, aliases)
        try:
            socket_directory = await self._socket_directory.open()
            self.environment = {
                "TINKERFIN_BROWSER_SESSION_TOKEN": secrets.token_hex(32),
                "TINKERFIN_BROWSER_SESSION_SOCKET": str(
                    socket_directory / "browser.sock"
                ),
            }
            workspace = await self._resources.enter_async_context(
                LocalWorkspace(
                    directory,
                    access=access,
                    sandbox_executable=sandbox,
                    environment={"MAC_CHROMIUM_TMPDIR": str(socket_directory)},
                    commands={"node": installation.node},
                )
            )
            session = await self._resources.enter_async_context(workspace.open())
            process = await self._resources.enter_async_context(
                session.open_process(
                    (str(installation.node), str(installation.service))
                )
            )
            self._process = process
            self._readers.append(asyncio.create_task(self._drain(process.stderr)))
            process.stdin.write(
                json.dumps(
                    {
                        "socket": self.environment["TINKERFIN_BROWSER_SESSION_SOCKET"],
                        "token": self.environment["TINKERFIN_BROWSER_SESSION_TOKEN"],
                        "profile": str(profile),
                        "workspace": str(directory),
                        "executable": str(executable),
                        "headed": True,
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            async with asyncio.timeout(30):
                ready = await process.stdout.readline()
            if ready != b'{"event":"ready"}\n':
                raise RuntimeError("浏览器工具未能启动")
            self._ready = True
            self._readers.append(asyncio.create_task(self._drain(process.stdout)))
            return self
        except BaseException as error:
            await settle(self.aclose(), primary_error=error)
            raise

    @staticmethod
    async def _drain(reader: asyncio.StreamReader) -> None:
        # 页面和依赖诊断可能含个人内容，仅持续排空有界管道，不写入日志
        while await reader.read(64 * 1024):
            pass

    async def aclose(self) -> None:
        """等待整组退出和输出读取结束，并清理自己创建的通信目录"""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await settle(self._closing)

    async def _close(self) -> None:
        failure: Exception | None = None
        try:
            if (
                self._process is not None
                and self._ready
                and self._process.returncode is None
            ):
                try:
                    async with asyncio.timeout(15):
                        _reader, writer = await asyncio.open_unix_connection(
                            self.environment["TINKERFIN_BROWSER_SESSION_SOCKET"]
                        )
                        try:
                            writer.write(
                                json.dumps(
                                    {
                                        "token": self.environment[
                                            "TINKERFIN_BROWSER_SESSION_TOKEN"
                                        ],
                                        "method": "shutdown",
                                    }
                                ).encode()
                                + b"\n"
                            )
                            await writer.drain()
                            code = await self._process.wait()
                            if code != 0:
                                raise RuntimeError("浏览器未确认正常退出")
                        finally:
                            writer.close()
                            await writer.wait_closed()
                except (OSError, TimeoutError, RuntimeError) as error:
                    failure = error
            elif (
                self._ready
                and self._process is not None
                and self._process.returncode != 0
            ):
                failure = RuntimeError("浏览器进程异常退出")
        finally:
            try:
                await self._resources.aclose()
            finally:
                for reader in self._readers:
                    reader.cancel()
                await asyncio.gather(*self._readers, return_exceptions=True)
                if self._socket_directory is not None:
                    await self._socket_directory.close()
                    self._socket_directory = None
        if failure is not None:
            raise RuntimeError("浏览器关闭未确认完成，请核验本机状态") from failure


class _BrowserSlot:
    """关闭完成前保留会话位置，阻止新进程同时占用相同网站资料"""

    def __init__(
        self,
        opening: asyncio.Task[BrowserSession],
        permissions: WorkspacePermissions,
    ) -> None:
        self.opening = opening
        self.permissions = permissions
        self.closing: asyncio.Task[None] | None = None

    @property
    def unavailable(self) -> bool:
        return self.opening.done() and (
            self.opening.cancelled()
            or self.opening.exception() is not None
            or not self.opening.result().available
        )

    async def close(self) -> None:
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        await settle(self.closing)

    async def _close(self) -> None:
        if not self.opening.done():
            self.opening.cancel()
        result = await asyncio.gather(self.opening, return_exceptions=True)
        if isinstance(result[0], BrowserSession):
            await result[0].aclose()


class BrowserManager:
    """宿主持有账号内的会话浏览器；网络传输结束不会关闭这些资源"""

    def __init__(
        self,
        installation: BrowserInstallation,
        provider: BrowserProvider,
        *,
        sandbox: Path,
        projects_root: Path,
        protected_paths: tuple[Path, ...],
        read_only_paths: tuple[Path, ...],
        aliases: Path | None = None,
    ) -> None:
        self.installation = installation
        self._provider = provider
        self._sandbox = sandbox
        self._projects_root = projects_root
        self._protected_paths = protected_paths
        self._read_only_paths = read_only_paths
        self.aliases = aliases
        self._owners: dict[tuple[Path, str], _BrowserSlot] = {}
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    async def environment(
        self,
        directory: Path,
        thread: str,
        profile: Path,
        permissions: WorkspacePermissions,
        *,
        on_prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, str]:
        """首次命令准备浏览器，随后命令借用同一会话环境"""
        if not permissions.browser:
            raise ValueError("当前执行未授权浏览器")
        key = directory, thread
        while True:
            if self._closed:
                raise RuntimeError("本机浏览器服务已关闭")
            current = self._owners.get(key)
            if current is not None:
                if current.closing is not None:
                    await current.close()
                    if self._owners.get(key) is current:
                        del self._owners[key]
                    continue
                if current.unavailable:
                    await self._retire(key, current)
                    continue
                # 浏览器进程保持启动时的网络边界，不能为另一批准范围借用或替换
                if (
                    current.permissions.network != permissions.network
                    or current.permissions.allowed_domains
                    != permissions.allowed_domains
                ):
                    raise ValueError("浏览器会话授权范围已变化，请关闭后重新执行")
            else:
                if len(self._owners) >= 8:
                    retired = False
                    for other_key, other in tuple(self._owners.items()):
                        if other.closing is not None or other.unavailable:
                            await self._retire(other_key, other)
                            retired = True
                    if retired:
                        continue
                    raise RuntimeError("浏览器会话数量已达上限，请关闭不用的会话")
                current = _BrowserSlot(
                    asyncio.create_task(
                        self._start(directory, profile, permissions, on_prepare)
                    ),
                    permissions,
                )
                self._owners[key] = current
            owner = await settle(current.opening)
            if self._closed or current.closing is not None:
                raise RuntimeError("当前浏览器会话已停止")
            return dict(owner.environment)

    async def _start(
        self,
        directory: Path,
        profile: Path,
        permissions: WorkspacePermissions,
        on_prepare: Callable[[], Awaitable[None]] | None,
    ) -> BrowserSession:
        if on_prepare is not None:
            await on_prepare()
        executable = await self._provider.resolve()
        return await BrowserSession().start(
            installation=self.installation,
            executable=executable,
            directory=directory,
            profile=profile,
            sandbox=self._sandbox,
            aliases=self.aliases,
            access=LocalAccess(
                filesystem="user",
                network=permissions.network,
                allowed_domains=permissions.allowed_domains,
                graphics=True,
                isolated_roots=(self._projects_root,),
                protected_paths=self._protected_paths,
                read_only_paths=self._read_only_paths,
                unix_socket_paths=(directory / ".tinkerfin/browser-connections",),
            ),
        )

    async def stop(self, directory: Path, thread: str) -> None:
        """停止当前会话所有浏览器动作；返回前等待启动或进程关闭完成"""
        key = directory, thread
        slot = self._owners.get(key)
        if slot is not None:
            await self._retire(key, slot)

    async def _retire(self, key: tuple[Path, str], slot: _BrowserSlot) -> None:
        await slot.close()
        if self._owners.get(key) is slot:
            del self._owners[key]

    async def aclose(self) -> None:
        """退出账号或应用时停止全部自有会话，保留用户文件和浏览器资料"""
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._close())
        await settle(self._closing)

    async def _close(self) -> None:
        results = await asyncio.gather(
            *(
                self.stop(directory, thread)
                for directory, thread in tuple(self._owners)
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("浏览器资源未能全部关闭", failures)


class BrowserBinding:
    """给一次已授权执行提供私有准备入口，页面命令仍由原生工具直接执行"""

    def __init__(
        self,
        manager: BrowserManager,
        *,
        directory: Path,
        thread: str,
        profile: Path,
        permissions: WorkspacePermissions,
    ) -> None:
        self.manager = manager
        self.directory = directory
        self.thread = thread
        self.profile = profile
        self.permissions = permissions
        self.socket: Path | None = None
        self.token = secrets.token_hex(32)
        self._aborted = False
        self._temporary: _SocketDirectory | None = None
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        self._temporary = _SocketDirectory(self.directory, self.manager.aliases)
        try:
            self.socket = (await self._temporary.open()) / "prepare.sock"
            self._server = await asyncio.start_unix_server(
                self._accept, path=self.socket, limit=16 * 1024
            )
            return self
        except BaseException as error:
            await settle(self.__aexit__(None, error, None), primary_error=error)
            raise

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        waiting: asyncio.Task[dict[str, str]] | None = None
        disconnected: asyncio.Task[bytes] | None = None
        try:
            if len(self._clients) > 16:
                raise RuntimeError("浏览器准备请求过多")
            async with asyncio.timeout(10):
                request = await reader.readline()
            proof = self.token.encode("ascii") + b"\n"
            if self._aborted or not secrets.compare_digest(
                request, b"prepare " + proof
            ):
                raise ValueError("浏览器准备请求无效")

            async def preparing() -> None:
                writer.write(b'{"status":"preparing"}\n')
                await writer.drain()

            waiting = asyncio.create_task(
                self.manager.environment(
                    self.directory,
                    self.thread,
                    self.profile,
                    self.permissions,
                    on_prepare=preparing,
                )
            )
            disconnected = asyncio.create_task(reader.read(1))
            done, _pending = await asyncio.wait(
                (waiting, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done:
                await self.abort()
                return
            environment = waiting.result()
            if self._aborted:
                raise RuntimeError("当前浏览器执行已停止")
            writer.write(
                json.dumps(
                    {
                        "environment": environment,
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
        except asyncio.CancelledError as error:
            await settle(self.abort(), primary_error=error)
            raise
        except Exception:  # noqa: BLE001 - 私有启动诊断不能经命令响应泄漏
            try:
                writer.write(b'{"error":"Browser preparation failed"}\n')
                await writer.drain()
            except (OSError, RuntimeError):
                pass
        finally:
            for task in (waiting, disconnected):
                if task is not None:
                    task.cancel()
            for task in (waiting, disconnected):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def abort(self) -> None:
        """命令异常时由工作区等待，保证浏览器动作停止后才返回终态"""
        self._aborted = True
        await self.manager.stop(self.directory, self.thread)

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._clients):
            task.cancel()
        await asyncio.gather(*self._clients, return_exceptions=True)
        if self._temporary is not None:
            await self._temporary.close()
            self._temporary = None
