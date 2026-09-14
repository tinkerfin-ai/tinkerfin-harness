"""Verify lifecycle ownership through the installed native OpenSandbox SDK."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from datetime import timedelta
from http import HTTPStatus
from typing import Literal

import httpx
import pytest
from opensandbox.config import ConnectionConfig
from opensandbox.transport import RetryAsyncTransport, RetryPolicy

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxRuntimeInfo,
)


class _ServiceTransport(httpx.AsyncBaseTransport):
    """Serve the SDK's actual HTTP contract without contacting a Sandbox."""

    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.closed = False
        self.mutation_status = 202
        self.mutation_started = asyncio.Event()
        self.mutation_release = asyncio.Event()
        self.mutation_release.set()
        self.mutation_cancelled = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[request.method, path] += 1
        if path.endswith(("/pause", "/resume")):
            self.mutation_started.set()
            try:
                await self.mutation_release.wait()
            except asyncio.CancelledError:
                self.mutation_cancelled = True
                raise
            return httpx.Response(
                self.mutation_status,
                json={"code": "SYNTHETIC", "message": "Synthetic response"},
            )
        if "/endpoints/" in path:
            return httpx.Response(200, json={"endpoint": "audit.invalid:9999"})
        if request.method == "DELETE":
            return httpx.Response(404, json={"code": "MISSING", "message": "Missing"})
        if path == "/v1/sandboxes" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "pagination": {
                        "page": 1,
                        "pageSize": 2,
                        "totalItems": 0,
                        "totalPages": 0,
                        "hasNextPage": False,
                    },
                },
            )
        if path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(202, json=_sandbox_info("bad"))
        if "/diagnostics/" in path:
            kind = "logs" if path.endswith("/logs") else "events"
            return httpx.Response(
                200,
                json={
                    "sandboxId": "existing",
                    "kind": kind,
                    "scope": request.url.params.get("scope"),
                    "delivery": "inline",
                    "contentType": "text/plain",
                    "truncated": False,
                    "content": "Synthetic diagnostic content",
                    "warnings": None,
                },
            )
        if path == "/v1/sandboxes/existing":
            return httpx.Response(200, json=_sandbox_info("existing"))
        if path == "/command":
            return httpx.Response(503, json={"code": "BUSY", "message": "Busy"})
        return httpx.Response(200, json={})

    async def aclose(self) -> None:
        self.closed = True


def _sandbox_info(sandbox_id: str) -> dict[str, object]:
    return {
        "id": sandbox_id,
        "status": {"state": "Running"},
        "createdAt": "2026-09-07T00:00:00Z",
        "entrypoint": ["sleep", "infinity"],
    }


class _BlockedEndpointBody(httpx.AsyncByteStream):
    """Keep the response body active after transport headers have returned."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.finished = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        try:
            await self.release.wait()
            yield b'{"endpoint":"audit.invalid:9999"}'
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.finished.set()


class _EndpointFaultTransport(_ServiceTransport):
    """Fail one endpoint only after its sibling has begun reading a body."""

    def __init__(self) -> None:
        super().__init__()
        self.body = _BlockedEndpointBody()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/bad/endpoints/" not in path:
            return await super().handle_async_request(request)
        self.calls[request.method, path] += 1
        if path.endswith("/44772"):
            await self.body.started.wait()
            raise httpx.ConnectError("Synthetic endpoint failure", request=request)
        return httpx.Response(200, stream=self.body)


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    request_timeout: timedelta = timedelta(seconds=5),
) -> OpenSandboxClient:
    return OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain="audit.invalid", transport=transport, request_timeout=request_timeout
        ),
        config=OpenSandboxConfig(workspace_root=None),
    )


@pytest.mark.parametrize("operation", ["connect", "create", "inspect", "destroy"])
async def test_failed_sdk_endpoint_discovery_settles_response_body(
    operation: Literal["connect", "create", "inspect", "destroy"],
) -> None:
    transport = _EndpointFaultTransport()
    client = _client(transport)
    try:
        if operation == "inspect":
            result = await client.inspect("bad")
            assert result.available is False
        else:
            with pytest.raises(OpenSandboxBackendError):
                if operation == "create":
                    await client.create()
                elif operation == "connect":
                    await client.connect("bad")
                else:
                    await client.destroy("bad")
        assert transport.body.finished.is_set()
        assert transport.calls["POST", "/v1/sandboxes"] <= 1
    finally:
        await client.aclose()
    assert transport.closed is False


async def test_repeated_cancel_and_close_wait_for_sdk_sibling_body_cleanup() -> None:
    transport = _EndpointFaultTransport()
    transport.body.cleanup_release.clear()
    client = _client(transport)
    connecting = asyncio.create_task(client.connect("bad"))
    await asyncio.wait_for(transport.body.cleanup_started.wait(), timeout=1)
    connecting.cancel()
    await asyncio.sleep(0)
    connecting.cancel()
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    assert not connecting.done()
    assert not closing.done()
    assert not transport.body.finished.is_set()
    transport.body.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(connecting, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    assert transport.body.finished.is_set()
    assert transport.closed is False


async def test_sdk_failure_scope_does_not_cancel_another_connection() -> None:
    transport = _EndpointFaultTransport()
    transport.body.cleanup_release.clear()
    client = _client(transport)
    failing = asyncio.create_task(client.connect("bad"))
    try:
        await asyncio.wait_for(transport.body.cleanup_started.wait(), timeout=1)
        backend = await asyncio.wait_for(client.connect("good"), timeout=1)
        assert backend.id == "good"
        await backend.aclose()
        assert not failing.done()
        transport.body.cleanup_release.set()
        with pytest.raises(OpenSandboxBackendError):
            await failing
    finally:
        transport.body.cleanup_release.set()
        await client.aclose()
    assert transport.closed is False


async def test_explicit_borrowed_retry_keeps_sse_commands_single_dispatch() -> None:
    transport = _ServiceTransport()
    policy = RetryPolicy(
        max_retries=2,
        initial_backoff=timedelta(0),
        max_backoff=timedelta(0),
        retryable_status_codes_non_idempotent=frozenset(
            {HTTPStatus.SERVICE_UNAVAILABLE}
        ),
    )
    borrowed_retry = RetryAsyncTransport(transport, policy, owns_inner=False)
    client = _client(borrowed_retry)
    try:
        backend = await client.connect("existing")
        with pytest.raises(OpenSandboxBackendError):
            await backend.aexecute("printf synthetic")
        await backend.aclose()
        assert transport.calls["POST", "/command"] == 1
    finally:
        await client.aclose()
    assert transport.closed is False


@pytest.mark.parametrize("explicit_retries", [None, 1])
async def test_owned_transport_disables_only_implicit_retry_defaults(
    explicit_retries: int | None,
) -> None:
    """Observe real HTTP attempts through the SDK-created transport."""
    calls: Counter[str] = Counter()
    connections: set[asyncio.Task[object]] = set()
    endpoint = ""

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task: asyncio.Task[object] | None = asyncio.current_task()
        assert task is not None
        connections.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", maxsplit=2)[1].decode().split("?", maxsplit=1)[0]
            calls[path] += 1
            status = 200
            payload: dict[str, object] = {}
            if "/endpoints/" in path:
                if calls[path] == 1:
                    status = 503
                    payload = {"code": "BUSY", "message": "Synthetic busy response"}
                else:
                    payload = {"endpoint": endpoint}
            body = json.dumps(payload).encode()
            writer.write(
                f"HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            # A sibling endpoint may be cancelled before it finishes its request.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            connections.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    endpoint = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    connection = (
        ConnectionConfig(domain=endpoint, request_timeout=timedelta(seconds=1))
        if explicit_retries is None
        else ConnectionConfig(
            domain=endpoint,
            request_timeout=timedelta(seconds=1),
            retry_policy=RetryPolicy(
                max_retries=explicit_retries,
                initial_backoff=timedelta(0),
                max_backoff=timedelta(0),
            ),
        )
    )
    client = OpenSandboxClient(
        connection_config=connection, config=OpenSandboxConfig(workspace_root=None)
    )
    try:
        if explicit_retries is None:
            with pytest.raises(OpenSandboxBackendError):
                await client.connect("existing")
            assert calls and max(calls.values()) == 1
        else:
            backend = await client.connect("existing")
            await backend.aclose()
            assert calls["/v1/sandboxes/existing/endpoints/44772"] == 2
            assert calls["/v1/sandboxes/existing/endpoints/18080"] == 2
        assert client.connection_config is connection
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(connections), return_exceptions=True)


@pytest.mark.parametrize(
    "status", [HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE]
)
def test_explicit_sdk_policy_cannot_replay_creation_or_write_responses(
    status: HTTPStatus,
) -> None:
    transport = _ServiceTransport()
    config = ConnectionConfig(
        transport=transport,
        retry_policy=RetryPolicy(
            retryable_status_codes_non_idempotent=frozenset({status})
        ),
    )
    with pytest.raises(ValueError, match="POST/PATCH"):
        OpenSandboxClient(connection_config=config)
    assert not transport.calls
    assert transport.closed is False


async def test_create_disables_unowned_telemetry_and_preserves_caller_headers() -> None:
    transport = _ServiceTransport()
    connection = ConnectionConfig(
        domain="audit.invalid",
        transport=transport,
        headers={"x-audit": "unchanged"},
        disable_metrics=False,
    )
    before_headers = dict(connection.headers)
    before_tasks = asyncio.all_tasks()
    client = OpenSandboxClient(
        connection_config=connection,
        config=OpenSandboxConfig(workspace_root=None),
    )
    try:
        backend = await client.create()
        await backend.aclose()
    finally:
        await client.aclose()
    await asyncio.sleep(0)
    pending = asyncio.all_tasks() - before_tasks
    try:
        assert not pending, "Client close must leave no SDK telemetry or endpoint tasks"
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert connection.headers == before_headers
    assert connection.disable_metrics is False
    assert connection.transport is transport
    assert transport.closed is False


async def test_sdk_scope_does_not_take_ownership_of_initializer_background_work() -> (
    None
):
    started = asyncio.Event()
    release = asyncio.Event()
    service = _ServiceTransport()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes/bad":
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={**_sandbox_info("bad"), "status": {"state": "Terminated"}},
            )
        return await service.handle_async_request(request)

    caller_tasks: list[asyncio.Task[OpenSandboxRuntimeInfo]] = []

    async def initialize(backend: OpenSandboxBackend) -> None:
        caller_tasks.append(asyncio.create_task(backend.aget_runtime_info()))
        await started.wait()

    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain="audit.invalid", transport=httpx.MockTransport(respond)
        ),
        config=OpenSandboxConfig(workspace_root=None),
        initializers=(initialize,),
    )
    try:
        backend = await client.create()
        assert len(caller_tasks) == 1
        assert not caller_tasks[0].done()
        release.set()
        info = await caller_tasks[0]
        assert info.sandbox_id == "bad"
        await backend.aclose()
    finally:
        release.set()
        await asyncio.gather(*caller_tasks, return_exceptions=True)
        await client.aclose()


async def test_close_joins_reclamation_registered_after_shutdown_started() -> None:
    service = _ServiceTransport()
    creating_started = asyncio.Event()
    creating_release = asyncio.Event()
    reclaim_started = asyncio.Event()
    reclaim_release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            creating_started.set()
            await creating_release.wait()
        if request.method == "DELETE":
            reclaim_started.set()
            await reclaim_release.wait()
            return httpx.Response(204)
        return await service.handle_async_request(request)

    client = _client(httpx.MockTransport(respond))
    creating = asyncio.create_task(client.create())
    await asyncio.wait_for(creating_started.wait(), timeout=1)
    closing = asyncio.create_task(client.aclose())
    # Let the public close call and its retained close task begin waiting while
    # creation is still owned by the caller rather than cancelled-create cleanup.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    creating.cancel()
    await asyncio.sleep(0)
    creating_release.set()
    try:
        await asyncio.wait_for(reclaim_started.wait(), timeout=1)
        creating.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        reclaim_release.set()
        with pytest.raises(asyncio.CancelledError):
            await creating
        await closing


async def test_destroy_accepts_confirmed_absence_after_issued_delete() -> None:
    transport = _ServiceTransport()
    client = _client(transport)
    try:
        await client.destroy("existing")
        assert transport.calls["DELETE", "/v1/sandboxes/existing"] == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("operation", ["pause", "resume"])
async def test_lifecycle_control_requests_wait_for_settlement_after_repeated_cancel(
    operation: Literal["pause", "resume"],
) -> None:
    transport = _ServiceTransport()
    transport.mutation_release.clear()
    client = _client(transport)
    changing = asyncio.create_task(
        client.pause("existing") if operation == "pause" else client.resume("existing")
    )
    await asyncio.wait_for(transport.mutation_started.wait(), timeout=1)
    changing.cancel()
    await asyncio.sleep(0)
    changing.cancel()
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    assert not changing.done()
    assert not closing.done()
    assert transport.mutation_cancelled is False
    transport.mutation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await changing
    await closing
    assert transport.calls == Counter(
        {("POST", f"/v1/sandboxes/existing/{operation}"): 1}
    )
    assert transport.closed is False


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
        (409, "rejected"),
        (408, "unknown"),
        (503, "unknown"),
    ],
)
async def test_lifecycle_control_preserves_known_rejection_and_unknown_outcome(
    status: int, outcome: str
) -> None:
    transport = _ServiceTransport()
    transport.mutation_status = status
    client = _client(transport)
    try:
        with pytest.raises(OpenSandboxBackendError) as failure:
            await client.pause("existing")
        assert failure.value.context["request_outcome"] == outcome
        assert failure.value.context["status_code"] == status
        assert failure.value.cause is failure.value.__cause__
        assert transport.calls["POST", "/v1/sandboxes/existing/pause"] == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("kind", ["logs", "events"])
async def test_diagnostics_use_only_control_plane_and_normalize_warnings(
    kind: Literal["logs", "events"],
) -> None:
    transport = _ServiceTransport()
    client = _client(transport)
    try:
        result = (
            await client.get_diagnostic_logs("existing")
            if kind == "logs"
            else await client.get_diagnostic_events("existing")
        )
        assert result.kind == kind
        assert result.scope == ("container" if kind == "logs" else "runtime")
        assert result.content == "Synthetic diagnostic content"
        assert result.warnings == ()
        assert transport.calls == Counter(
            {("GET", f"/v1/sandboxes/existing/diagnostics/{kind}"): 1}
        )
    finally:
        await client.aclose()


async def test_control_plane_info_never_reconnects_or_probes_health() -> None:
    transport = _ServiceTransport()
    client = _client(transport)
    try:
        info = await client.get_runtime_info("existing")
        assert info.available is True
        assert info.healthy is False
        assert info.status is not None and info.status.state == "Running"
        assert transport.calls == Counter({("GET", "/v1/sandboxes/existing"): 1})
    finally:
        await client.aclose()


@pytest.mark.parametrize("operation", ["connect", "create"])
async def test_cancelled_native_open_owns_late_initializer_failure(
    operation: Literal["connect", "create"],
) -> None:
    """Late failure is reclaimed without invoking the loop's exception handler."""
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _ServiceTransport()
    unhandled: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def initialize(backend: OpenSandboxBackend) -> None:
        assert backend.id
        entered.set()
        await release.wait()
        raise RuntimeError("private initializer failure")

    client = OpenSandboxClient(
        connection_config=ConnectionConfig(domain="audit.invalid", transport=transport),
        config=OpenSandboxConfig(workspace_root=None),
        initializers=[initialize],
    )
    try:
        task = asyncio.create_task(
            client.connect("existing") if operation == "connect" else client.create()
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
        await asyncio.sleep(0)
        assert unhandled == []
        assert not transport.closed
        assert transport.calls["DELETE", "/v1/sandboxes/existing"] == 0
        if operation == "create":
            assert transport.calls["DELETE", "/v1/sandboxes/bad"] == 1
    finally:
        release.set()
        await client.aclose()
        loop.set_exception_handler(previous)
