"""原生客户端的配置、独占进程和授权 HTTP 契约；不申请桌面系统权限"""

import asyncio
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tinkerfin_desktop.native import OpenURLCommand
from tinkerfin_sandbox import LocalAccess


async def run_contract(binary: Path, mode: str, location: str | Path) -> str:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        mode,
        str(location),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return stdout.decode().strip()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_native_language_preferences_preserve_error_parameters_and_system_order(
    swift_contracts, tmp_path
):
    resources = Path(__file__).resolve().parents[1] / "resources"
    for language in ("en", "zh-Hans"):
        shutil.copytree(
            resources / f"{language}.lproj",
            tmp_path / f"resources/{language}.lproj",
        )
    assert await run_contract(swift_contracts, "localization", tmp_path) == (
        "verified localization"
    )


@pytest.mark.companion_integration
async def test_native_credential_session_reuses_loaded_values_and_clears_removed_accounts(
    swift_contracts,
):
    service = f"com.tinkerfin.companion.tests.{uuid4().hex}"
    try:
        assert await run_contract(swift_contracts, "credential-session", service) == (
            "verified credential-session"
        )
    finally:
        await run_contract(swift_contracts, "credential-cleanup", service)


async def test_native_configuration_roundtrip_privacy_and_sandbox_domain_contract(
    swift_contracts, tmp_path
):
    cases = [
        [],
        ["example.com", "*.example.com:443", "EXAMPLE.org"],
        ["localhost"],
        ["*.localhost"],
        ["localhost."],
        ["127.0.0.1."],
        ["example.com."],
        ["127.1"],
        ["0177.0.0.1"],
        ["0x7f000001"],
        ["2130706433"],
        ["[::1]"],
        ["example.com:0"],
        ["example.com:65536"],
        ["*.com"],
        ["*"],
        ["example..com"],
        ["https://example.com"],
        ["example.com\n"],
        ["example.com"] * 129,
    ]
    expected = []
    for values in cases:
        try:
            LocalAccess(network="restricted", allowed_domains=tuple(values))
            accepted = True
        except ValueError:
            accepted = False
        expected.append({"values": values, "accepted": accepted})
    (tmp_path / "domains.json").write_text(json.dumps(expected))
    assert await run_contract(swift_contracts, "configuration", tmp_path) == (
        "verified configuration"
    )


@pytest.mark.parametrize(
    "mode", ["normal", "start-failure", "early-exit", "malformed", "pipes"]
)
async def test_native_process_owns_start_messages_and_all_stop_waiters(
    swift_contracts, tmp_path, mode
):
    executable = tmp_path / "python/bin/python3"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, os, sys\n"
        f"mode = {mode!r}\n"
        "initial = json.loads(sys.stdin.readline())\n"
        "assert initial['kind'] == 'initialize'\n"
        "assert initial['readiness'] == {'accessibility':False,'screen_capture':False}\n"
        "secret = initial['configuration']['credential']\n"
        "assert secret not in str(sys.argv) and secret not in str(dict(os.environ))\n"
        "if mode == 'malformed':\n"
        "    print('invalid-json', flush=True)\n"
        "    sys.stdin.read()\n"
        "elif mode != 'early-exit':\n"
        "    if mode == 'pipes':\n"
        "        sys.stderr.write('diagnostic' * 16384)\n"
        "        sys.stderr.flush()\n"
        "    print(json.dumps({'kind':'state','state':'online','padding':'x' * 131072}), flush=True)\n"
        "    assert json.loads(sys.stdin.readline()) == {'kind':'stop'}\n"
        "print(json.dumps({'kind':'state','state':'stopped'}), flush=True)\n"
    )
    executable.chmod(0o700)
    assert await run_contract(swift_contracts, mode, tmp_path) == f"verified {mode}"


async def run_feedback_contract(
    binary: Path, directory: Path, mode: str, result: str
) -> None:
    """显式放行响应，核验宿主启动、连接结果与按钮反馈的边界"""
    hosts: asyncio.Queue[asyncio.StreamWriter] = asyncio.Queue(maxsize=2)
    host_writers: list[asyncio.StreamWriter] = []
    requests: asyncio.Queue[asyncio.Future] = asyncio.Queue(maxsize=2)
    cancellations: asyncio.Queue[asyncio.Future] = asyncio.Queue(maxsize=1)
    handlers: set[asyncio.Task] = set()

    def host_connected(_reader, writer):
        host_writers.append(writer)
        hosts.put_nowait(writer)

    async def respond(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.partition(b":")[2])
                    for line in header.lower().split(b"\r\n")
                    if line.startswith(b"content-length:")
                ),
                0,
            )
            await reader.readexactly(length)
            path = header.split(b" ", 2)[1]
            if path == b"/api/devices/projects":
                status, body = (
                    200,
                    {
                        "code": 0,
                        "data": {
                            "account_name": "contract",
                            "items": [],
                            "next_cursor": None,
                        },
                    },
                )
            else:
                response = asyncio.get_running_loop().create_future()
                queue = cancellations if path.endswith(b"/cancel") else requests
                queue.put_nowait(response)
                status, body = await response
            content = json.dumps(body).encode()
            writer.write(
                f"HTTP/1.1 {status} Test\r\nConnection: close\r\n".encode()
                + f"Content-Length: {len(content)}\r\n\r\n".encode()
                + content
            )
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    def connected(reader, writer):
        handlers.add(asyncio.create_task(respond(reader, writer)))

    host_server = await asyncio.start_server(host_connected, "127.0.0.1", 0)
    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    account = str(uuid4())
    resources = directory / "resources"
    executable = resources / "python/bin/python3"
    executable.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "native/feedback_host.py"
    executable.write_text(f"#!{Path(sys.executable).resolve()}\n" + fixture.read_text())
    executable.chmod(0o700)
    (resources / "feedback-control.json").write_text(
        json.dumps({"port": host_server.sockets[0].getsockname()[1]})
    )
    (directory / "feedback.json").write_text(
        json.dumps(
            {
                "account": account,
                "server": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                "result": result,
            }
        )
    )
    process = None
    exited = None
    diagnostics = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            mode,
            str(directory),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        commands = process.stdin
        events = process.stdout
        current_host = None
        current_request = None
        current_cancellation = None
        exited = asyncio.create_task(process.wait())
        diagnostics = asyncio.create_task(process.stderr.read())

        async def while_running(operation):
            pending = asyncio.create_task(operation)
            try:
                done, _ = await asyncio.wait(
                    [pending, exited], return_when=asyncio.FIRST_COMPLETED
                )
                if pending in done:
                    return await pending
                raise AssertionError((await diagnostics).decode())
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        def authorization(state):
            return {
                "code": 0,
                "data": {
                    "authorization_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "state": state,
                    "device": {
                        "device_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "namespace": "ns_1",
                        "name": "Contract Mac",
                    }
                    if state == "authorized"
                    else None,
                },
            }

        async def send_host(event):
            assert current_host is not None
            current_host.write(json.dumps(event).encode() + b"\n")
            await current_host.drain()

        value = ""
        while line := await events.readline():
            value = line.decode().strip()
            if value == f"verified {mode}":
                break
            assert value.startswith("checkpoint "), value
            checkpoint = value.removeprefix("checkpoint ")
            if checkpoint == "initial-started":
                current_host = await while_running(hosts.get())
                await send_host({"kind": "state", "state": "offline"})
            elif checkpoint == "retry-started":
                current_host = await while_running(hosts.get())
                await send_host({"kind": "state", "state": "connecting"})
            elif checkpoint == "retry-result":
                if result == "failure":
                    await send_host({"kind": "failure", "message": "Contract failure"})
                elif result == "exit":
                    await send_host({"kind": "exit"})
                else:
                    await send_host({"kind": "state", "state": result})
            elif checkpoint == "authorization-started":
                current_request = await while_running(requests.get())
            elif checkpoint == "authorization-result":
                assert current_request is not None
                response = (
                    (503, {"code": 1})
                    if result == "error"
                    else (200, authorization(result))
                )
                current_request.set_result(response)
            elif checkpoint == "authorization-connected":
                current_host = await while_running(hosts.get())
                await send_host({"kind": "state", "state": "online"})
            elif checkpoint == "authorization-cancelling":
                current_cancellation = await while_running(cancellations.get())
            elif checkpoint == "authorization-cancel-result":
                assert current_cancellation is not None
                current_cancellation.set_result((200, authorization("cancelled")))
            elif checkpoint == "authorization-recovery":
                current_request = await while_running(requests.get())
                current_request.set_result((200, authorization("cancelled")))
            else:
                raise AssertionError(f"Unknown checkpoint {checkpoint}")
            commands.write((checkpoint + "\n").encode())
            await commands.drain()
        commands.close()
        await commands.wait_closed()
        stdout = await events.read()
        await exited
        assert process.returncode == 0, (await diagnostics).decode()
        assert value == f"verified {mode}" and not stdout
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if exited is not None:
            await asyncio.gather(exited, return_exceptions=True)
        if diagnostics is not None:
            await asyncio.gather(diagnostics, return_exceptions=True)
        host_server.close()
        server.close()
        for writer in host_writers:
            writer.close()
        for task in handlers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await asyncio.gather(
            *(writer.wait_closed() for writer in host_writers),
            return_exceptions=True,
        )
        await host_server.wait_closed()
        await server.wait_closed()
        await run_contract(binary, "feedback-cleanup", directory)


@pytest.mark.companion_integration
@pytest.mark.parametrize(
    "result", ["online", "offline", "unauthorized", "failure", "exit"]
)
async def test_native_retry_waits_for_connection_result_after_host_restart(
    swift_contracts, tmp_path, result
):
    await run_feedback_contract(swift_contracts, tmp_path, "retry-feedback", result)


@pytest.mark.companion_integration
@pytest.mark.parametrize(
    "result",
    ["authorized", "cancelled", "expired", "superseded", "error", "cancel-request"],
)
async def test_native_authorization_feedback_covers_completion_failure_and_cancellation(
    swift_contracts, tmp_path, result
):
    await run_feedback_contract(
        swift_contracts, tmp_path, "authorization-feedback", result
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            200,
            {
                "code": 0,
                "data": {
                    "authorization_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "state": "authorized",
                    "device": {
                        "device_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "namespace": "ns_1",
                        "name": "Contract Mac",
                    },
                },
            },
            '"namespace":"ns_1"',
        ),
        (410, {"code": 1001012001}, "授权已过期"),
        (404, {"detail": "Not Found"}, "本机连接服务暂不可用"),
        (404, {"code": 1001012000}, "授权已过期"),
        (302, {"code": 0}, "连接尚未完成"),
        (
            200,
            {
                "code": 0,
                "data": {
                    "authorization_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "state": "authorized",
                    "device": {
                        "device_id": "invalid",
                        "namespace": "ns_1",
                        "name": "Mac",
                    },
                },
            },
            "设备身份无效",
        ),
    ],
)
async def test_native_authorization_identity_errors_and_redirect_refusal(
    swift_contracts, status, body, expected
):
    requests = []
    connections: set[asyncio.Task] = set()

    async def respond(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.partition(b":")[2])
                for line in header.lower().split(b"\r\n")
                if line.startswith(b"content-length:")
            )
            content = json.loads(await reader.readexactly(length))
            requests.append((header, content))
            response = json.dumps(body).encode()
            writer.write(
                f"HTTP/1.1 {status} Test\r\n".encode()
                + b"Location: /unapproved\r\nConnection: close\r\n"
                + f"Content-Length: {len(response)}\r\n\r\n".encode()
                + response
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connect(reader, writer):
        task = asyncio.create_task(respond(reader, writer))
        connections.add(task)

    server = await asyncio.start_server(connect, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await run_contract(
            swift_contracts, "authorization", f"http://127.0.0.1:{port}"
        )
        assert expected in result
        assert len(requests) == 1
        header, content = requests[0]
        assert header.startswith(
            b"POST /api/devices/authorizations/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/status HTTP/1.1"
        )
        assert content["credential"] == "b" * 64
        assert b"cookie:" not in header.lower()
    finally:
        server.close()
        await server.wait_closed()
        for task in connections:
            if not task.done():
                task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)


async def test_native_desktop_refuses_unobserved_targets(swift_contracts, tmp_path):
    assert (
        await run_contract(swift_contracts, "desktop", tmp_path) == "verified desktop"
    )


async def test_native_readiness_separates_web_desktop_and_capture_actions(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "native-readiness", tmp_path)
        == "verified native-readiness"
    )


async def test_native_web_url_validation_matches_the_tool_contract(
    swift_contracts, tmp_path
):
    cases = []
    for url in [
        "https://example.test/page?q=1#part",
        "http://localhost:3000",
        "http://127.0.0.1:5173/",
        "http://[::1]:8000",
        "https://example.test/a b",
        "file:///tmp/report",
        "javascript:alert(1)",
        "https://user:secret@example.test",
        "https://@example.test",
        "https:///example.test",
        "https://example.test\n",
        " https://example.test",
        "https://example.test:0",
        "https://example.test:65536",
    ]:
        try:
            OpenURLCommand(url=url)
            accepted = True
        except ValueError:
            accepted = False
        cases.append({"url": url, "accepted": accepted})
    (tmp_path / "websites.json").write_text(json.dumps(cases))
    assert (
        await run_contract(swift_contracts, "website", tmp_path) == "verified website"
    )


async def test_native_capture_requires_unique_complete_window_identity(
    swift_contracts, tmp_path
):
    assert await run_contract(swift_contracts, "capture-window", tmp_path) == (
        "verified capture-window"
    )


async def test_native_mcp_command_preserves_argv_without_shell_expansion(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "mcp-command", tmp_path)
        == "verified mcp-command"
    )


@pytest.mark.companion_integration
async def test_native_connection_failures_expose_the_matching_recovery(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "connection-recovery", tmp_path)
        == "verified connection-recovery"
    )


async def test_native_connect_link_accepts_only_the_client_open_entry(
    swift_contracts, tmp_path
):
    assert await run_contract(swift_contracts, "connect-link", tmp_path) == (
        "verified connect-link"
    )


async def test_missing_pending_credential_can_be_cancelled_without_losing_local_grants(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "pending-recovery", tmp_path)
        == "verified pending-recovery"
    )


async def test_menu_bar_panel_tracks_icon_and_fits_each_screen(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "panel-geometry", tmp_path)
        == "verified panel-geometry"
    )


async def test_native_panel_preserves_form_drafts_and_independent_errors(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "panel-drafts", tmp_path)
        == "verified panel-drafts"
    )


async def test_native_prepares_new_projects_and_preserves_each_account_directory(
    swift_contracts, tmp_path
):
    assert (
        await run_contract(swift_contracts, "managed-projects", tmp_path)
        == "verified managed-projects"
    )
