"""受管官方 CLI 的作用域、请求身份与真实浏览器进程归属"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import select
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, closing
from pathlib import Path

import pytest
from pydantic import JsonValue

from tinkerfin_sandbox import LocalAccess, LocalProcess, LocalSession, LocalWorkspace

pytestmark = [
    pytest.mark.companion_integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="需要 macOS 本机进程作用域"),
]


@pytest.fixture(scope="module")
def browser_cli() -> Path:
    root = Path(__file__).resolve().parents[3]
    binary = Path(
        os.environ.get(
            "TINKERFIN_TEST_BROWSER_CLI",
            str(root / ".agents/tools/agent-browser-managed/agent-browser"),
        )
    )
    if not binary.is_file():
        pytest.skip("需要先构建带 managed-process 特性的原生 CLI")
    manifest = json.loads(Path(str(binary) + ".json").read_text())
    assert manifest["features"] == ["managed-process"]
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == manifest["binary_sha256"]
    return binary.resolve()


@pytest.fixture
def browser_directory() -> Iterator[Path]:
    # Darwin 的 Unix socket 地址容量独立于文件系统路径容量
    with tempfile.TemporaryDirectory(prefix="tf-ab-", dir="/tmp") as temporary:
        yield Path(temporary).resolve()


def environment(
    root: Path, name: str, executable: Path | None = None
) -> dict[str, str]:
    directory = root / name
    directory.mkdir()
    for child in ("socket", "profile", "state"):
        (directory / child).mkdir()
    config = directory / "config.json"
    config.write_text("{}\n")
    return {
        "AGENT_BROWSER_SESSION": name,
        "AGENT_BROWSER_SOCKET_DIR": str(directory / "socket"),
        "AGENT_BROWSER_PROFILE": str(directory / "profile"),
        "AGENT_BROWSER_STATE_DIR": str(directory / "state"),
        "AGENT_BROWSER_CONFIG": str(config),
        "AGENT_BROWSER_EXECUTABLE_PATH": str(executable or Path("/usr/bin/false")),
        "AGENT_BROWSER_CONNECTION_TOKEN": secrets.token_hex(32),
        "AGENT_BROWSER_HEADED": "1",
        "AGENT_BROWSER_ARGS": "--no-sandbox",
        "AGENT_BROWSER_IDLE_TIMEOUT_MS": "0",
        "MAC_CHROMIUM_TMPDIR": str(directory / "socket"),
    }


async def invoke(
    session: LocalSession,
    binary: Path,
    *arguments: str,
    content: bytes | None = None,
    temporary_proxy: str | None = None,
) -> tuple[int, str, str]:
    command = [str(binary), *arguments]
    if temporary_proxy is not None:
        command = ["/usr/bin/env", f"HTTP_PROXY={temporary_proxy}", *command]
    async with session.open_process(command) as process:
        output, error = await process.communicate(content)
        return await process.wait(), output.decode(), error.decode()


@asynccontextmanager
async def daemon(
    workspace: LocalWorkspace,
    binary: Path,
    scope: dict[str, str],
    *,
    startup_proxy: str | None = None,
) -> AsyncIterator[LocalProcess]:
    command = ["/usr/bin/env", "AGENT_BROWSER_DAEMON=1"]
    if startup_proxy is not None:
        command.append(f"HTTP_PROXY={startup_proxy}")
    command.append(str(binary))
    diagnostics = bytearray()

    async def drain(reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(4096):
            diagnostics.extend(chunk[: max(0, 65536 - len(diagnostics))])

    async with workspace.open() as session:
        reader: asyncio.Task[None] | None = None
        failure: BaseException | None = None
        try:
            async with session.open_process(command) as process:
                reader = asyncio.create_task(drain(process.stderr))
                assert await process.stdout.readline() == b'{"event":"ready"}\n'
                path = Path(scope["AGENT_BROWSER_SOCKET_DIR"])
                assert (path / f"{scope['AGENT_BROWSER_SESSION']}.sock").is_socket()
                assert not list(path.glob("*.stream"))
                assert not (
                    Path(scope["AGENT_BROWSER_PROFILE"]) / "DevToolsActivePort"
                ).exists()
                yield process
        except BaseException as error:
            failure = error
            raise
        finally:
            if reader is not None:
                await reader
            if failure is not None and diagnostics:
                failure.add_note(diagnostics.decode(errors="replace"))


async def request(scope: dict[str, str], payload: dict) -> dict:
    path = Path(scope["AGENT_BROWSER_SOCKET_DIR"]) / (
        scope["AGENT_BROWSER_SESSION"] + ".sock"
    )
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


async def test_missing_daemon_fails_without_autostart(browser_cli, browser_directory):
    scope = environment(browser_directory, "offline")
    async with LocalWorkspace(
        browser_directory, access=LocalAccess(filesystem="user"), environment=scope
    ) as workspace:
        async with workspace.open() as session:
            code, output, error = await invoke(
                session, browser_cli, "get", "url", "--json"
            )
    assert code != 0
    assert "managed" in (output + error).lower()
    assert list(Path(scope["AGENT_BROWSER_SOCKET_DIR"]).iterdir()) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("--session", "another", "get", "url"),
        ("--namespace", "another", "get", "url"),
        ("--profile", "/tmp/another-browser", "get", "url"),
        ("--executable-path", "/usr/bin/true", "get", "url"),
        ("--config", "{config}", "get", "url"),
        ("--args", "--user-data-dir=/tmp/another-browser", "get", "url"),
        ("--auto-connect", "get", "url"),
        ("--cdp", "9222", "get", "url"),
        ("--engine", "lightpanda", "get", "url"),
        ("connect", "9222"),
        ("close", "--all"),
        ("install",),
        ("upgrade",),
        ("dashboard",),
        ("doctor",),
        ("profiles",),
        ("plugin", "list"),
        ("plugins", "list"),
        ("chat", "hello"),
        ("inspect",),
        ("stream", "enable"),
        ("stream", "disable"),
        ("stream", "status"),
        ("get", "cdp-url"),
        ("--proxy", "http://127.0.0.1:1234", "get", "url"),
        ("--proxy-bypass", "localhost", "get", "url"),
    ],
)
async def test_scope_overrides_and_unowned_services_are_rejected(
    browser_cli, browser_directory, arguments
):
    scope = environment(browser_directory, "scope")
    other_config = browser_directory / "other.json"
    other_config.write_text("{}\n")
    selected = [
        argument.replace("{config}", str(other_config)) for argument in arguments
    ]
    async with LocalWorkspace(
        browser_directory, access=LocalAccess(filesystem="user"), environment=scope
    ) as workspace:
        async with workspace.open() as session:
            code, output, error = await invoke(
                session, browser_cli, *selected, "--json"
            )
    assert code != 0
    assert any(
        word in (output + error)
        for word in ("owned by the host", "external browsers", "debugging endpoint")
    )
    assert list(Path(scope["AGENT_BROWSER_SOCKET_DIR"]).iterdir()) == []


async def test_help_version_and_skills_need_no_runtime_identity(
    browser_cli, browser_directory
):
    config = browser_directory / "empty.json"
    config.write_text("{}\n")
    scope = {
        "AGENT_BROWSER_CONFIG": str(config),
        "AGENT_BROWSER_SKILLS_DIR": str(
            browser_cli.parent / "agent-browser-assets" / "skill-data"
        ),
    }
    async with LocalWorkspace(
        browser_directory, access=LocalAccess(filesystem="user"), environment=scope
    ) as workspace:
        async with workspace.open() as session:
            for arguments in (("--help",), ("--version",), ("skills", "get", "core")):
                code, output, error = await invoke(session, browser_cli, *arguments)
                assert code == 0, error
                assert output


async def test_daemon_authenticates_before_actions_without_launching_chrome(
    browser_cli, browser_directory
):
    scope = environment(browser_directory, "authenticated")
    async with LocalWorkspace(
        browser_directory, access=LocalAccess(filesystem="user"), environment=scope
    ) as workspace:
        async with daemon(workspace, browser_cli, scope) as process:
            command = {"id": "owned", "action": "session_info"}
            for payload in (
                command,
                {"connectionToken": "0" * 64, "command": command},
            ):
                result = await request(scope, payload)
                assert result["success"] is False
                assert result["error"] == "Invalid managed browser identity"
            result = await request(
                scope,
                {
                    "connectionToken": scope["AGENT_BROWSER_CONNECTION_TOKEN"],
                    "command": command,
                },
            )
            assert result["success"] is True, result
            commands: list[dict[str, JsonValue]] = [
                {"id": "service", "action": action}
                for action in (
                    "inspect",
                    "cdp_url",
                    "stream_enable",
                    "stream_disable",
                    "stream_status",
                )
            ]
            launch_overrides: tuple[tuple[str, JsonValue], ...] = (
                ("proxy", {"server": "http://127.0.0.1:1234"}),
                ("cdpPort", 9222),
                ("autoConnect", True),
                ("provider", "safari"),
                ("deviceName", "iPhone"),
                ("udid", "unowned"),
                ("platformVersion", "18"),
                ("port", 4444),
                ("profile", "/tmp/unowned-profile"),
                ("executablePath", "/usr/bin/true"),
                ("args", ["--remote-debugging-port=9222"]),
                ("extensions", ["/tmp/unowned-extension"]),
                ("engine", "lightpanda"),
                ("headless", True),
                ("noXvfb", True),
                ("unknownLaunchField", True),
            )
            for field, value in launch_overrides:
                commands.append({"id": "override", "action": "launch", field: value})
            plugin_overrides: tuple[JsonValue, ...] = (
                None,
                {},
                [{"name": "mutator", "capabilities": ["launch.mutate"]}],
            )
            for action in ("launch", "navigate", "session_info"):
                for plugins in plugin_overrides:
                    commands.append(
                        {"id": "plugin", "action": action, "plugins": plugins}
                    )
            for command in commands:
                result = await request(
                    scope,
                    {
                        "connectionToken": scope["AGENT_BROWSER_CONNECTION_TOKEN"],
                        "command": command,
                    },
                )
                assert result["success"] is False, result
                assert any(
                    word in result["error"]
                    for word in ("owned by the host", "host-managed scope")
                ), result
            result = await request(
                scope,
                {
                    "connectionToken": scope["AGENT_BROWSER_CONNECTION_TOKEN"],
                    "command": {"id": "still-usable", "action": "session_info"},
                },
            )
            assert result["success"] is True, result
            assert not list(Path(scope["AGENT_BROWSER_SOCKET_DIR"]).glob("*.stream"))
            code, listening = await host_command(
                "/usr/sbin/lsof",
                "-a",
                "-g",
                str(process.pid),
                "-iTCP",
                "-sTCP:LISTEN",
                "-Fpn",
            )
            assert code == 1 and not listening

            assert not (
                Path(scope["AGENT_BROWSER_PROFILE"]) / "DevToolsActivePort"
            ).exists()


async def test_daemon_rejects_external_cdp_before_connecting(
    browser_cli, browser_directory
):
    scope = environment(browser_directory, "external-cdp")
    connections: list[asyncio.StreamWriter] = []

    def accepted(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        writer.close()

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with LocalWorkspace(
            browser_directory, access=LocalAccess(filesystem="user"), environment=scope
        ) as workspace:
            async with daemon(workspace, browser_cli, scope):
                result = await request(
                    scope,
                    {
                        "connectionToken": scope["AGENT_BROWSER_CONNECTION_TOKEN"],
                        "command": {
                            "id": "external",
                            "action": "launch",
                            "cdpUrl": f"ws://127.0.0.1:{port}/browser",
                        },
                    },
                )
                assert result["success"] is False, result
                assert (
                    "cdpUrl" in result["error"]
                    and "host-managed scope" in result["error"]
                )
    finally:
        server.close()
        await server.wait_closed()
        for writer in connections:
            await writer.wait_closed()
    assert connections == []


async def test_daemon_rejects_truncated_and_invalid_utf8_requests(
    browser_cli, browser_directory
):
    scope = environment(browser_directory, "framing")
    path = Path(scope["AGENT_BROWSER_SOCKET_DIR"]) / "framing.sock"
    async with LocalWorkspace(
        browser_directory, access=LocalAccess(filesystem="user"), environment=scope
    ) as workspace:
        async with daemon(workspace, browser_cli, scope):
            for payload, expected in ((b"{", "Truncated"), (b"\xff\n", "UTF-8")):
                reader, writer = await asyncio.open_unix_connection(path)
                try:
                    writer.write(payload)
                    await writer.drain()
                    writer.write_eof()
                    response = json.loads(await reader.readline())
                    assert response["success"] is False
                    assert expected in response["error"]
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()


async def test_startup_modes_cannot_enable_unowned_services(
    browser_cli, browser_directory
):
    for index, (name, value) in enumerate(
        (
            ("AGENT_BROWSER_DASHBOARD", "1"),
            ("AGENT_BROWSER_PROVIDER", "safari"),
            ("AGENT_BROWSER_ENGINE", "lightpanda"),
            ("AGENT_BROWSER_CDP", "9222"),
            ("AGENT_BROWSER_AUTO_CONNECT", "0"),
            (
                "AGENT_BROWSER_PLUGINS",
                '[{"name":"mutator","capabilities":["launch.mutate"]}]',
            ),
        )
    ):
        scope = environment(browser_directory, f"startup-{index}")
        scope[name] = value
        if name != "AGENT_BROWSER_DASHBOARD":
            scope["AGENT_BROWSER_DAEMON"] = "1"
        async with LocalWorkspace(
            browser_directory, access=LocalAccess(filesystem="user"), environment=scope
        ) as workspace:
            async with workspace.open() as session:
                code, output, error = await invoke(session, browser_cli)
        assert code != 0 and "ready" not in output, (name, output, error)
        assert (
            "owned by the host" in error or "external providers or plugins" in error
        ), error
        assert list(Path(scope["AGENT_BROWSER_SOCKET_DIR"]).iterdir()) == []


async def test_lost_response_does_not_repeat_request_or_restart_daemon(
    browser_cli, browser_directory
):
    scope = environment(browser_directory, "interrupted")
    sockets = Path(scope["AGENT_BROWSER_SOCKET_DIR"])
    (sockets / "interrupted.version").write_text("0.33.2")
    requests: list[dict] = []
    handlers: list[asyncio.Task[None]] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if line:
                requests.append(json.loads(line))
        finally:
            writer.close()
            await writer.wait_closed()

    def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_unix_server(accepted, sockets / "interrupted.sock")
    try:
        async with LocalWorkspace(
            browser_directory, access=LocalAccess(filesystem="user"), environment=scope
        ) as workspace:
            async with workspace.open() as session:
                code, output, error = await invoke(
                    session,
                    browser_cli,
                    "get",
                    "url",
                    "--json",
                    temporary_proxy="http://127.0.0.1:1234",
                )
                assert code != 0, output + error
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
    assert len(requests) == 1
    assert requests[0]["connectionToken"] == scope["AGENT_BROWSER_CONNECTION_TOKEN"]
    assert "proxy" not in requests[0]["command"]
    assert not list(sockets.glob("*.pid"))


async def host_command(*arguments: str) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        output, error = await process.communicate()
        assert not error, error.decode()
        assert process.returncode is not None
        return process.returncode, output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def owned_pids(group: int) -> set[int]:
    code, output = await host_command("/bin/ps", "-g", str(group), "-o", "pid=,comm=")
    assert code == 0
    assert b"Google Chrome" in output
    return {int(line.split(None, 1)[0]) for line in output.splitlines()}


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_real_browser_keeps_round_state_and_stops_only_owned_conversation(
    browser_cli, browser_directory, cancel_owner
):
    if sys.platform != "darwin":
        pytest.skip("需要 macOS 的进程退出通知")
    executable = Path(
        os.environ.get(
            "TINKERFIN_TEST_CHROME",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    )
    if not executable.is_file():
        pytest.skip("需要 Chrome 可执行程序；测试始终使用独立资料目录")
    first_scope = environment(browser_directory, "first", executable)
    second_scope = environment(browser_directory, "second", executable)
    async with (
        LocalWorkspace(
            browser_directory,
            access=LocalAccess(filesystem="user"),
            environment=first_scope,
        ) as first,
        LocalWorkspace(
            browser_directory,
            access=LocalAccess(filesystem="user"),
            environment=second_scope,
        ) as second,
        AsyncExitStack() as owned,
    ):
        if cancel_owner:
            ready: asyncio.Future[LocalProcess] = asyncio.Future()
            keep_open = asyncio.Event()

            async def retain_browser() -> None:
                try:
                    async with daemon(first, browser_cli, first_scope) as process:
                        ready.set_result(process)
                        await keep_open.wait()
                except BaseException as error:
                    if not ready.done():
                        ready.set_exception(error)
                    raise

            owner = asyncio.create_task(retain_browser())

            async def cancel_browser() -> None:
                owner.cancel()
                result = await asyncio.gather(owner, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError)

            owned.push_async_callback(cancel_browser)
            first_daemon = await ready
        else:
            first_daemon = await owned.enter_async_context(
                daemon(first, browser_cli, first_scope)
            )
        async with daemon(second, browser_cli, second_scope) as second_daemon:
            for workspace in (first, second):
                async with workspace.open() as session:
                    code, output, error = await invoke(
                        session, browser_cli, "open", "about:blank", "--json"
                    )
                    assert code == 0, output + error
            async with first.open() as round_one:
                code, output, error = await invoke(
                    round_one,
                    browser_cli,
                    "eval",
                    "window.name='retained'; window.name",
                    "--json",
                )
                assert code == 0, output + error
            async with first.open() as round_two:
                code, output, error = await invoke(
                    round_two, browser_cli, "eval", "window.name", "--json"
                )
                assert code == 0, output + error
                assert json.loads(output)["data"]["result"] == "retained"
                for command, expected in (
                    (["connect", "9222"], "external browsers"),
                    (["stream", "enable"], "owned by the host"),
                ):
                    code, output, error = await invoke(
                        round_two,
                        browser_cli,
                        "batch",
                        "--json",
                        content=json.dumps([["get", "url"], command]).encode(),
                    )
                    assert code != 0
                    assert expected in output + error
            first_pids = await owned_pids(first_daemon.pid)
            second_pids = await owned_pids(second_daemon.pid)
            for group, scope in (
                (first_daemon.pid, first_scope),
                (second_daemon.pid, second_scope),
            ):
                assert not (
                    Path(scope["AGENT_BROWSER_PROFILE"]) / "DevToolsActivePort"
                ).exists()
                code, listening = await host_command(
                    "/usr/sbin/lsof",
                    "-a",
                    "-g",
                    str(group),
                    "-iTCP",
                    "-sTCP:LISTEN",
                    "-Fpn",
                )
                assert code == 1 and not listening
            assert first_pids.isdisjoint(second_pids)
            assert all(os.getpgid(pid) == first_daemon.pid for pid in first_pids)
            with closing(select.kqueue()) as exits:
                events = [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                    for pid in first_pids
                ]
                exits.control(events, 0, 0)
                await owned.aclose()
                observed: set[int] = set()
                while observed != first_pids:
                    for event in await asyncio.to_thread(
                        exits.control, None, len(first_pids), None
                    ):
                        observed.add(event.ident)
                assert observed == first_pids
            async with second.open() as remaining:
                code, output, error = await invoke(
                    remaining, browser_cli, "get", "url", "--json"
                )
                assert code == 0, output + error
            assert Path(first_scope["AGENT_BROWSER_PROFILE"]).is_dir()


async def test_real_browser_uses_daemon_proxy_across_command_scopes(
    browser_cli, browser_directory
):
    executable = Path(
        os.environ.get(
            "TINKERFIN_TEST_CHROME",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    )
    if not executable.is_file():
        pytest.skip("需要 Chrome 可执行程序")
    tasks: list[asyncio.Task[None]] = []
    visited: list[str] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                header = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError as error:
                # 浏览器可关闭尚未发送请求的连接，非空截断请求仍视为错误
                if error.partial:
                    raise
                return
            request_line = header.split(b"\r\n", 1)[0].decode()
            owned = request_line.startswith("GET http://managed.invalid/")
            if owned:
                visited.append(request_line)
            body = (
                b"<title>Owned proxy</title><h1>Owned proxy</h1>"
                if owned
                else b"denied"
            )
            writer.write(
                (b"HTTP/1.1 200 OK" if owned else b"HTTP/1.1 403 Forbidden")
                + b"\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.append(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    proxy = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    scope = environment(browser_directory, "proxy", executable)
    try:
        async with LocalWorkspace(
            browser_directory, access=LocalAccess(filesystem="user"), environment=scope
        ) as workspace:
            async with daemon(workspace, browser_cli, scope, startup_proxy=proxy):
                async with workspace.open() as first:
                    code, output, error = await invoke(
                        first,
                        browser_cli,
                        "open",
                        "http://managed.invalid/",
                        "--json",
                        temporary_proxy="http://127.0.0.1:1",
                    )
                    assert code == 0, output + error
                async with workspace.open() as second:
                    code, output, error = await invoke(
                        second,
                        browser_cli,
                        "get",
                        "title",
                        "--json",
                        temporary_proxy="http://127.0.0.1:2",
                    )
                    assert code == 0, output + error
                    assert json.loads(output)["data"]["title"] == "Owned proxy"
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tasks)
    assert visited


async def test_restricted_real_browser_uses_its_owned_proxy_and_preserves_boundaries(
    browser_cli, browser_directory, native_sandbox
):
    executable = Path(
        os.environ.get(
            "TINKERFIN_TEST_CHROME",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    )
    if not executable.is_file():
        pytest.skip("需要 Chrome 可执行程序")
    scope = environment(browser_directory, "restricted", executable)
    protected = browser_directory / "credential.txt"
    protected.write_text("owned-protected-fixture")
    allowed = browser_directory / "allowed.html"
    allowed.write_text("<title>Owned local file</title>")
    reached = asyncio.Event()
    connections: list[asyncio.StreamWriter] = []

    def loopback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reached.set()
        connections.append(writer)
        writer.close()

    server = await asyncio.start_server(loopback, "127.0.0.1", 0)
    loopback_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        async with LocalWorkspace(
            browser_directory,
            access=LocalAccess(
                filesystem="user",
                network="restricted",
                graphics=True,
                allowed_domains=("example.com",),
                unix_socket_paths=(Path(scope["AGENT_BROWSER_SOCKET_DIR"]),),
                protected_paths=(protected,),
            ),
            environment=scope,
            sandbox_executable=native_sandbox,
        ) as workspace:
            async with daemon(workspace, browser_cli, scope):
                async with workspace.open() as first:
                    code, output, error = await invoke(
                        first, browser_cli, "open", "https://example.com", "--json"
                    )
                    assert code == 0, output + error
                async with workspace.open() as second:
                    code, output, error = await invoke(
                        second, browser_cli, "get", "title", "--json"
                    )
                    assert code == 0, output + error
                    assert json.loads(output)["data"]["title"] == "Example Domain"
                    code, output, error = await invoke(
                        second, browser_cli, "open", allowed.as_uri(), "--json"
                    )
                    assert code == 0, output + error
                    for url in (protected.as_uri(), loopback_url):
                        code, output, error = await invoke(
                            second, browser_cli, "open", url, "--json"
                        )
                        assert code != 0, output + error
                        assert "owned-protected-fixture" not in output + error
                    assert not reached.is_set()
                    code, output, error = await invoke(
                        second, browser_cli, "open", "https://example.com", "--json"
                    )
                    assert code == 0, output + error
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*(writer.wait_closed() for writer in connections))
