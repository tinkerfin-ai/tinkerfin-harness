"""Fixed workspace HTTP ownership, file routing, and cancellation contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from deepagents.backends.protocol import INVALID_PATH
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint
from opensandbox.transport import RetryAsyncTransport, RetryPolicy
from pydantic import JsonValue, TypeAdapter

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxFileTooLargeError,
    OpenSandboxHandle,
    OpenSandboxHandleClosedError,
    OpenSandboxHandleOwnershipError,
)
from tinkerfin_sandbox.backends import _isolated, _rooted_protocol
from tinkerfin_sandbox.backends._isolated import _WorkspaceConnection
from tinkerfin_sandbox.backends._workspace_backend import _WorkspaceBackend

_JSON = TypeAdapter(JsonValue)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, blocked: bool = False) -> None:
        self.body = body
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


def _events(*events: dict[str, JsonValue], standard: bool = False) -> bytes:
    prefix = b"data: " if standard else b""
    return b"".join(prefix + json.dumps(event).encode() + b"\n\n" for event in events)


class _Native(httpx.AsyncBaseTransport):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.namespace = str(uuid4())
        self.session_id = str(uuid4())
        self.requests: list[httpx.Request] = []
        self.streams: list[_Stream] = []
        self.closed = False
        self.delete_started = asyncio.Event()
        self.delete_release = asyncio.Event()
        self.delete_release.set()
        self.run_stream: _Stream | None = None
        self.download_stream: _Stream | None = None
        self.create_status = 201
        self.delete_status = 200
        self.capability_overrides: dict[str, JsonValue] = {}
        self.deleted = False

    def response(self, status: int, body: bytes = b"") -> httpx.Response:
        stream = _Stream(body)
        self.streams.append(stream)
        return httpx.Response(status, stream=stream)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/isolated/capabilities":
            payload: dict[str, JsonValue] = {
                "available": True,
                "client_session_ownership": True,
                "namespace_exit_confirmation": True,
                "rooted_filesystem": True,
                "session_namespace": self.namespace,
                **self.capability_overrides,
            }
            return self.response(200, json.dumps(payload).encode())
        if path == "/v1/isolated/session" and request.method == "POST":
            return self.response(
                self.create_status, json.dumps({"session_id": self.session_id}).encode()
            )
        assert path.startswith(f"/v1/isolated/session/{self.session_id}")
        if request.method == "DELETE":
            assert request.url.params["session_namespace"] == self.namespace
            self.delete_started.set()
            await self.delete_release.wait()
            self.deleted = self.delete_status == 200
            return self.response(self.delete_status)
        if path.endswith("/run"):
            if self.run_stream is not None:
                self.streams.append(self.run_stream)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=self.run_stream,
                )
            payload = _JSON.validate_json(await request.aread())
            assert isinstance(payload, dict)
            command = payload["code"]
            assert isinstance(command, str) and command.startswith("cd /workspace && ")
            command = command.removeprefix("cd /workspace && ")
            if "invalid rooted helper request" in command:
                arguments = shlex.split(command)
                helper = _JSON.validate_json(base64.b64decode(arguments[-1]))
                assert isinstance(helper, dict)
                helper["root"] = str(self.root)
                values = helper["arguments"]
                assert isinstance(values, dict)
                for key in ("old_path", "new_path", "working_directory"):
                    value = values.get(key)
                    if isinstance(value, str):
                        values[key] = value.replace("/workspace", str(self.root), 1)
                encoded = base64.b64encode(json.dumps(helper).encode()).decode()
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    arguments[-2],
                    encoded,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.root,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.root,
                )
            try:
                stdout, stderr = await process.communicate()
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, stderr.decode()
            stream = _Stream(
                _events(
                    {
                        "type": "stdout",
                        "timestamp": 1,
                        "text": stdout.decode().rstrip("\n"),
                    },
                    {"type": "execution_complete", "timestamp": 1},
                )
            )
            self.streams.append(stream)
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=stream
            )
        if path.endswith("/files/upload"):
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["Content-Type"].encode()
                + b"\r\n\r\n"
                + await request.aread()
            )
            parts = list(message.iter_parts())
            metadata = _JSON.validate_json(parts[0].get_content())
            assert isinstance(metadata, dict) and isinstance(metadata["path"], str)
            relative = metadata["path"]
            assert not relative.startswith("/") and ".." not in Path(relative).parts
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = parts[1].get_content()
            assert isinstance(content, bytes)
            target.write_bytes(content)
            return self.response(200)
        if path.endswith("/files/download"):
            relative = request.url.params["path"]
            assert not relative.startswith("/") and ".." not in Path(relative).parts
            if self.download_stream is not None:
                self.streams.append(self.download_stream)
                return httpx.Response(200, stream=self.download_stream)
            target = self.root / relative
            if not target.exists():
                return self.response(404, b'{"code":"FILE_NOT_FOUND"}')
            return self.response(200, target.read_bytes())
        raise AssertionError(f"Unexpected parent or unsupported route: {path}")

    async def aclose(self) -> None:
        self.closed = True


async def _connected(
    native: _Native,
) -> tuple[_WorkspaceConnection, OpenSandboxBackend]:
    sandbox = Mock(spec=Sandbox)
    sandbox.id = "physical-parent"
    sandbox.connection_config = ConnectionConfig(
        headers={"X-Test-Authorization": "private-auth"},
        transport=RetryAsyncTransport(native, RetryPolicy()),
    )
    sandbox.get_endpoint = AsyncMock(
        return_value=SandboxEndpoint(
            endpoint="execd.example",
            headers={
                "X-Endpoint-Authorization": "private-endpoint",
                "X-EXECD-ACCESS-TOKEN": "private-execd",
            },
        )
    )
    sandbox.commands.run.side_effect = AssertionError("parent command route used")
    sandbox.files.write_file.side_effect = AssertionError("parent file route used")
    parent = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox), command_env={"PARENT_SECRET": "hidden"}
    )
    connection = await _WorkspaceConnection.connect(parent)
    await connection.capabilities()
    return connection, parent


def _backend(connection: _WorkspaceConnection, native: _Native) -> _WorkspaceBackend:
    return _WorkspaceBackend(
        connection,
        session_id=native.session_id,
        session_namespace=native.namespace,
        stop_run=lambda: connection.stop(native.session_id, native.namespace),
        environment={
            "HOME": "/home/workspace",
            "PATH": "/dependencies/python/bin:/usr/bin:/bin",
            "HTTPS_PROXY": "http://127.0.0.1:18080",
        },
        enable_capture_offload=True,
    )


@pytest.mark.parametrize(
    "missing",
    [
        "available",
        "client_session_ownership",
        "namespace_exit_confirmation",
        "rooted_filesystem",
    ],
)
async def test_capabilities_fail_closed(tmp_path: Path, missing: str) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    native.capability_overrides[missing] = False
    with pytest.raises(OpenSandboxBackendProtocolError) as failure:
        await connection.capabilities()
    assert failure.value.context["reason"] == "capabilities_missing"
    assert all(stream.closed for stream in native.streams)
    await connection.aclose()
    assert not native.closed


@pytest.mark.parametrize("headers", [{}, {"X-EXECD-ACCESS-TOKEN": ""}])
async def test_parent_authentication_is_required_before_opening_transport(
    tmp_path: Path, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _Native(tmp_path)
    connection, parent = await _connected(native)
    await connection.aclose()
    monkeypatch.setattr(
        parent._sandbox,
        "get_endpoint",
        AsyncMock(
            return_value=SandboxEndpoint(endpoint="execd.example", headers=headers)
        ),
    )
    with pytest.raises(OpenSandboxBackendProtocolError) as rejected:
        await _WorkspaceConnection.connect(parent)
    assert rejected.value.context["reason"] == "parent_authentication_missing"
    assert len(native.requests) == 1


async def test_known_identity_creation_and_namespace_fencing(tmp_path: Path) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    request: dict[str, JsonValue] = {
        "session_id": native.session_id,
        "session_namespace": native.namespace,
        "workspace": {"path": "/trusted/project/files", "mode": "rw"},
        "share_net": False,
    }
    await connection.start(request)
    submitted = _JSON.validate_json(await native.requests[-1].aread())
    assert isinstance(submitted, dict)
    assert submitted["session_id"] == native.session_id
    assert submitted["session_namespace"] == native.namespace
    await connection.stop(native.session_id, native.namespace)
    await connection.stop(native.session_id, native.namespace)
    native.delete_status = 409
    with pytest.raises(OpenSandboxBackendProtocolError):
        await connection.stop(native.session_id, native.namespace)
    native.capability_overrides["session_namespace"] = str(uuid4())
    with pytest.raises(OpenSandboxBackendProtocolError) as failure:
        await connection.capabilities()
    assert failure.value.context["reason"] == "namespace_changed"
    assert all(stream.closed for stream in native.streams)
    await connection.aclose()


async def test_native_requests_never_retry_or_expose_authentication(
    tmp_path: Path,
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    native.create_status = 503
    with pytest.raises(OpenSandboxBackendUnavailableError) as failure:
        await connection.start(
            {
                "session_id": native.session_id,
                "session_namespace": native.namespace,
                "workspace": {"path": "/trusted/files"},
            }
        )
    assert (
        len([request for request in native.requests if request.method == "POST"]) == 1
    )
    assert "private-auth" not in str(failure.value)
    assert "private-endpoint" not in str(failure.value.context)
    assert native.requests[-1].headers["X-Endpoint-Authorization"] == "private-endpoint"
    await connection.aclose()
    assert not native.closed


async def test_file_shell_capture_and_large_edit_share_fixed_session(
    tmp_path: Path,
) -> None:
    native = _Native(tmp_path)
    connection, parent = await _connected(native)
    handle = OpenSandboxHandle(parent)
    async with handle._alease():
        backend = _backend(connection, native)
        assert (await backend.awrite("/notes.txt", "before\nkeep\n")).error is None
        assert (await backend.aread("/notes.txt")).error is None
        assert (await backend.als("/")).entries
        assert (await backend.aglob("*.txt")).matches
        assert (await backend.agrep("keep")).matches
        assert (await backend.aedit("/notes.txt", "before", "after")).occurrences == 1
        large = "oversized payload " * 4000
        assert (await backend.aedit("/notes.txt", "after", large)).occurrences == 1
        assert (await backend.aread_bytes("/notes.txt", max_bytes=100_000)).startswith(
            large.encode()
        )
        assert (await backend.adownload_files(["/notes.txt"]))[0].content == (
            large + "\nkeep\n"
        ).encode()
        result = await backend.aexecute_with_offload(
            "printf 'captured output'", "/capture.txt", max_inline_bytes=2
        )
        assert result.offloaded
        assert (
            await backend.aread_bytes("/capture.txt", max_bytes=100)
        ) == b"captured output"
        assert (await backend.aexecute("printf shell")).output == "shell"
        assert (await backend.adelete("/notes.txt")).error is None
        assert not list(native.root.glob(".tinkerfin-rooted-edit-*"))
        assert backend.id == "physical-parent"
        for request in native.requests:
            if not request.url.path.endswith("/run"):
                continue
            payload = _JSON.validate_json(await request.aread())
            assert isinstance(payload, dict) and isinstance(payload["envs"], dict)
            assert "PARENT_SECRET" not in payload["envs"]
            assert payload["envs"]["HOME"] == "/home/workspace"
            assert payload["envs"]["HTTPS_PROXY"] == "http://127.0.0.1:18080"
        await backend.aclose()
    assert handle.is_closed is False
    assert all(stream.closed for stream in native.streams)
    await connection.aclose()
    assert native.closed is False


@pytest.mark.parametrize(
    "path", ["../sibling", "/../sibling", "//sibling", "/nul\0byte"]
)
async def test_invalid_paths_never_reach_native_files(
    tmp_path: Path, path: str
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    before = len(native.requests)
    assert (await backend.aupload_files([(path, b"unsafe")]))[0].error == INVALID_PATH
    assert (await backend.adownload_files([path]))[0].error == INVALID_PATH
    with pytest.raises(ValueError):
        await backend.aread_bytes(path, max_bytes=10)
    assert len(native.requests) == before
    await backend.aclose()
    await connection.aclose()


async def test_binary_limit_closes_response_without_returning_prefix(
    tmp_path: Path,
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    native.download_stream = _Stream(b"abc")
    with pytest.raises(OpenSandboxFileTooLargeError):
        await backend.aread_bytes("/file", max_bytes=2)
    assert native.download_stream.closed
    assert native.deleted
    await connection.aclose()


@pytest.mark.parametrize("operation", ["command", "read"])
async def test_cancellation_waits_for_native_exit_and_closes_response(
    tmp_path: Path, operation: str
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    stream = _Stream(b"", blocked=True)
    if operation == "command":
        native.run_stream = stream
        task = asyncio.create_task(backend.aexecute("blocked"))
    else:
        native.download_stream = stream
        task = asyncio.create_task(backend.aread_bytes("/file", max_bytes=20))
    native.delete_release.clear()
    await stream.started.wait()
    task.cancel()
    await native.delete_started.wait()
    assert stream.closed
    assert not task.done()
    task.cancel()
    native.delete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.is_closed
    with pytest.raises(OpenSandboxHandleClosedError):
        await backend.aexecute("never replayed")
    assert (
        len([request for request in native.requests if request.method == "DELETE"]) == 1
    )
    await backend.aclose()
    await connection.aclose()


@pytest.mark.parametrize("terminal", ["incomplete", "timeout", "exit", "complete"])
async def test_command_terminal_contract(tmp_path: Path, terminal: str) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    payload: list[dict[str, JsonValue]] = [
        {"type": "stdout", "timestamp": 1, "text": "result"}
    ]
    if terminal == "timeout":
        payload.append(
            {
                "type": "error",
                "timestamp": 1,
                "error": {
                    "ename": "RuntimeError",
                    "evalue": "context deadline exceeded",
                },
            }
        )
    elif terminal == "exit":
        payload.append(
            {
                "type": "error",
                "timestamp": 1,
                "error": {"ename": "ExitError", "evalue": "7"},
            }
        )
    elif terminal == "complete":
        payload.append({"type": "execution_complete", "timestamp": 1})
    native.run_stream = _Stream(_events(*payload, standard=True))
    if terminal in {"incomplete", "timeout"}:
        with pytest.raises(
            (OpenSandboxBackendProtocolError, OpenSandboxBackendTimeoutError)
        ):
            await backend.aexecute("one attempt")
        assert native.deleted
    else:
        result = await backend.aexecute("one attempt")
        assert result.exit_code == (7 if terminal == "exit" else 0)
        assert result.output == "result"
        assert not native.deleted
    assert native.run_stream.closed
    await backend.aclose()
    await connection.aclose()


async def test_commands_are_serialized_and_close_drains_active_call(
    tmp_path: Path,
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    stream = _Stream(
        _events({"type": "execution_complete", "timestamp": 1}), blocked=True
    )
    native.run_stream = stream
    first = asyncio.create_task(backend.aexecute("first"))
    await stream.started.wait()
    queued = asyncio.Event()

    async def second() -> None:
        queued.set()
        await backend.aexecute("second")

    next_call = asyncio.create_task(second())
    await queued.wait()
    assert (
        len(
            [
                request
                for request in native.requests
                if request.url.path.endswith("/run")
            ]
        )
        == 1
    )
    await backend.aclose()
    results = await asyncio.gather(first, next_call, return_exceptions=True)
    assert all(isinstance(result, BaseException) for result in results)
    assert stream.closed
    assert (
        len(
            [
                request
                for request in native.requests
                if request.url.path.endswith("/run")
            ]
        )
        == 1
    )
    await connection.aclose()


async def test_parent_management_is_never_forwarded(tmp_path: Path) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    with pytest.raises(OpenSandboxHandleOwnershipError):
        await backend.arenew(timedelta(seconds=30))
    with pytest.raises(OpenSandboxHandleOwnershipError):
        await backend.aget_runtime_info()
    with pytest.raises(OpenSandboxHandleOwnershipError):
        backend.close()
    with pytest.raises(OpenSandboxBackendError, match="asynchronous remote I/O only"):
        backend.execute("printf never")
    await backend.aclose()
    await connection.aclose()
    assert not native.closed


async def test_event_size_limit_is_enforced_before_sdk_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    monkeypatch.setattr(_isolated, "_EVENT_LIMIT", 32)
    native.run_stream = _Stream(b"data: " + b"x" * 33)
    with pytest.raises(OpenSandboxBackendProtocolError):
        await backend.aexecute("overlong event")
    assert native.deleted
    assert native.run_stream.closed
    await connection.aclose()


async def test_edit_helper_rejects_unowned_staging_paths(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    victim = root / "victim"
    victim.write_text("must survive")
    target = root / "target"
    target.write_text("original")
    request = _rooted_protocol._build_rooted_command(
        root=str(root),
        operation="edit",
        arguments={
            "path": "/target",
            "old_path": str(victim),
            "new_path": str(victim),
            "replace_all": False,
        },
    )
    process = await asyncio.create_subprocess_exec(
        *shlex.split(request.command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 1
    assert b"invalid rooted helper request" in stderr
    assert not stdout
    assert victim.read_text() == "must survive"
    assert target.read_text() == "original"


@pytest.mark.parametrize("value", [1, "true", None])
async def test_capabilities_require_boolean_guarantees(
    tmp_path: Path, value: JsonValue
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    native.capability_overrides["rooted_filesystem"] = value
    with pytest.raises(OpenSandboxBackendProtocolError):
        await connection.capabilities()
    await connection.aclose()


async def test_transport_timeout_ends_run_without_replay(tmp_path: Path) -> None:
    class TimedOutNative(_Native):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/run"):
                self.requests.append(request)
                raise httpx.ReadTimeout("provider detail private-auth", request=request)
            return await super().handle_async_request(request)

    native = TimedOutNative(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    with pytest.raises(OpenSandboxBackendTimeoutError) as failure:
        await backend.aexecute("single attempt")
    assert "private-auth" not in str(failure.value)
    assert native.deleted
    assert backend.is_closed
    assert (
        len(
            [
                request
                for request in native.requests
                if request.url.path.endswith("/run")
            ]
        )
        == 1
    )
    await backend.aclose()
    await connection.aclose()


async def test_cancelled_upload_waits_for_late_remote_write_settlement(
    tmp_path: Path,
) -> None:
    class LateNative(_Native):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.upload_started = asyncio.Event()
            self.finish_upload = asyncio.Event()
            self.remote_upload: asyncio.Task[None] | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/files/upload"):
                self.requests.append(request)

                async def write() -> None:
                    self.upload_started.set()
                    await self.finish_upload.wait()
                    (self.root / "late-file").write_bytes(b"completed before DELETE")

                self.remote_upload = asyncio.create_task(write())
                await asyncio.shield(self.remote_upload)
                return self.response(200)
            if request.method == "DELETE":
                self.delete_started.set()
                await self.delete_release.wait()
                self.finish_upload.set()
                assert self.remote_upload is not None
                await self.remote_upload
            return await super().handle_async_request(request)

    native = LateNative(tmp_path)
    native.delete_release.clear()
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    upload = asyncio.create_task(backend.aupload_files([("/late-file", b"content")]))
    try:
        await native.upload_started.wait()
        upload.cancel()
        await native.delete_started.wait()
        assert not upload.done()
        assert not (native.root / "late-file").exists()
        native.delete_release.set()
        with pytest.raises(asyncio.CancelledError):
            await upload
        assert native.deleted
        assert native.remote_upload is not None and native.remote_upload.done()
        assert (native.root / "late-file").read_bytes() == b"completed before DELETE"
        (native.root / "late-file").unlink()
        await backend.aclose()
        assert not (native.root / "late-file").exists()
    finally:
        native.finish_upload.set()
        if native.remote_upload is not None:
            await native.remote_upload
        await connection.aclose()


async def test_failed_termination_remains_closed_and_is_never_reported_as_success(
    tmp_path: Path,
) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    native.delete_status = 503
    with pytest.raises(OpenSandboxBackendUnavailableError):
        await backend.aclose()
    assert backend.is_closed
    assert not native.deleted
    with pytest.raises(OpenSandboxHandleClosedError):
        await backend.awrite("/cannot-write", "data")
    with pytest.raises(OpenSandboxBackendUnavailableError):
        await backend.aclose()
    assert (
        len([request for request in native.requests if request.method == "DELETE"]) == 1
    )
    await connection.aclose()


async def test_failed_native_close_still_drains_local_responses(tmp_path: Path) -> None:
    native = _Native(tmp_path)
    connection, _ = await _connected(native)
    backend = _backend(connection, native)
    native.run_stream = _Stream(b"", blocked=True)
    native.delete_status = 503
    command = asyncio.create_task(backend.aexecute("blocked"))
    await native.run_stream.started.wait()
    with pytest.raises(OpenSandboxBackendUnavailableError):
        await backend.aclose()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert native.run_stream.closed
    assert not native.deleted
    await connection.aclose()
