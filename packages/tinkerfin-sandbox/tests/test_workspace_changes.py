"""Collector framing, authenticated routing, and read-only connection contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from opensandbox.config import ConnectionConfig
from test_backend import _FakeSandbox

from tinkerfin_sandbox import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxClient,
    OpenSandboxConfig,
)
from tinkerfin_sandbox.backends import _workspace_changes
from tinkerfin_sandbox.backends._isolated import _workspace_call
from tinkerfin_sandbox.backends._workspace_changes import (
    _open_changes,
    _WorkspaceEvent,
    _WorkspaceReady,
)


class _Body(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, block: bool = False) -> None:
        self.chunks = chunks
        self.block = block
        self.closed = asyncio.Event()
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


def _ready() -> bytes:
    return (
        json.dumps(
            {"type": "ready", "source": str(uuid4()), "incarnation": str(uuid4())}
        ).encode()
        + b"\n"
    )


async def _collect(client: httpx.AsyncClient) -> list[_WorkspaceEvent]:
    async with _open_changes(client, "a" * 64, request_timeout=30) as events:
        return [event async for event in events]


@pytest.mark.parametrize(
    "ending", [b'{"type":"closed"}\n', b'{"type":"resync","reason":"source_closed"}\n']
)
async def test_collector_stream_preserves_framing_and_closes_response(
    ending: bytes,
) -> None:
    ready = _ready()
    body = _Body(
        ready[:17], ready[17:] + b'{"type":"changed"}\n{"type":"heartbeat"}\n', ending
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "application/x-ndjson"}, stream=body
        )

    async with httpx.AsyncClient(
        base_url="https://execd.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        events = await _workspace_call(_collect(client))
    assert isinstance(events[0], _WorkspaceReady)
    assert [event.type for event in events[:3]] == ["ready", "changed", "heartbeat"]
    assert requests[0].url.path == f"/proxy/44773/projects/{'a' * 64}/changes"
    assert requests[0].extensions["timeout"]["read"] is None
    assert body.closed.is_set()


@pytest.mark.parametrize(
    "invalid",
    [
        b"x" * 4097,
        b'{"type":"changed"}',
        b'{"type":"unknown"}\n',
        b'{"type":"changed","path":"/secret"}\n',
        b'{"type":"resync","reason":"unknown"}\n',
        _ready(),
    ],
)
async def test_invalid_and_unbounded_events_fail_with_owned_cleanup(
    invalid: bytes,
) -> None:
    body = _Body(_ready(), invalid)
    async with httpx.AsyncClient(
        base_url="https://execd.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Type": "application/x-ndjson"}, stream=body
            )
        ),
    ) as client:
        with pytest.raises(OpenSandboxBackendProtocolError):
            await _workspace_call(_collect(client))
    assert body.closed.is_set()


@pytest.mark.parametrize("status", [404, 409, 503])
async def test_collector_admission_uses_safe_package_errors(status: int) -> None:
    body = _Body(b"private remote failure")
    async with httpx.AsyncClient(
        base_url="https://execd.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=body)),
    ) as client:
        with pytest.raises(
            OpenSandboxBusyError
            if status == 409
            else OpenSandboxBackendUnavailableError
        ) as caught:
            await _workspace_call(_collect(client))
    assert "private" not in str(caught.value)
    assert body.closed.is_set()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip"},
    ],
)
async def test_collector_requires_identity_ndjson(headers: dict[str, str]) -> None:
    body = _Body(_ready())
    async with httpx.AsyncClient(
        base_url="https://execd.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, stream=body)
        ),
    ) as client:
        with pytest.raises(OpenSandboxBackendProtocolError):
            await _workspace_call(_collect(client))
    assert body.closed.is_set()


async def test_heartbeat_deadline_is_controlled_and_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _Body(_ready(), block=True)
    deadline_entered = asyncio.Event()
    expire = asyncio.Event()

    async def expire_read(lines: AsyncIterator[bytes], *, timeout: float) -> bytes:
        assert timeout == 45
        deadline_entered.set()
        await expire.wait()
        raise TimeoutError("Controlled collector silence")

    monkeypatch.setattr(_workspace_changes, "_next_line", expire_read)
    async with httpx.AsyncClient(
        base_url="https://execd.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Type": "application/x-ndjson"}, stream=body
            )
        ),
    ) as client:
        task = asyncio.create_task(_workspace_call(_collect(client)))
        try:
            await deadline_entered.wait()
            expire.set()
            with pytest.raises(OpenSandboxBackendTimeoutError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert body.closed.is_set()


async def test_unready_collector_deadline_closes_the_pending_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: list[asyncio.Timeout] = []

    def controlled_deadline(_deadline: float) -> asyncio.Timeout:
        deadline = asyncio.timeout(None)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(
        _workspace_changes,
        "asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            timeout=asyncio.timeout,
            timeout_at=controlled_deadline,
        ),
    )
    body = _Body(block=True)
    async with httpx.AsyncClient(
        base_url="https://execd.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Type": "application/x-ndjson"}, stream=body
            )
        ),
    ) as client:
        task = asyncio.create_task(_workspace_call(_collect(client)))
        try:
            await body.waiting.wait()
            deadlines[-1].reschedule(0)
            with pytest.raises(OpenSandboxBackendTimeoutError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert body.closed.is_set()


async def test_observation_connection_skips_initializers_and_settles_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox("existing")
    sandbox.info.metadata = {"tinkerfin.ai/purpose": "workspaces"}
    initialize = AsyncMock()
    connected = asyncio.Event()
    release = asyncio.Event()

    async def connect(*_: object, **__: object) -> _FakeSandbox:
        connected.set()
        await release.wait()
        return sandbox

    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.Sandbox.connect", connect)
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
        initializers=[initialize],
    )
    task = asyncio.create_task(client._connect_observer("existing"))
    try:
        await connected.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        initialize.assert_not_awaited()
        assert not sandbox.commands.calls and not sandbox.files.created_directories
        assert sandbox.closed and not sandbox.killed
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
