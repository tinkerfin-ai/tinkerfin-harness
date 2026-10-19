"""浏览器按需准备、会话复用与停止清理的公共行为"""

import asyncio
import json
import shlex
import shutil
import sys
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar, Literal, Self

import httpx
import pytest

from tinkerfin_companion import browser, browser_command
from tinkerfin_companion.browser_install import BrowserProvider
from tinkerfin_companion.configuration import WorkspacePermissions
from tinkerfin_sandbox import LocalAccess, LocalWorkspace

PERMISSIONS = WorkspacePermissions(network="normal", browser=True)


class BrowserOwner:
    """以显式信号模拟浏览器占用资料目录和完成关闭"""

    instances: ClassVar[list["BrowserOwner"]] = []

    def __init__(self) -> None:
        self.environment = {"AGENT_BROWSER_SESSION": f"owned-{len(self.instances)}"}
        self.available = True
        self.profile: Path | None = None
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.closed = False
        self.instances.append(self)

    async def start(
        self,
        *,
        installation: browser.BrowserInstallation,
        executable: Path,
        directory: Path,
        profile: Path,
        access: LocalAccess,
        sandbox: Path,
        aliases: Path | None = None,
    ) -> Self:
        assert all(
            item is self or item.profile != profile or item.closed
            for item in self.instances
        ), "前一个浏览器关闭前不能重新占用相同资料目录"
        self.profile = profile
        return self

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True
        self.available = False


class ControlledProvider(BrowserProvider):
    """准备和取消清理由测试信号推进，不依赖真实下载或等待时长"""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.calls = 0
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.cancelling = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_release.set()
        self.error: Exception | None = None

    async def resolve(self) -> Path:
        self.calls += 1
        self.started.set()
        try:
            await self.proceed.wait()
        except asyncio.CancelledError:
            self.cancelling.set()
            await self.cancel_release.wait()
            raise
        if self.error is not None:
            raise self.error
        return Path("/owned/browser")


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch) -> list[BrowserOwner]:
    instances: list[BrowserOwner] = []
    monkeypatch.setattr(BrowserOwner, "instances", instances)
    monkeypatch.setattr(browser, "BrowserSession", BrowserOwner)
    return instances


@pytest.fixture
def provider(tmp_path: Path) -> ControlledProvider:
    return ControlledProvider(tmp_path / "cache")


@pytest.fixture
async def manager(
    tmp_path: Path, provider: ControlledProvider, owners: list[BrowserOwner]
) -> AsyncIterator[browser.BrowserManager]:
    service = browser.BrowserManager(
        browser.BrowserInstallation(Path("/owned/agent-browser")),
        provider,
        sandbox=Path("/usr/bin/sandbox-exec"),
        projects_root=tmp_path,
        protected_paths=(),
        read_only_paths=(),
    )
    try:
        yield service
    finally:
        provider.proceed.set()
        provider.cancel_release.set()
        for owner in owners:
            owner.close_release.set()
        await service.aclose()


async def environment(
    manager: browser.BrowserManager,
    directory: Path,
    entered: asyncio.Event | None = None,
) -> dict[str, str]:
    if entered is not None:
        entered.set()
    return await manager.environment(
        directory, "owned-thread", directory / "profile", PERMISSIONS
    )


async def prepare(
    binding: browser.BrowserBinding,
    *,
    action: Literal["prepare", "describe"] = "prepare",
    token: str | None = None,
    progress: list[str] | None = None,
):
    assert binding.socket is not None
    reader, writer = await asyncio.open_unix_connection(binding.socket)
    try:
        writer.write(
            action.encode()
            + b" "
            + (binding.token if token is None else token).encode()
            + b"\n"
        )
        await writer.drain()
        result = json.loads(await reader.readline())
        if result == {"status": "preparing"}:
            if progress is not None:
                progress.append("preparing")
            result = json.loads(await reader.readline())
        assert await reader.read() == b""
        return result
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.skipif(sys.platform != "darwin", reason="普通命令执行需要 macOS")
async def test_ordinary_command_and_binding_do_not_start_browser(
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    async with browser.BrowserBinding(
        manager,
        directory=tmp_path,
        thread="owned-thread",
        profile=tmp_path / "profile",
        permissions=PERMISSIONS,
    ) as binding:
        async with LocalWorkspace(
            tmp_path,
            access=LocalAccess(filesystem="user"),
            environment={
                "TINKERFIN_BROWSER_SOCKET": str(binding.socket),
                "TINKERFIN_BROWSER_TOKEN": binding.token,
            },
            on_command_abort=binding.abort,
        ) as workspace:
            async with workspace.open() as session:
                result = await session.aexecute("printf ordinary")
        assert result.exit_code == 0
        assert result.output == "ordinary"
        assert provider.calls == 0
        invalid_progress: list[str] = []
        assert await prepare(
            binding, token="not-authorized", progress=invalid_progress
        ) == {"error": "Browser preparation failed"}
        assert invalid_progress == []
        assert provider.calls == 0
        initial_progress: list[str] = []
        reused_progress: list[str] = []
        first = await prepare(binding, progress=initial_progress)
        assert await prepare(binding, progress=reused_progress) == first
        assert initial_progress == ["preparing"]
        assert reused_progress == []
        assert first == {
            "cli": "/owned/agent-browser",
            "environment": owners[0].environment,
        }
        assert provider.calls == 1
    assert not owners[0].closed
    await manager.aclose()
    assert owners[0].closed


async def test_describe_requires_binding_token_without_preparing_browser(
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    async with browser.BrowserBinding(
        manager,
        directory=tmp_path,
        thread="owned-thread",
        profile=tmp_path / "profile",
        permissions=PERMISSIONS,
    ) as binding:
        for token in ("", "not-authorized"):
            assert await prepare(binding, action="describe", token=token) == {
                "error": "Browser preparation failed"
            }
        progress: list[str] = []
        reply = await prepare(binding, action="describe", progress=progress)
        assert reply == {
            "cli": "/owned/agent-browser",
            "environment": {
                "AGENT_BROWSER_CONFIG": str(
                    Path(browser.__file__).with_name("browser_config.json")
                )
            },
        }
        assert progress == []
    assert provider.calls == 0
    assert not owners


@pytest.mark.skipif(sys.platform != "darwin", reason="真实命令入口需要 macOS")
@pytest.mark.parametrize(
    "arguments", [("skills", "get"), ("skills", "list"), ("--help",), ("--version",)]
)
async def test_shim_reads_skill_and_help_metadata_without_starting_browser(
    arguments: tuple[str, ...],
    tmp_path: Path,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    cli = tmp_path / "owned-cli"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "print(json.dumps({'arguments':sys.argv[1:],'environment':"
        "{key:value for key,value in os.environ.items() if key.startswith('AGENT_BROWSER_')}}))\n"
    )
    cli.chmod(0o755)
    service = browser.BrowserManager(
        browser.BrowserInstallation(cli),
        provider,
        sandbox=Path("/usr/bin/sandbox-exec"),
        projects_root=tmp_path,
        protected_paths=(),
        read_only_paths=(),
    )
    try:
        async with browser.BrowserBinding(
            service,
            directory=tmp_path,
            thread="owned-thread",
            profile=tmp_path / "profile",
            permissions=PERMISSIONS,
        ) as binding:
            async with LocalWorkspace(
                tmp_path,
                access=LocalAccess(filesystem="user"),
                commands={"agent-browser": Path(browser_command.__file__)},
                environment={
                    "TINKERFIN_BROWSER_SOCKET": str(binding.socket),
                    "TINKERFIN_BROWSER_TOKEN": binding.token,
                    "AGENT_BROWSER_PROFILE": "untrusted-profile",
                },
                on_command_abort=binding.abort,
            ) as workspace:
                async with workspace.open() as session:
                    result = await session.aexecute(
                        shlex.join(("agent-browser", *arguments))
                    )
            assert result.exit_code == 0, result
            assert json.loads(result.output) == {
                "arguments": list(arguments),
                "environment": {
                    "AGENT_BROWSER_CONFIG": str(
                        Path(browser.__file__).with_name("browser_config.json")
                    )
                },
            }
        assert provider.calls == 0
        assert not owners
    finally:
        await service.aclose()


@pytest.mark.skipif(sys.platform != "darwin", reason="真实命令取消需要 macOS")
async def test_shim_preparation_cancellation_waits_for_provider_cleanup(
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    provider.proceed.clear()
    provider.cancel_release.clear()
    async with browser.BrowserBinding(
        manager,
        directory=tmp_path,
        thread="owned-thread",
        profile=tmp_path / "profile",
        permissions=PERMISSIONS,
    ) as binding:
        async with LocalWorkspace(
            tmp_path,
            access=LocalAccess(filesystem="user"),
            commands={"agent-browser": Path(browser_command.__file__)},
            environment={
                "TINKERFIN_BROWSER_SOCKET": str(binding.socket),
                "TINKERFIN_BROWSER_TOKEN": binding.token,
            },
            on_command_abort=binding.abort,
        ) as workspace:
            async with workspace.open() as session:
                command = asyncio.create_task(
                    session.aexecute("agent-browser snapshot")
                )
                try:
                    await provider.started.wait()
                    command.cancel()
                    await provider.cancelling.wait()
                    assert not command.done()
                finally:
                    provider.cancel_release.set()
                    command.cancel()
                    await asyncio.gather(command, return_exceptions=True)
                assert command.cancelled()
    assert provider.calls == 1
    assert not owners


async def test_stop_blocks_replacement_and_application_close_joins_it(
    tmp_path: Path, manager: browser.BrowserManager, owners: list[BrowserOwner]
) -> None:
    await environment(manager, tmp_path)
    owner = owners[0]
    owner.close_release.clear()
    async with asyncio.TaskGroup() as tasks:
        stop = tasks.create_task(manager.stop(tmp_path, "owned-thread"))
        await owner.close_entered.wait()
        entered = asyncio.Event()

        async def replacement_is_stopped() -> None:
            with pytest.raises(RuntimeError, match="已关闭|已停止"):
                await environment(manager, tmp_path, entered)

        replacement = tasks.create_task(replacement_is_stopped())
        await entered.wait()
        assert len(owners) == 1
        closing = tasks.create_task(manager.aclose())
        owner.close_release.set()
        await stop
        await closing
        await replacement
    assert all(owner.closed for owner in owners)


async def test_concurrent_recovery_starts_one_replacement(
    tmp_path: Path, manager: browser.BrowserManager, owners: list[BrowserOwner]
) -> None:
    await environment(manager, tmp_path)
    previous = owners[0]
    previous.available = False
    previous.close_release.clear()
    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(environment(manager, tmp_path))
        await previous.close_entered.wait()
        entered = asyncio.Event()
        second = tasks.create_task(environment(manager, tmp_path, entered))
        await entered.wait()
        previous.close_release.set()
    assert first.result() == second.result()
    assert len(owners) == 2
    await manager.aclose()
    assert all(owner.closed for owner in owners)


async def test_exited_other_conversation_releases_capacity(
    tmp_path: Path, manager: browser.BrowserManager, owners: list[BrowserOwner]
) -> None:
    for index in range(8):
        await manager.environment(
            tmp_path, f"thread-{index}", tmp_path / f"profile-{index}", PERMISSIONS
        )
    with pytest.raises(RuntimeError, match="数量已达上限"):
        await manager.environment(
            tmp_path, "new-thread", tmp_path / "new-profile", PERMISSIONS
        )
    owners[0].available = False
    await manager.environment(
        tmp_path, "new-thread", tmp_path / "new-profile", PERMISSIONS
    )
    assert owners[0].closed
    assert len(owners) == 9
    assert all(not owner.closed for owner in owners[1:])


async def test_cancelled_stop_and_application_close_wait_for_start_cleanup(
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    provider.proceed.clear()
    provider.cancel_release.clear()
    async with asyncio.TaskGroup() as tasks:
        opening = tasks.create_task(environment(manager, tmp_path))
        await provider.started.wait()
        stop = tasks.create_task(manager.stop(tmp_path, "owned-thread"))
        await provider.cancelling.wait()
        stop.cancel()
        closing_entered = asyncio.Event()

        async def close_application() -> None:
            closing_entered.set()
            await manager.aclose()

        closing = tasks.create_task(close_application())
        await closing_entered.wait()
        assert not stop.done()
        assert not closing.done()
        provider.cancel_release.set()
        await closing
    assert stop.cancelled()
    assert opening.cancelled()
    assert not owners


async def test_failed_preparation_is_private_and_next_request_can_retry(
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
) -> None:
    provider.error = httpx.ReadTimeout("private provider diagnostic")
    async with browser.BrowserBinding(
        manager,
        directory=tmp_path,
        thread="owned-thread",
        profile=tmp_path / "profile",
        permissions=PERMISSIONS,
    ) as binding:
        assert await prepare(binding) == {"error": "Browser preparation failed"}
        provider.error = None
        assert (await prepare(binding))["cli"] == "/owned/agent-browser"
        assert provider.calls == 2


@pytest.mark.parametrize(
    ("initial", "requested"),
    [
        (
            WorkspacePermissions(network="normal", browser=True),
            WorkspacePermissions(network="restricted", browser=True),
        ),
        (
            WorkspacePermissions(
                network="restricted",
                allowed_domains=("a.example", "b.example"),
                browser=True,
            ),
            WorkspacePermissions(
                network="restricted", allowed_domains=("a.example",), browser=True
            ),
        ),
    ],
)
async def test_prepare_cannot_borrow_a_broader_existing_browser(
    initial: WorkspacePermissions,
    requested: WorkspacePermissions,
    tmp_path: Path,
    manager: browser.BrowserManager,
    provider: ControlledProvider,
    owners: list[BrowserOwner],
) -> None:
    original = await manager.environment(
        tmp_path, "owned-thread", tmp_path / "profile", initial
    )
    async with browser.BrowserBinding(
        manager,
        directory=tmp_path,
        thread="owned-thread",
        profile=tmp_path / "profile",
        permissions=requested,
    ) as binding:
        progress: list[str] = []
        assert await prepare(binding, progress=progress) == {
            "error": "Browser preparation failed"
        }
        assert progress == []
    assert (
        await manager.environment(
            tmp_path, "owned-thread", tmp_path / "profile", initial
        )
        == original
    )
    assert provider.calls == 1
    assert len(owners) == 1 and not owners[0].closed


@pytest.mark.parametrize("fail_real", [False, True])
async def test_alias_removal_failure_still_attempts_real_communication_cleanup(
    fail_real: bool,
    tmp_path: Path,
    provider: ControlledProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="tf-close-", dir="/tmp") as name:
        aliases = Path(name).resolve()
        service = browser.BrowserManager(
            browser.BrowserInstallation(Path("/owned/cli")),
            provider,
            sandbox=Path("/usr/bin/sandbox-exec"),
            projects_root=tmp_path,
            protected_paths=(),
            read_only_paths=(),
            aliases=aliases,
        )
        binding = browser.BrowserBinding(
            service,
            directory=tmp_path,
            thread="owned-thread",
            profile=tmp_path / "profile",
            permissions=PERMISSIONS,
        )
        await binding.__aenter__()
        assert binding.socket is not None
        connection_directory = binding.socket.resolve().parent
        original = shutil.rmtree
        failure = PermissionError("owned alias cannot be removed")
        real_failure = PermissionError(
            "owned communication directory cannot be removed"
        )

        def remove(path: Path) -> None:
            if path.is_relative_to(aliases):
                raise failure
            if fail_real:
                raise real_failure
            original(path)

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(shutil, "rmtree", remove)
                with pytest.raises((PermissionError, ExceptionGroup)) as raised:
                    await binding.__aexit__(None, None, None)
                if fail_real:
                    assert isinstance(raised.value, ExceptionGroup)
                    assert set(raised.value.exceptions) == {failure, real_failure}
                else:
                    assert raised.value is failure
            assert connection_directory.exists() == fail_real
            assert list(aliases.iterdir())
        finally:
            await binding.__aexit__(None, None, None)
            await service.aclose()
        assert list(aliases.iterdir()) == []


async def test_cancelled_binding_close_waits_for_all_directory_removals(
    tmp_path: Path, provider: ControlledProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory(prefix="tf-close-", dir="/tmp") as name:
        aliases = Path(name).resolve()
        service = browser.BrowserManager(
            browser.BrowserInstallation(Path("/owned/cli")),
            provider,
            sandbox=Path("/usr/bin/sandbox-exec"),
            projects_root=tmp_path,
            protected_paths=(),
            read_only_paths=(),
            aliases=aliases,
        )
        binding = browser.BrowserBinding(
            service,
            directory=tmp_path,
            thread="owned-thread",
            profile=tmp_path / "profile",
            permissions=PERMISSIONS,
        )
        await binding.__aenter__()
        assert binding.socket is not None
        connection_directory = binding.socket.resolve().parent
        original = shutil.rmtree
        loop = asyncio.get_running_loop()
        removing = asyncio.Event()
        release = threading.Event()

        def remove(path: Path) -> None:
            if path.is_relative_to(aliases):
                loop.call_soon_threadsafe(removing.set)
                release.wait()
            original(path)

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(shutil, "rmtree", remove)
                closing = asyncio.create_task(binding.__aexit__(None, None, None))
                try:
                    await removing.wait()
                    closing.cancel()
                finally:
                    release.set()
                    await asyncio.gather(closing, return_exceptions=True)
                assert closing.cancelled()
            assert not connection_directory.exists()
            assert list(aliases.iterdir()) == []
        finally:
            await binding.__aexit__(None, None, None)
            await service.aclose()


@pytest.mark.skipif(sys.platform != "darwin", reason="项目通信隔离需要 macOS")
async def test_prepare_short_alias_only_connects_from_its_project(
    tmp_path: Path, provider: ControlledProvider, owners: list[BrowserOwner]
) -> None:
    project, other = tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    source = """
import json, socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    try:
        client.connect(sys.argv[1])
    except PermissionError:
        print(json.dumps({'denied': True}))
    else:
        client.sendall(('prepare ' + sys.argv[2] + '\\n').encode())
        response = client.makefile('rb')
        reply = json.loads(response.readline())
        if reply == {'status': 'preparing'}:
            reply = json.loads(response.readline())
        print(json.dumps(reply))
"""
    with tempfile.TemporaryDirectory(prefix="tf-bind-", dir="/tmp") as name:
        aliases = Path(name).resolve()
        service = browser.BrowserManager(
            browser.BrowserInstallation(Path("/owned/agent-browser")),
            provider,
            sandbox=Path("/usr/bin/sandbox-exec"),
            projects_root=tmp_path,
            protected_paths=(),
            read_only_paths=(aliases,),
            aliases=aliases,
        )
        try:
            async with browser.BrowserBinding(
                service,
                directory=project,
                thread="owned-thread",
                profile=project / "profile",
                permissions=PERMISSIONS,
            ) as binding:
                assert binding.socket is not None
                assert len(str(binding.socket.resolve()).encode()) > 104
                for directory in (other, project):
                    async with LocalWorkspace(
                        directory,
                        access=LocalAccess(
                            filesystem="user",
                            isolated_roots=(tmp_path,),
                            read_only_paths=(aliases,),
                        ),
                    ) as workspace:
                        async with workspace.open() as session:
                            async with session.open_process(
                                (
                                    "python",
                                    "-I",
                                    "-c",
                                    source,
                                    str(binding.socket),
                                    binding.token,
                                )
                            ) as process:
                                output, error = await process.communicate()
                                assert process.returncode == 0, error.decode()
                    if directory == other:
                        assert json.loads(output) == {"denied": True}
                        assert provider.calls == 0
                    else:
                        assert json.loads(output) == {
                            "cli": "/owned/agent-browser",
                            "environment": owners[0].environment,
                        }
            assert list(aliases.iterdir()) == []
            assert list((project / ".tinkerfin/browser-connections").iterdir()) == []
        finally:
            await service.aclose()


@pytest.mark.parametrize("kind", ["session", "binding"])
async def test_cancelled_temporary_allocation_removes_connection_and_alias(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    allocated = asyncio.Event()
    release = threading.Event()
    original = tempfile.mkdtemp

    def allocate(*, prefix: str, dir: Path | None = None) -> str:
        path = original(prefix=prefix, dir=tmp_path if dir is None else dir)
        loop.call_soon_threadsafe(allocated.set)
        release.wait()
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", allocate)
    if kind == "session":
        session = browser.BrowserSession()
        opening = asyncio.create_task(
            session.start(
                installation=browser.BrowserInstallation(Path("/owned/cli")),
                executable=Path("/owned/browser"),
                directory=tmp_path,
                profile=tmp_path / "profile",
                access=LocalAccess(filesystem="user"),
                sandbox=Path("/usr/bin/sandbox-exec"),
            )
        )
    else:
        service = browser.BrowserManager(
            browser.BrowserInstallation(Path("/owned/cli")),
            ControlledProvider(tmp_path),
            sandbox=Path("/usr/bin/sandbox-exec"),
            projects_root=tmp_path,
            protected_paths=(),
            read_only_paths=(),
        )
        binding = browser.BrowserBinding(
            service,
            directory=tmp_path,
            thread="owned-thread",
            profile=tmp_path / "profile",
            permissions=PERMISSIONS,
        )
        opening = asyncio.create_task(binding.__aenter__())
    try:
        await allocated.wait()
        opening.cancel()
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
    assert opening.cancelled()
    assert list((tmp_path / ".tinkerfin" / "browser-connections").iterdir()) == []
    assert list(tmp_path.glob("tf-browser-*")) == []
