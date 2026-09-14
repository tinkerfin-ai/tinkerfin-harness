"""Exercise remote termination and cancellation through the installed SDK."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.transport import RetryPolicy

from tinkerfin_sandbox import OpenSandboxBackend, OpenSandboxBackendError
from tinkerfin_sandbox.backends._operations import RemoteOperations


class _CommandStream(httpx.AsyncByteStream):
    """Keep remote execution independent from the lifetime of the SSE consumer."""

    def __init__(self, server: _CommandServer) -> None:
        self.server = server

    async def __aiter__(self) -> AsyncGenerator[bytes]:
        if self.server.emit_init:
            yield b'data: {"type":"init","text":"command-1","timestamp":1}\n\n'
        self.server.started.set()
        await self.server.complete.wait()
        if self.server.fail_stream:
            raise httpx.ReadError("command stream ended unexpectedly")
        self.server.running = False
        yield b'data: {"type":"execution_complete","timestamp":2}\n\n'

    async def aclose(self) -> None:
        self.server.stream_closed.set()


class _CommandServer:
    """Expose command progress and transport observations without a live Sandbox."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.complete = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.emit_init = True
        self.fail_stream = False
        self.running = True
        self.command_status = 200
        self.status_response = 200
        self.status_calls = 0
        self.interrupt_calls = 0
        self.command_calls = 0

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/endpoints/" in path:
            return httpx.Response(200, json={"endpoint": "sandbox.invalid:44772"})
        if request.method == "POST" and path == "/command":
            self.command_calls += 1
            if self.command_status != 200:
                return httpx.Response(
                    self.command_status, json={"code": "REJECTED", "message": "refused"}
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=_CommandStream(self),
            )
        if request.method == "GET" and path == "/command/status/command-1":
            self.status_calls += 1
            if self.status_response != 200:
                return httpx.Response(
                    self.status_response,
                    json={"code": "UNAVAILABLE", "message": "status unavailable"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "command-1",
                    "running": self.running,
                    "exit_code": None if self.running else 143,
                },
            )
        if request.method == "DELETE" and path == "/command":
            self.interrupt_calls += 1
            self.interrupted.set()
            return httpx.Response(204)
        raise AssertionError(f"Unexpected SDK request: {request.method} {path}")


@asynccontextmanager
async def _backend(server: _CommandServer) -> AsyncGenerator[OpenSandboxBackend]:
    transport = httpx.MockTransport(server.respond)
    sandbox = await Sandbox.connect(
        "sandbox-1",
        connection_config=ConnectionConfig(
            domain="control.invalid",
            transport=transport,
            disable_metrics=True,
            retry_policy=RetryPolicy.disabled(),
        ),
        skip_health_check=True,
    )
    backend = OpenSandboxBackend(sandbox=sandbox)
    try:
        yield backend
    finally:
        server.running = False
        server.complete.set()
        await backend.aclose()
        await transport.aclose()


async def _cancel_command(
    backend: OpenSandboxBackend,
    server: _CommandServer,
    tracker: RemoteOperations | None,
) -> None:
    if tracker is None:
        command = asyncio.create_task(backend.aexecute("sleep 60"))
    else:
        with tracker.activate():
            command = asyncio.create_task(backend.aexecute("sleep 60"))
    await asyncio.wait_for(server.started.wait(), timeout=1)
    command.cancel("caller stopped waiting")
    with pytest.raises(asyncio.CancelledError, match="caller stopped waiting"):
        await asyncio.wait_for(command, timeout=0.25)


@pytest.mark.asyncio
async def test_cancelled_command_retains_remote_work_until_confirmed_terminal() -> None:
    server = _CommandServer()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        await _cancel_command(backend, server, tracker)
        await asyncio.wait_for(server.interrupted.wait(), timeout=1)
        assert server.running
        assert server.stream_closed.is_set()
        assert not tracker.is_idle

        server.running = False
        await asyncio.wait_for(tracker.wait(), timeout=1)
        assert tracker.is_idle
        assert server.command_calls == 1
        assert server.interrupt_calls == 1


@pytest.mark.asyncio
async def test_successful_command_needs_no_termination_requests() -> None:
    server = _CommandServer()
    server.complete.set()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        with tracker.activate():
            response = await backend.aexecute("printf done")
        assert response.exit_code == 0
        assert tracker.is_idle
        assert server.status_calls == 0
        assert server.interrupt_calls == 0


@pytest.mark.asyncio
async def test_broken_command_stream_retains_its_identified_remote_execution() -> None:
    server = _CommandServer()
    server.fail_stream = True
    server.complete.set()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        with tracker.activate(), pytest.raises(OpenSandboxBackendError):
            await backend.aexecute("sleep 60")
        await asyncio.wait_for(server.interrupted.wait(), timeout=1)
        assert not tracker.is_idle
        assert server.running
        server.running = False
        await asyncio.wait_for(tracker.wait(), timeout=1)
        assert tracker.is_idle
        assert server.command_calls == 1


@pytest.mark.asyncio
async def test_cancelled_command_without_an_id_preserves_uncertainty() -> None:
    server = _CommandServer()
    server.emit_init = False
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        await _cancel_command(backend, server, tracker)
        await tracker.wait()
        assert not tracker.is_idle
        assert server.status_calls == 0
        assert server.interrupt_calls == 0


@pytest.mark.asyncio
async def test_failed_remote_status_cannot_confirm_idle() -> None:
    server = _CommandServer()
    server.status_response = 503
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        await _cancel_command(backend, server, tracker)
        await asyncio.wait_for(tracker.wait(), timeout=1)
        assert not tracker.is_idle
        assert server.running


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "idle"), [(400, True), (403, True), (408, False)])
async def test_only_confirmed_request_rejection_is_idle(
    status: int, idle: bool
) -> None:
    server = _CommandServer()
    server.command_status = status
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        with tracker.activate(), pytest.raises(OpenSandboxBackendError):
            await backend.aexecute("sleep 60")
        assert tracker.is_idle is idle
        assert server.command_calls == 1
        assert server.status_calls == 0


@pytest.mark.asyncio
async def test_standalone_close_waits_for_owned_remote_checks() -> None:
    server = _CommandServer()
    before = set(asyncio.all_tasks())
    async with _backend(server) as backend:
        await _cancel_command(backend, server, None)
        await asyncio.wait_for(server.interrupted.wait(), timeout=1)
        closing = asyncio.create_task(backend.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        server.running = False
        await asyncio.wait_for(closing, timeout=1)
    await asyncio.sleep(0)
    assert not (set(asyncio.all_tasks()) - before)


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_cancel_owned_termination() -> None:
    server = _CommandServer()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        await _cancel_command(backend, server, tracker)
        await asyncio.wait_for(server.interrupted.wait(), timeout=1)
        waiting = asyncio.create_task(tracker.wait())
        await asyncio.sleep(0)
        waiting.cancel("stop waiting for settlement")
        with pytest.raises(asyncio.CancelledError, match="stop waiting"):
            await waiting
        assert not tracker.is_idle
        server.running = False
        await asyncio.wait_for(tracker.wait(), timeout=1)
        assert tracker.is_idle


@pytest.mark.asyncio
async def test_settlement_capacity_keeps_uncertainty_without_unbounded_tasks() -> None:
    tracker = RemoteOperations()
    release = asyncio.Event()
    started = 0

    async def settle() -> None:
        nonlocal started
        started += 1
        await release.wait()

    for _ in range(100):
        tracker.start_settlement(settle)
    await asyncio.sleep(0)
    assert started == 32
    release.set()
    await tracker.wait()
    assert not tracker.is_idle


class _FileServer(_CommandServer):
    """Report file request acceptance separately from its response outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.file_calls = 0
        self.file_status: int | None = None
        self.file_payload: bytes | None = None

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/files/upload", "/files/download"}:
            self.file_calls += 1
            self.file_payload = await request.aread()
            if self.file_status is not None:
                return httpx.Response(self.file_status, content=b"confirmed response")
            raise httpx.ReadTimeout("file response was lost", request=request)
        return await super().respond(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("upload", [False, True])
async def test_file_response_loss_is_unresolved_and_never_replayed(
    upload: bool,
) -> None:
    server = _FileServer()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        with tracker.activate():
            if upload:
                responses = await backend.aupload_files([("/content.bin", b"content")])
            else:
                responses = await backend.adownload_files(["/content.bin"])
        assert responses[0].error is not None
        assert not tracker.is_idle
        assert server.file_calls == 1


@pytest.mark.asyncio
async def test_cancelled_download_finishes_request_cleanup_before_returning() -> None:
    opened = asyncio.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()

    class FileStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncGenerator[bytes]:
            opened.set()
            await asyncio.Event().wait()
            yield b"content"

        async def aclose(self) -> None:
            closing.set()
            await release_close.wait()

    class FileServer(_CommandServer):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/files/download":
                return httpx.Response(200, stream=FileStream())
            return await super().respond(request)

    tracker = RemoteOperations()
    async with _backend(FileServer()) as backend:
        with tracker.activate():
            downloading = asyncio.create_task(backend.adownload_files(["/content.bin"]))
        await asyncio.wait_for(opened.wait(), timeout=1)
        downloading.cancel()
        await asyncio.wait_for(closing.wait(), timeout=1)
        assert not downloading.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(downloading, timeout=1)
        await tracker.wait()
        assert not tracker.is_idle


@pytest.mark.asyncio
async def test_rooted_transfer_failure_cannot_confirm_helper_termination() -> None:
    class RootedServer(_CommandServer):
        def __init__(self) -> None:
            super().__init__()
            self.token = ""
            self.uploads = 0

        async def respond(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/command":
                body = json.loads(await request.aread())
                helper = json.loads(base64.b64decode(shlex.split(body["command"])[-1]))
                self.token = helper["arguments"]["token"]
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=(
                        b'data: {"type":"init","text":"command-1","timestamp":1}\n\n'
                        b'data: {"type":"execution_complete","timestamp":2}\n\n'
                    ),
                )
            if path == "/command/command-1/logs":
                return httpx.Response(
                    200,
                    headers={"EXECD-COMMANDS-TAIL-CURSOR": "1"},
                    text=json.dumps(
                        {"token": self.token, "mode": "upload", "pid": 12, "fd": 9}
                    )
                    + "\n",
                )
            if path == "/files/upload":
                self.uploads += 1
                await request.aread()
                return httpx.Response(200)
            if request.method == "DELETE" and path == "/command":
                return httpx.Response(
                    503, json={"code": "UNAVAILABLE", "message": "cannot interrupt"}
                )
            return await super().respond(request)

    server = RootedServer()
    tracker = RemoteOperations()
    async with _backend(server) as backend:
        with tracker.activate(), pytest.raises(SandboxApiException):
            await backend._aupload_rooted_file(
                root="/workspace", path="/content.bin", content=b"content"
            )
        await tracker.wait()
        assert not tracker.is_idle
        assert server.uploads == 1
        assert server.running


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Pausing", "Paused", "Resuming"])
async def test_suspended_runtime_details_never_start_a_health_command(
    state: str,
) -> None:
    class RuntimeServer(_CommandServer):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/sandboxes/sandbox-1":
                return httpx.Response(
                    200,
                    json={
                        "id": "sandbox-1",
                        "status": {"state": state},
                        "createdAt": "2026-09-07T00:00:00Z",
                        "entrypoint": ["/entrypoint.sh"],
                    },
                )
            return await super().respond(request)

    server = RuntimeServer()
    async with _backend(server) as backend:
        info = await backend.aget_runtime_info()
        assert info.healthy is False
        assert info.status is not None
        assert info.status.state == state
        assert server.command_calls == 0
