"""Bound workspace readiness through the installed SDK without external resources."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import timedelta
from types import ModuleType, TracebackType
from typing import Literal

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from test_sdk_upgrade import _sandbox_info, _ServiceTransport

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxClient,
    OpenSandboxConfig,
)
from tinkerfin_sandbox.lifecycle import client as client_module


class _Deadline:
    """Expire only after the test explicitly signals this exact deadline."""

    def __init__(self, clock: _Clock, when: float) -> None:
        self.clock = clock
        self.when = when
        self.owner: asyncio.Task[object] | None = None
        self.cancel_count = 0
        self.triggered = False

    async def __aenter__(self) -> _Deadline:
        self.owner = asyncio.current_task()
        assert self.owner is not None
        self.cancel_count = self.owner.cancelling()
        self.clock.deadlines.append(self)
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del error_type, traceback
        if self.triggered and isinstance(error, asyncio.CancelledError):
            assert self.owner is not None
            if self.owner.uncancel() <= self.cancel_count:
                raise TimeoutError("Explicit readiness deadline") from error
        return False

    def expired(self) -> bool:
        return self.triggered

    def expire(self) -> None:
        assert self.owner is not None
        self.clock.now = self.when
        self.triggered = True
        self.owner.cancel()


class _Clock:
    """Drive deadlines and readiness intervals without a wall-clock timer."""

    def __init__(self) -> None:
        self.now = 100.0
        self.deadlines: list[_Deadline] = []
        self.delays: list[float] = []
        self.poll_started = asyncio.Event()
        self.poll_release = asyncio.Event()
        self.poll_release.set()

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.poll_started.set()
        await self.poll_release.wait()
        self.now += seconds

    def module(self) -> ModuleType:
        controlled = ModuleType("controlled_readiness_asyncio")
        controlled.__dict__.update(vars(asyncio))
        setattr(controlled, "get_running_loop", lambda: self)
        setattr(controlled, "timeout_at", lambda when: _Deadline(self, when))
        setattr(controlled, "sleep", self.sleep)
        return controlled


class _ReadinessBody(httpx.AsyncByteStream):
    """Expose body cancellation and close independently of response headers."""

    def __init__(self, service: _WorkspaceService, payload: str) -> None:
        self.service = service
        self.payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.service.body_started.set()
        if self.service.block == "body":
            await self.service.release.wait()
        if self.service.command_error:
            yield (
                b'data: {"type":"error","timestamp":1,"error":{"ename":"RuntimeError",'
                b'"evalue":"Synthetic command failure"}}\n\n'
            )
        for payload in (
            {"type": "init", "text": "readiness-command", "timestamp": 1},
            {"type": "stdout", "text": self.payload, "timestamp": 2},
            {"type": "execution_complete", "timestamp": 3},
        ):
            yield ("data: " + json.dumps(payload) + "\n\n").encode()

    async def aclose(self) -> None:
        self.service.body_closed.set()


class _WorkspaceService(_ServiceTransport):
    """Serve exact SDK lifecycle endpoints and a controllable readiness command."""

    def __init__(self, block: Literal["headers", "body", "none"] = "none") -> None:
        super().__init__()
        self.block = block
        self.payload = '{"ready":true}'
        self.command_started = asyncio.Event()
        self.body_started = asyncio.Event()
        self.body_closed = asyncio.Event()
        self.headers_cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.metadata = {"tinkerfin.ai/purpose": "workspaces"}
        self.statuses: deque[int] = deque()
        self.failures: deque[Exception] = deque()
        self.payloads: deque[str] = deque()
        self.responses: list[httpx.Response] = []
        self.create_started = asyncio.Event()
        self.create_release = asyncio.Event()
        self.create_release.set()
        self.create_cancelled = False
        self.create_failure: Exception | None = None
        self.clock: _Clock | None = None
        self.endpoint_cost = 0.0
        self.info_cost = 0.0
        self.delete_status = 204
        self.command_error = False
        self.delete_started = asyncio.Event()
        self.delete_release = asyncio.Event()
        self.delete_release.set()
        self.delete_cancelled = False
        self.discovered_ids: tuple[str, ...] = ("owned-sandbox",)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/sandboxes":
            self.calls[request.method, path] += 1
            body = json.loads(request.content)
            self.metadata = body["metadata"]
            self.create_started.set()
            try:
                await self.create_release.wait()
            except asyncio.CancelledError:
                self.create_cancelled = True
                raise
            if self.create_failure is not None:
                raise self.create_failure
            return httpx.Response(
                202,
                json={**_sandbox_info("owned-sandbox"), "metadata": self.metadata},
            )
        if request.method == "DELETE":
            self.calls[request.method, path] += 1
            self.delete_started.set()
            try:
                await self.delete_release.wait()
            except asyncio.CancelledError:
                self.delete_cancelled = True
                raise
            return httpx.Response(
                self.delete_status,
                json={"code": "SYNTHETIC", "message": "Synthetic delete response"},
            )
        if path == "/v1/sandboxes" and request.method == "GET":
            self.calls[request.method, path] += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {**_sandbox_info(sandbox_id), "metadata": self.metadata}
                        for sandbox_id in self.discovered_ids
                    ],
                    "pagination": {
                        "page": 1,
                        "pageSize": 2,
                        "totalItems": len(self.discovered_ids),
                        "totalPages": 1,
                        "hasNextPage": False,
                    },
                },
            )
        if "/endpoints/" in path and path.endswith("/44772") and self.clock is not None:
            self.clock.now += self.endpoint_cost
        if path.startswith("/v1/sandboxes/") and path.count("/") == 3:
            self.calls[request.method, path] += 1
            if self.clock is not None:
                self.clock.now += self.info_cost
            return httpx.Response(
                200,
                json={
                    **_sandbox_info(path.rsplit("/", 1)[1]),
                    "metadata": self.metadata,
                },
            )
        if path == "/command":
            self.calls[request.method, path] += 1
            self.command_started.set()
            if self.failures:
                raise self.failures.popleft()
            status = self.statuses.popleft() if self.statuses else 200
            if status != 200:
                response = httpx.Response(
                    status,
                    json={"code": "REJECTED", "message": "synthetic-private-detail"},
                )
                self.responses.append(response)
                return response
            if self.block == "headers":
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.headers_cancelled.set()
                    raise
            response = httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=_ReadinessBody(
                    self, self.payloads.popleft() if self.payloads else self.payload
                ),
            )
            self.responses.append(response)
            return response
        return await super().handle_async_request(request)


def _client(
    service: _WorkspaceService | None,
    *,
    ready_seconds: float = 4,
    connect_seconds: float = 5,
) -> OpenSandboxClient:
    return OpenSandboxClient(
        connection_config=ConnectionConfig(domain="audit.invalid", transport=service),
        config=OpenSandboxConfig(
            workspace_root=None,
            ready_timeout=timedelta(seconds=ready_seconds),
            connect_timeout=timedelta(seconds=connect_seconds),
            warm_pool_size=0,
        ),
    )


@pytest.fixture
def sdk_closes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    closed: list[str] = []
    original_close = Sandbox.close

    async def close(sandbox: Sandbox) -> None:
        closed.append(sandbox.id)
        await original_close(sandbox)

    monkeypatch.setattr(Sandbox, "close", close)
    return closed


@pytest.mark.parametrize("blocked_phase", ["headers", "body"])
async def test_workspace_ready_deadline_covers_pending_headers_and_body(
    monkeypatch: pytest.MonkeyPatch,
    blocked_phase: Literal["headers", "body"],
) -> None:
    service = _WorkspaceService(blocked_phase)
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating: asyncio.Task[OpenSandboxBackend] = asyncio.create_task(
        client.create(purpose="workspaces")
    )
    try:
        if blocked_phase == "headers":
            await service.command_started.wait()
        else:
            await service.body_started.wait()
        assert clock.deadlines, "Readiness must own a client-side deadline"
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxBackendTimeoutError):
            await creating
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 0
        assert (
            service.headers_cancelled.is_set()
            if blocked_phase == "headers"
            else service.body_closed.is_set()
        )
    finally:
        service.release.set()
        if not creating.done():
            creating.cancel()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()
    assert not service.closed


@pytest.mark.parametrize(
    ("status", "reason"), [(401, "authentication"), (403, "permission")]
)
async def test_workspace_authentication_failure_is_immediate_and_never_rediscovered(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str], status: int, reason: str
) -> None:
    service = _WorkspaceService()
    service.statuses.append(status)
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await client.create(purpose="workspaces")
        assert captured.value.context["reason"] == reason
        assert "synthetic-private-detail" not in captured.value.message
        assert service.calls["POST", "/command"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 0
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert sdk_closes == ["owned-sandbox"]
        assert clock.delays == []
        assert all(response.is_closed for response in service.responses)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload", ["not-json", '{"ready":1}', '{"ready":true,"extra":1}']
)
async def test_workspace_invalid_readiness_payload_is_a_protocol_failure(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    service = _WorkspaceService()
    service.payload = payload
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendProtocolError) as captured:
            await client.create(purpose="workspaces")
        assert captured.value.cause is not None
        assert captured.value.__cause__ is not captured.value
        assert service.calls["POST", "/command"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 0
        assert clock.delays == []
    finally:
        await client.aclose()


@pytest.mark.parametrize("failure", ["http", "remote_protocol"])
async def test_workspace_transient_probe_can_reach_explicit_readiness(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    service = _WorkspaceService()
    if failure == "http":
        service.statuses.append(503)
    else:
        service.failures.append(httpx.RemoteProtocolError("Synthetic peer disconnect"))
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        backend = await client.create(purpose="workspaces")
        assert backend.id == "owned-sandbox"
        assert service.calls["POST", "/command"] == 2
        assert service.calls["POST", "/v1/sandboxes"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 0
        assert clock.delays == [0.2]
        await backend.aclose()
    finally:
        await client.aclose()


async def test_workspace_probe_timeout_does_not_reset_total_ready_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService("headers")
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service, ready_seconds=60)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await service.command_started.wait()
        assert clock.deadlines[-1].when == 130
        service.block = "none"
        clock.deadlines[-1].expire()
        backend = await creating
        assert clock.deadlines[-1].when == 160
        assert service.calls["POST", "/command"] == 2
        assert service.headers_cancelled.is_set()
        await backend.aclose()
    finally:
        service.release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


async def test_workspace_not_ready_exhausts_remaining_budget_without_fake_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.payload = '{"ready":false}'
    clock = _Clock()
    clock.poll_release.clear()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await clock.poll_started.wait()
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxBackendTimeoutError) as captured:
            await creating
        assert captured.value.cause is None
        assert captured.value.diagnostic_context["last_result"] == "not_ready"
        assert service.calls["POST", "/command"] == 1
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
    finally:
        clock.poll_release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


async def test_workspace_deadline_keeps_last_failed_probe_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    original = httpx.RemoteProtocolError("Synthetic response header disconnect")
    service.failures.append(original)
    clock = _Clock()
    clock.poll_release.clear()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await clock.poll_started.wait()
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxBackendTimeoutError) as captured:
            await creating
        assert captured.value.cause is not None
        assert captured.value.cause.__cause__ is original
        assert service.calls["POST", "/command"] == 1
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
    finally:
        clock.poll_release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


async def test_workspace_command_failure_is_protocol_error_without_self_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.command_error = True
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendProtocolError) as captured:
            await client.create(purpose="workspaces")
        assert captured.value.diagnostic_context["command_error"] is True
        assert captured.value.__cause__ is not captured.value
        assert service.calls["POST", "/command"] == 1
        assert clock.delays == []
    finally:
        await client.aclose()


def _causes(error: BaseException) -> list[BaseException]:
    pending = [error]
    found: list[BaseException] = []
    while pending:
        current = pending.pop()
        if any(current is known for known in found):
            continue
        found.append(current)
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return found


async def test_workspace_cleanup_failures_preserve_readiness_error_and_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.statuses.append(401)
    service.delete_status = 500
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    original_close = Sandbox.close
    close_error = RuntimeError("Synthetic close failure")
    closed: list[str] = []

    async def fail_close(sandbox: Sandbox) -> None:
        await original_close(sandbox)
        closed.append(sandbox.id)
        raise close_error

    monkeypatch.setattr(Sandbox, "close", fail_close)
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await client.create(purpose="workspaces")
        assert captured.value.context["reason"] == "authentication"
        assert any(error is close_error for error in _causes(captured.value))
        assert any(
            isinstance(error, OpenSandboxBackendUnavailableError)
            and error.context.get("reason") == "unreachable"
            for error in _causes(captured.value)
        )
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 0
        assert closed == ["owned-sandbox"]
    finally:
        service.delete_status = 204
        monkeypatch.setattr(Sandbox, "close", original_close)
        await client.aclose()


@pytest.mark.parametrize("confirmed_status", [204, 404])
async def test_failed_owned_kill_is_retained_until_explicit_close_confirms_absence(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str], confirmed_status: int
) -> None:
    service = _WorkspaceService()
    service.statuses.append(401)
    service.delete_status = 500
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    monkeypatch.setattr(
        "opensandbox.config.connection.httpx.AsyncHTTPTransport",
        lambda **_options: service,
    )
    client = _client(None)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as readiness:
            await client.create(purpose="workspaces")
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert sdk_closes == ["owned-sandbox"]
        assert not service.closed
        before_close = service.calls.copy()
        with pytest.raises(OpenSandboxBackendError) as cleanup:
            await client.aclose()
        assert cleanup.value.diagnostic_context["sandbox_id"] == "owned-sandbox"
        assert cleanup.value.cause is not None
        assert any(
            error is previous
            for error in _causes(cleanup.value)
            for previous in _causes(readiness.value)
        )
        assert service.calls - before_close == {
            ("DELETE", "/v1/sandboxes/owned-sandbox"): 1
        }
        assert not service.closed
        assert sdk_closes == ["owned-sandbox"]
        service.delete_status = confirmed_status
        await client.aclose()
        assert service.closed
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 3
        await client.aclose()
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 3
    finally:
        service.delete_status = 204
        await client.aclose()


async def test_failed_borrowed_close_is_retained_without_remote_destruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.statuses.append(401)
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    original_close = Sandbox.close
    closed: list[str] = []
    close_error = RuntimeError("Synthetic borrowed close failure")
    allow_close = False

    async def close(sandbox: Sandbox) -> None:
        closed.append(sandbox.id)
        if not allow_close:
            raise close_error
        await original_close(sandbox)

    monkeypatch.setattr(Sandbox, "close", close)
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as readiness:
            await client.connect("existing", purpose="workspaces")
        assert close_error in _causes(readiness.value)
        assert closed == ["existing"]
        with pytest.raises(OpenSandboxBackendError) as cleanup:
            await client.aclose()
        assert cleanup.value.diagnostic_context["sandbox_id"] == "existing"
        assert close_error in _causes(cleanup.value)
        assert closed == ["existing", "existing"]
        allow_close = True
        await client.aclose()
        await client.aclose()
        assert closed == ["existing", "existing", "existing"]
        assert not any(method == "DELETE" for method, _path in service.calls)
        assert not service.closed
    finally:
        allow_close = True
        await client.aclose()


async def test_concurrent_close_attempts_owned_cleanup_once_and_retains_cancel_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.statuses.append(401)
    service.delete_status = 500
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    closing_tasks: list[asyncio.Task[None]] = []
    second_entered = asyncio.Event()

    async def close_again() -> None:
        second_entered.set()
        await client.aclose()

    try:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await client.create(purpose="workspaces")
        service.delete_started.clear()
        service.delete_release.clear()
        closing_tasks.append(asyncio.create_task(client.aclose()))
        await service.delete_started.wait()
        closing_tasks[0].cancel()
        closing_tasks.append(asyncio.create_task(close_again()))
        await second_entered.wait()
        service.delete_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await closing_tasks[0]
        with pytest.raises(OpenSandboxBackendError) as cleanup:
            await closing_tasks[1]
        assert cleanup.value in _causes(cancelled.value)
        assert cleanup.value.diagnostic_context["sandbox_id"] == "owned-sandbox"
        assert not service.delete_cancelled
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 2
        service.delete_status = 204
        await client.aclose()
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 3
    finally:
        service.delete_release.set()
        service.delete_status = 204
        await asyncio.gather(*closing_tasks, return_exceptions=True)
        await client.aclose()


async def test_cancelled_workspace_preserves_failed_kill_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str]
) -> None:
    service = _WorkspaceService("body")
    service.delete_status = 500
    service.delete_release.clear()
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await service.body_started.wait()
        creating.cancel()
        await service.delete_started.wait()
        creating.cancel()
        service.delete_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await creating
        assert any(
            isinstance(error, OpenSandboxBackendError)
            and error.diagnostic_context.get("sandbox_id") == "owned-sandbox"
            for error in _causes(cancelled.value)
        )
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert sdk_closes == ["owned-sandbox"]
        service.delete_status = 204
        await client.aclose()
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 2
    finally:
        service.release.set()
        service.delete_release.set()
        service.delete_status = 204
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


async def test_discovered_owned_candidates_remain_owned_after_failed_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.create_failure = httpx.ReadError("Synthetic lost create response")
    service.discovered_ids = ("owned-first", "owned-second")
    service.delete_status = 500
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendError):
            await client.create(purpose="workspaces")
        assert service.calls["POST", "/v1/sandboxes"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 1
        for sandbox_id in service.discovered_ids:
            assert service.calls["DELETE", f"/v1/sandboxes/{sandbox_id}"] == 1
        with pytest.raises(OpenSandboxBackendError) as cleanup:
            await client.aclose()
        assert {
            error.diagnostic_context["sandbox_id"]
            for error in _causes(cleanup.value)
            if isinstance(error, OpenSandboxBackendError)
            and "sandbox_id" in error.diagnostic_context
        } == set(service.discovered_ids)
        for sandbox_id in service.discovered_ids:
            assert service.calls["DELETE", f"/v1/sandboxes/{sandbox_id}"] == 2
        service.delete_status = 204
        await client.aclose()
        for sandbox_id in service.discovered_ids:
            assert service.calls["DELETE", f"/v1/sandboxes/{sandbox_id}"] == 3
    finally:
        service.delete_status = 204
        await client.aclose()


@pytest.mark.parametrize("operation", ["create", "connect"])
async def test_workspace_cancel_stops_known_preparation_and_respects_remote_ownership(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str], operation: str
) -> None:
    service = _WorkspaceService("body")
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    running = asyncio.create_task(
        client.create(purpose="workspaces")
        if operation == "create"
        else client.connect("existing", purpose="workspaces")
    )
    try:
        await service.body_started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert service.body_closed.is_set()
        assert not service.release.is_set()
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == (
            operation == "create"
        )
        assert service.calls["DELETE", "/v1/sandboxes/existing"] == 0
        assert sdk_closes == ["owned-sandbox" if operation == "create" else "existing"]
    finally:
        service.release.set()
        await asyncio.gather(running, return_exceptions=True)
        await client.aclose()


async def test_workspace_cancel_retains_unknown_create_until_exact_late_id(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str]
) -> None:
    service = _WorkspaceService()
    service.create_release.clear()
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await service.create_started.wait()
        creating.cancel()
        service.create_release.set()
        with pytest.raises(asyncio.CancelledError):
            await creating
        assert not service.create_cancelled
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert sdk_closes == ["owned-sandbox"]
    finally:
        service.create_release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


async def test_workspace_repeated_cancel_keeps_exact_reclamation_owned(
    monkeypatch: pytest.MonkeyPatch, sdk_closes: list[str]
) -> None:
    service = _WorkspaceService("body")
    service.delete_release.clear()
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await service.body_started.wait()
        creating.cancel()
        await service.delete_started.wait()
        creating.cancel()
        service.delete_release.set()
        with pytest.raises(asyncio.CancelledError):
            await creating
        assert not service.delete_cancelled
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert sdk_closes == ["owned-sandbox"]
        assert service.body_closed.is_set()
    finally:
        service.release.set()
        service.delete_release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("inherited_deadline", [None, 104.0])
async def test_workspace_connect_shares_lookup_probe_and_inherited_deadline(
    monkeypatch: pytest.MonkeyPatch, inherited_deadline: float | None
) -> None:
    service = _WorkspaceService("headers")
    clock = _Clock()
    service.clock = clock
    service.endpoint_cost = 2
    service.info_cost = 1
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    token = client_module._connection_deadline.set(inherited_deadline)
    connecting = asyncio.create_task(client.connect("existing", purpose="workspaces"))
    try:
        await service.command_started.wait()
        expected = 105 if inherited_deadline is None else inherited_deadline
        assert clock.now == 103
        assert {deadline.when for deadline in clock.deadlines} == {expected}
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxBackendTimeoutError):
            await connecting
        assert service.calls["DELETE", "/v1/sandboxes/existing"] == 0
    finally:
        client_module._connection_deadline.reset(token)
        service.release.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await client.aclose()


async def test_discovered_workspace_keeps_recovery_deadline_and_is_not_rediscovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService("headers")
    service.create_failure = httpx.ReadError("Synthetic lost create response")
    clock = _Clock()
    service.clock = clock
    service.endpoint_cost = 2
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service, ready_seconds=60)
    creating = asyncio.create_task(client.create(purpose="workspaces"))
    try:
        await service.command_started.wait()
        assert clock.now == 102
        assert clock.deadlines[-1].when == 105
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxBackendTimeoutError):
            await creating
        assert service.calls["POST", "/v1/sandboxes"] == 1
        assert service.calls["GET", "/v1/sandboxes"] == 1
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
    finally:
        service.release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("operation", ["create", "connect"])
async def test_command_purpose_does_not_require_workspace_probe(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service = _WorkspaceService()
    service.metadata = {"tinkerfin.ai/purpose": "commands"}
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    client = _client(service)
    try:
        backend = (
            await client.create(purpose="commands")
            if operation == "create"
            else await client.connect("existing", purpose="commands")
        )
        assert service.calls["POST", "/command"] == 0
        assert service.calls["GET", "/ping"] == 1
        await backend.aclose()
    finally:
        await client.aclose()


async def test_discovery_failure_retains_owned_ids_from_completed_pages() -> None:
    class PagedDiscovery(_WorkspaceService):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/v1/sandboxes":
                self.calls[request.method, request.url.path] += 1
                if request.url.params.get("page") == "2":
                    return httpx.Response(
                        503, json={"code": "BUSY", "message": "Unavailable"}
                    )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {**_sandbox_info(identity), "metadata": self.metadata}
                            for identity in ("exact-owned-first", "exact-owned-second")
                        ],
                        "pagination": {
                            "page": 1,
                            "pageSize": 2,
                            "totalItems": 3,
                            "totalPages": 2,
                            "hasNextPage": True,
                        },
                    },
                )
            return await super().handle_async_request(request)

    service = PagedDiscovery()
    service.create_failure = httpx.ReadError("Lost create response")
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendError):
            await client.create(purpose="workspaces")
    finally:
        await client.aclose()
    deleted = {
        path: count
        for (method, path), count in service.calls.items()
        if method == "DELETE" and count
    }
    assert deleted == {
        "/v1/sandboxes/exact-owned-first": 1,
        "/v1/sandboxes/exact-owned-second": 1,
    }
