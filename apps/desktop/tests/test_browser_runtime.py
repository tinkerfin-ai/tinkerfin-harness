"""官方浏览器工具的私有入口、会话隔离与进程关闭"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import select
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from tinkerfin_desktop import browser_command
from tinkerfin_desktop.browser import BrowserInstallation, BrowserSession
from tinkerfin_sandbox import LocalAccess, LocalProcess, LocalWorkspace

pytestmark = [
    pytest.mark.companion_integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="需要 macOS 本机进程作用域"),
]


@pytest.fixture(scope="module")
def browser_installation() -> BrowserInstallation:
    root = Path(__file__).resolve().parents[1] / "runtime"
    node = shutil.which("node")
    if (
        node is None
        or not (root / "node_modules/@playwright/mcp/package.json").is_file()
    ):
        pytest.skip("需要安装锁定的桌面 Node 依赖")
    return BrowserInstallation(Path(node).resolve(), root / "browser-service.mjs")


@pytest.fixture
def browser_directory() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tf-browser-", dir="/tmp") as temporary:
        yield Path(temporary).resolve()


@pytest.fixture
def chrome() -> Path:
    path = Path(
        os.environ.get(
            "TINKERFIN_TEST_CHROME",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    )
    if not path.is_file():
        pytest.skip("需要 Chrome；测试始终使用独立资料目录")
    return path


@dataclass(frozen=True, slots=True)
class BrowserScope:
    process: LocalProcess
    socket: Path
    token: str
    profile: Path


async def request(scope: BrowserScope, method: str, **values: JsonValue) -> dict:
    reader, writer = await asyncio.open_unix_connection(
        scope.socket, limit=8 * 1024 * 1024 + 1
    )
    try:
        writer.write(
            json.dumps({"token": scope.token, "method": method, **values}).encode()
            + b"\n"
        )
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


async def call(
    scope: BrowserScope, name: str, arguments: dict[str, JsonValue] | None = None
) -> dict:
    reply = await request(scope, "call", name=name, arguments=arguments or {})
    assert "error" not in reply, reply
    return reply["result"]


@asynccontextmanager
async def daemon(
    installation: BrowserInstallation,
    directory: Path,
    executable: Path,
    *,
    sandbox: Path,
    access: LocalAccess | None = None,
    startup_proxy: str | None = None,
) -> AsyncIterator[BrowserScope]:
    directory.mkdir(parents=True, exist_ok=True)
    config = {
        "socket": str(directory / "browser.sock"),
        "token": secrets.token_hex(32),
        "profile": str(directory / "profile"),
        "workspace": str(directory),
        "executable": str(executable),
        "headed": False,
    }
    async with LocalWorkspace(
        directory,
        access=access or LocalAccess(filesystem="user", graphics=True),
        sandbox_executable=sandbox,
        environment={"MAC_CHROMIUM_TMPDIR": str(directory)},
        commands={"node": installation.node},
    ) as workspace:
        async with workspace.open() as session:
            command = (str(installation.node), str(installation.service))
            if startup_proxy is not None:
                command = ("/usr/bin/env", f"HTTP_PROXY={startup_proxy}", *command)
            async with session.open_process(command) as process:
                diagnostics = bytearray()

                async def drain():
                    while content := await process.stderr.read(4096):
                        diagnostics.extend(content[: max(0, 65536 - len(diagnostics))])

                reader = asyncio.create_task(drain())
                scope = BrowserScope(
                    process,
                    Path(config["socket"]),
                    config["token"],
                    Path(config["profile"]),
                )
                try:
                    process.stdin.write(json.dumps(config).encode() + b"\n")
                    await process.stdin.drain()
                    assert await process.stdout.readline() == b'{"event":"ready"}\n', (
                        diagnostics.decode()
                    )
                    yield scope
                finally:
                    try:
                        if process.returncode is None and scope.socket.exists():
                            await request(scope, "shutdown")
                            assert await process.wait() == 0, diagnostics.decode()
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)


async def test_tool_catalog_and_token_reject_unowned_actions(
    browser_installation, browser_directory
):
    async with daemon(
        browser_installation,
        browser_directory,
        Path("/usr/bin/false"),
        sandbox=Path("/usr/bin/sandbox-exec"),
    ) as scope:
        denied = await request(
            scope, "call", token="0" * 64, name="browser_snapshot", arguments={}
        )
        assert denied == {"error": "Browser request denied"}
        assert (await request(scope, "connect", arguments={})) == {
            "error": "Browser request denied"
        }
        assert "error" in await request(
            scope,
            "call",
            name="browser_run_code_unsafe",
            arguments={"code": "process.exit()"},
        )
        catalog = (await request(scope, "tools"))["result"]
        assert any(tool["name"] == "browser_snapshot" for tool in catalog)
        assert all(tool["name"] != "browser_run_code_unsafe" for tool in catalog)
        assert all(tool["name"] != "browser_close" for tool in catalog)
        description = await request(scope, "describe", name="browser_navigate")
        assert "url" in description["result"]["inputSchema"]["properties"]
        assert not scope.profile.exists()
        assert scope.socket.stat().st_mode & 0o777 == 0o600
        for payload in (b"{", b"\xff\n", b"{}\n{}\n"):
            reader, writer = await asyncio.open_unix_connection(scope.socket)
            try:
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                response = await reader.read()
                assert not response or json.loads(response) == {
                    "error": "Browser request denied"
                }
            finally:
                writer.close()
                await writer.wait_closed()
        assert (await request(scope, "tools"))["result"] == catalog


@pytest.mark.parametrize(
    "arguments",
    [
        ("--cdp", "9222"),
        ("--profile", "/tmp/other"),
        ("call", "browser_snapshot", "[]"),
        ("call", "browser_snapshot", '{"x":NaN}'),
    ],
)
async def test_cli_rejects_scope_overrides_and_invalid_input_without_starting_browser(
    browser_directory, arguments
):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(browser_command.__file__)),
        *arguments,
        cwd=browser_directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await process.communicate()
        assert process.returncode != 0
        assert list(browser_directory.iterdir()) == []
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_cli_does_not_retry_when_action_response_is_lost(browser_directory):
    token = "c" * 64
    preparation = browser_directory / "prepare.sock"
    action = browser_directory / "action.sock"
    calls = []
    handlers = []

    async def prepare(reader, writer):
        try:
            assert await reader.readline() == f"prepare {token}\n".encode()
            writer.write(
                json.dumps(
                    {
                        "environment": {
                            "TINKERFIN_BROWSER_SESSION_TOKEN": token,
                            "TINKERFIN_BROWSER_SESSION_SOCKET": str(action),
                        }
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def lose_response(reader, writer):
        try:
            calls.append(json.loads(await reader.readline()))
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(handler):
        def connected(reader, writer):
            handlers.append(asyncio.create_task(handler(reader, writer)))

        return connected

    servers = [
        await asyncio.start_unix_server(accept(prepare), preparation),
        await asyncio.start_unix_server(accept(lose_response), action),
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(browser_command.__file__)),
            "call",
            "browser_snapshot",
            "{}",
            env={
                **os.environ,
                "TINKERFIN_BROWSER_SOCKET": str(preparation),
                "TINKERFIN_BROWSER_TOKEN": token,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 1
        assert not stdout
        assert "操作未确认完成" in stderr.decode()
        assert len(calls) == 1
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        for server in servers:
            server.close()
            await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_owned_sessions_keep_page_state_and_close_independently(
    browser_installation, browser_directory, chrome
):
    if sys.platform != "darwin":
        pytest.skip("需要 macOS 进程退出通知")
    first = browser_directory / "first"
    second = browser_directory / "second"
    async with daemon(
        browser_installation, second, chrome, sandbox=Path("/usr/bin/sandbox-exec")
    ) as other:
        await call(other, "browser_navigate", {"url": "about:blank"})
        async with daemon(
            browser_installation, first, chrome, sandbox=Path("/usr/bin/sandbox-exec")
        ) as scope:
            await call(scope, "browser_navigate", {"url": "about:blank"})
            await call(
                scope,
                "browser_evaluate",
                {"function": "() => { window.name = 'retained'; return window.name; }"},
            )
            assert "retained" in json.dumps(
                await call(scope, "browser_evaluate", {"function": "() => window.name"})
            )
            assert "retained" not in json.dumps(
                await call(other, "browser_evaluate", {"function": "() => window.name"})
            )
            result = await call(
                scope,
                "browser_take_screenshot",
                {"type": "png", "filename": str(first / "screen.png")},
            )
            assert not result.get("isError"), result
            assert (first / "screen.png").read_bytes().startswith(b"\x89PNG")
            assert not (scope.profile / "DevToolsActivePort").exists()
            pids = await owned_processes(scope.process.pid)
            with closing(select.kqueue()) as exits:
                exits.control(
                    [
                        select.kevent(
                            pid,
                            filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT,
                        )
                        for pid in pids
                    ],
                    0,
                    0,
                )
                code, listening = await host_command(
                    "/usr/sbin/lsof",
                    "-a",
                    "-p",
                    ",".join(map(str, pids)),
                    "-iTCP",
                    "-sTCP:LISTEN",
                    "-Fpn",
                )
                assert code == 1 and not listening
                await request(scope, "shutdown")
                assert await scope.process.wait() == 0
                observed: set[int] = set()
                while observed != pids:
                    events = await asyncio.to_thread(
                        exits.control, None, len(pids), None
                    )
                    observed.update(event.ident for event in events)
        assert scope.process.returncode == 0
        assert not (await call(other, "browser_snapshot")).get("isError")
    assert other.process.returncode == 0
    assert (first / "profile").is_dir()


async def host_command(*arguments: str) -> tuple[int | None, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        output, error = await process.communicate()
        assert not error, error.decode()
        return process.returncode, output
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def owned_processes(parent: int) -> set[int]:
    code, output = await host_command("/bin/ps", "-axo", "pid=,ppid=,command=")
    assert code == 0
    entries = [line.split(None, 2) for line in output.splitlines()]
    owned = {parent}
    while True:
        descendants = {
            int(pid) for pid, ppid, _command in entries if int(ppid) in owned
        }
        if descendants <= owned:
            break
        owned.update(descendants)
    assert any(
        int(pid) in owned and b"Google Chrome" in command for pid, _, command in entries
    )
    # LocalWorkspace 的 supervisor 在作用域退出时关闭，不属于浏览器动作进程
    return owned - {parent}


async def test_application_session_waits_for_service_exit(
    browser_installation, browser_directory, chrome
):
    session = BrowserSession()
    await session.start(
        installation=browser_installation,
        executable=chrome,
        directory=browser_directory,
        profile=browser_directory / "profile",
        access=LocalAccess(filesystem="user", graphics=True),
        sandbox=Path("/usr/bin/sandbox-exec"),
    )
    try:
        assert session.available
        socket = Path(session.environment["TINKERFIN_BROWSER_SESSION_SOCKET"])
        assert socket.is_socket()
    finally:
        await session.aclose()
    assert not session.available
    assert not socket.exists()
    assert list((browser_directory / ".tinkerfin/browser-connections").iterdir()) == []


@pytest.mark.parametrize("disconnect", ["active", "queued"])
async def test_lost_action_connection_closes_browser_without_repeating(
    browser_installation, browser_directory, chrome, disconnect
):
    requested = asyncio.Event()
    visits = []
    handlers = []

    async def handle(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            first = header.split(b"\r\n", 1)[0]
            visits.append(first)
            writer.write(
                b"HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            if b" /signal " in first:
                requested.set()
        except asyncio.IncompleteReadError as error:
            assert not error.partial
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        handlers.append(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        async with daemon(
            browser_installation,
            browser_directory,
            chrome,
            sandbox=Path("/usr/bin/sandbox-exec"),
        ) as scope:
            await call(scope, "browser_navigate", {"url": url})
            visits.clear()
            requested.clear()
            reader, writer = await asyncio.open_unix_connection(scope.socket)
            writers = [writer]
            try:
                function = "async () => { await fetch('/signal'); await new Promise(() => {}); }"
                writer.write(
                    json.dumps(
                        {
                            "token": scope.token,
                            "method": "call",
                            "name": "browser_evaluate",
                            "arguments": {"function": function},
                        }
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                await requested.wait()
                abandoned = writer
                if disconnect == "queued":
                    _, abandoned = await asyncio.open_unix_connection(scope.socket)
                    writers.append(abandoned)
                    abandoned.write(
                        json.dumps(
                            {
                                "token": scope.token,
                                "method": "call",
                                "name": "browser_evaluate",
                                "arguments": {
                                    "function": "async () => { await fetch('/queued'); }"
                                },
                            }
                        ).encode()
                        + b"\n"
                    )
                    await abandoned.drain()
                abandoned.close()
                await abandoned.wait_closed()
                assert await scope.process.wait() == 0
                assert await reader.read() == b""
                assert len([line for line in visits if b"/signal " in line]) == 1
                assert not any(b"/queued " in line for line in visits)
            finally:
                for connection in writers:
                    connection.close()
                    await connection.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_browser_uses_its_owned_proxy(
    browser_installation, browser_directory, chrome
):
    visited = []
    handlers = []

    async def handle(reader, writer):
        try:
            try:
                header = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError as error:
                assert not error.partial
                return
            first = header.split(b"\r\n", 1)[0]
            visited.append(first)
            body = (
                b"<title>Owned proxy</title>"
                if first.startswith(b"GET http://managed.invalid/")
                else b"denied"
            )
            status = b"200 OK" if body.startswith(b"<") else b"403 Forbidden"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        handlers.append(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        proxy = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with daemon(
            browser_installation,
            browser_directory,
            chrome,
            sandbox=Path("/usr/bin/sandbox-exec"),
            startup_proxy=proxy,
        ) as scope:
            result = await call(
                scope, "browser_navigate", {"url": "http://managed.invalid/"}
            )
            assert not result.get("isError"), result
            assert "Owned proxy" in json.dumps(result)
        assert any(line.startswith(b"GET http://managed.invalid/") for line in visited)
    finally:
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_restricted_browser_preserves_file_and_loopback_boundaries(
    browser_installation, browser_directory, chrome, native_sandbox
):
    protected = browser_directory / "credential.txt"
    protected.write_text("protected-browser-fixture")
    page = browser_directory / "page.html"
    page.write_text("<title>Owned page</title>")
    reached = asyncio.Event()
    connections = []

    def accept(_reader, writer):
        reached.set()
        connections.append(writer)
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        address = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with daemon(
            browser_installation,
            browser_directory,
            chrome,
            sandbox=native_sandbox,
            access=LocalAccess(
                filesystem="user",
                network="restricted",
                graphics=True,
                unix_socket_paths=(browser_directory,),
                protected_paths=(protected,),
            ),
        ) as scope:
            result = await call(scope, "browser_navigate", {"url": page.as_uri()})
            assert not result.get("isError"), result
            assert "Owned page" in json.dumps(result)
            denied = await call(scope, "browser_navigate", {"url": protected.as_uri()})
            assert denied.get("isError") is True
            assert "protected-browser-fixture" not in json.dumps(denied)
            await call(scope, "browser_navigate", {"url": address})
            network = await call(scope, "browser_network_requests")
            assert "403" in json.dumps(network), network
        assert not reached.is_set()
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*(writer.wait_closed() for writer in connections))
