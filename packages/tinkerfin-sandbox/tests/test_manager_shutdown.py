"""Manager shutdown preserves completed work and retries only failed finalizers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from test_manager import _FakeBackend, _FakeClient
from test_workspace_readiness import (
    _causes,
    _client,
    _Clock,
    _Deadline,
    _WorkspaceService,
)

from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    Notifications,
    NotificationScope,
)
from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxError,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxPurpose,
    OpenSandboxSettlementTimeoutError,
    OpenSandboxStateError,
)
from tinkerfin_sandbox.lifecycle import client as client_module
from tinkerfin_sandbox.lifecycle import manager as manager_module


class _ClosingClient(_FakeClient):
    def __init__(self, backend: _FakeBackend | None = None) -> None:
        super().__init__(
            backend_factory=None if backend is None else lambda _sandbox_id: backend
        )
        self.close_error: BaseException | None = None
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.close_cancelled = False

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        try:
            await self.close_release.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        if self.close_error is not None:
            raise self.close_error
        self.closed.set()


class _ClosingState(InMemoryOpenSandboxState):
    def __init__(self, *, persistent: bool = False) -> None:
        super().__init__()
        self.close_error: BaseException | None = None
        self.close_calls = 0
        self.shutdown_calls = 0
        self.retain_remote = persistent

    @property
    def persistent(self) -> bool:
        return self.retain_remote

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        self.shutdown_calls += 1
        return () if self.persistent else await super().shutdown_sandbox_ids()

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        await super().aclose()


async def test_manager_reports_and_retries_its_owned_sdk_client_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkspaceService()
    service.payload = "invalid readiness JSON"
    service.delete_status = 503
    monkeypatch.setattr(client_module, "asyncio", _Clock().module())
    client = _client(service)
    state = _ClosingState()
    manager = OpenSandboxManager(client=client, state=state)
    try:
        await manager.start()
        with pytest.raises(OpenSandboxBackendProtocolError):
            async with manager.workspace("owner", workspace_key="project").open():
                pytest.fail("Invalid readiness cannot produce a workspace")
        assert service.calls["POST", "/v1/sandboxes"] == 1
        with pytest.raises(OpenSandboxBackendError) as captured:
            await manager.aclose()
        assert captured.value.diagnostic_context["sandbox_id"] == "owned-sandbox"
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 2
        assert state.close_calls == state.shutdown_calls == 1
        service.delete_status = 204
        await manager.aclose()
        await manager.aclose()
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 3
        assert state.close_calls == state.shutdown_calls == 1
        assert not service.closed
    finally:
        service.delete_status = 204
        await manager.aclose()
        await client.aclose()


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("failed_finalizer", ["client", "state", "both"])
async def test_manager_retries_only_unfinished_finalizers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    persistent: bool,
    failed_finalizer: str,
) -> None:
    backend = _FakeBackend("known-instance")
    client = _ClosingClient(backend)
    state = _ClosingState(persistent=persistent)
    client_error = OpenSandboxBackendError("Controlled client close failure")
    state_error = OpenSandboxStateError("Controlled State close failure")
    client.close_error = client_error if failed_finalizer != "state" else None
    state.close_error = state_error if failed_finalizer != "client" else None
    closed_notifications: list[MemoryBackend] = []
    close_memory = MemoryBackend.aclose

    async def observe_close(notification_backend: MemoryBackend) -> None:
        await close_memory(notification_backend)
        closed_notifications.append(notification_backend)

    monkeypatch.setattr(MemoryBackend, "aclose", observe_close)
    borrowed_backend = MemoryBackend()
    async with Notifications(backend=borrowed_backend) as notifications:
        manager = OpenSandboxManager(
            client=client,
            state=state,
            notifications=notifications if persistent else None,
        )
        try:
            await manager.start()
            handle = await manager.get("owner")
            with pytest.raises(OpenSandboxError) as captured:
                await manager.aclose()
            assert captured.value is (
                client_error if failed_finalizer == "client" else state_error
            )
            if failed_finalizer == "both":
                assert client_error in _causes(captured.value)
            assert not any(
                record.name.startswith("tinkerfin.sandbox") for record in caplog.records
            )
            assert client.close_calls == state.close_calls == state.shutdown_calls == 1
            assert backend.close_calls == 1
            assert handle.is_closed
            assert client.destroy_calls == ([] if persistent else [backend.id])
            assert len(closed_notifications) == (0 if persistent else 1)
            with pytest.raises(OpenSandboxManagerClosedError):
                await manager.get("owner")
            client.close_error = state.close_error = None
            await manager.aclose()
            await manager.aclose()
            assert client.close_calls == (1 if failed_finalizer == "state" else 2)
            assert state.close_calls == (1 if failed_finalizer == "client" else 2)
            assert state.shutdown_calls == backend.close_calls == 1
            assert client.destroy_calls == ([] if persistent else [backend.id])
            assert len(closed_notifications) == (0 if persistent else 1)
            assert borrowed_backend not in closed_notifications
            notification = Notification(
                scope=NotificationScope("borrowed"), topic="usable", key="one"
            )
            async with notifications.subscribe() as subscription:
                await notifications.publish(notification)
                assert await anext(subscription) == notification
        finally:
            client.close_error = state.close_error = None
            await manager.aclose()


@pytest.mark.parametrize("retry", [False, True])
async def test_cancelled_manager_close_caller_does_not_repeat_concurrent_finalization(
    retry: bool,
) -> None:
    client = _ClosingClient()
    client.close_error = OpenSandboxBackendError("Controlled close failure")
    state = _ClosingState()
    manager = OpenSandboxManager(client=client, state=state)
    tasks: list[asyncio.Task[None]] = []
    second_entered = asyncio.Event()

    async def close_again() -> None:
        second_entered.set()
        await manager.aclose()

    try:
        await manager.start()
        if retry:
            with pytest.raises(OpenSandboxBackendError):
                await manager.aclose()
        client.close_entered.clear()
        client.close_release.clear()
        tasks.append(asyncio.create_task(manager.aclose()))
        await client.close_entered.wait()
        tasks.append(asyncio.create_task(close_again()))
        await second_entered.wait()
        tasks[0].cancel("Closing caller stopped waiting")
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not client.close_cancelled
        assert not tasks[1].done()
        client.close_release.set()
        with pytest.raises(OpenSandboxBackendError) as captured:
            await tasks[1]
        assert captured.value is client.close_error
        assert client.close_calls == (2 if retry else 1)
        assert state.close_calls == state.shutdown_calls == 1
        client.close_error = None
        await manager.aclose()
        assert client.close_calls == (3 if retry else 2)
        assert state.close_calls == state.shutdown_calls == 1
    finally:
        client.close_error = None
        client.close_release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.aclose()


async def test_manager_close_timeout_keeps_the_unfinished_finalizer_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    controlled = clock.module()
    setattr(
        controlled, "timeout", lambda seconds: _Deadline(clock, clock.now + seconds)
    )
    monkeypatch.setattr(manager_module, "asyncio", controlled)
    client = _ClosingClient()
    client.close_release.clear()
    manager = OpenSandboxManager(client=client, settlement_timeout=1)
    closing = asyncio.create_task(manager.aclose())
    try:
        await client.close_entered.wait()
        clock.deadlines[-1].expire()
        with pytest.raises(OpenSandboxSettlementTimeoutError):
            await closing
        assert not client.close_cancelled
        client.close_release.set()
        await manager.aclose()
        await manager.aclose()
        assert client.close_calls == 1
    finally:
        client.close_release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await manager.aclose()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_manager_context_preserves_body_failure_and_finalizer_cause(
    cancelled: bool,
) -> None:
    client = _ClosingClient()
    close_error = OpenSandboxBackendError("Controlled context cleanup failure")
    client.close_error = close_error
    manager = OpenSandboxManager(client=client)
    original = (
        asyncio.CancelledError("Body cancelled")
        if cancelled
        else ValueError("Body failed")
    )
    try:
        with pytest.raises(type(original)) as captured:
            async with manager:
                raise original
        assert captured.value is original
        assert close_error in _causes(captured.value)
        client.close_error = None
        await manager.aclose()
        assert client.close_calls == 2
    finally:
        client.close_error = None
        await manager.aclose()


async def test_manager_startup_failure_retains_its_failed_finalizer_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ClosingClient()
    startup_error = OpenSandboxBackendError("Controlled startup failure")
    close_error = OpenSandboxBackendError("Controlled startup cleanup failure")
    client.close_error = close_error

    async def fail_create(
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        del purpose, metadata
        raise startup_error

    monkeypatch.setattr(client, "create", fail_create)
    manager = OpenSandboxManager(
        client=client, warm_pool_size=1, fail_on_startup_warmup_error=True
    )
    try:
        with pytest.raises(OpenSandboxBackendError) as captured:
            async with manager:
                pytest.fail("Failed startup cannot enter the manager")
        assert captured.value is startup_error
        assert close_error in _causes(captured.value)
        client.close_error = None
        await manager.aclose()
        assert client.close_calls == 2
    finally:
        client.close_error = None
        await manager.aclose()


async def test_sticky_state_failure_does_not_skip_unfinished_client_cleanup() -> None:
    client = _ClosingClient()
    state = _ClosingState(persistent=True)
    client_error = OpenSandboxBackendError("Controlled client close failure")
    state_error = OpenSandboxStateError("Retained State close failure")
    client.close_error = client_error
    state.close_error = state_error
    manager = OpenSandboxManager(client=client, state=state)
    try:
        await manager.start()
        with pytest.raises(OpenSandboxStateError) as first:
            await manager.aclose()
        assert first.value is state_error
        assert client_error in _causes(first.value)
        client.close_error = None
        with pytest.raises(OpenSandboxStateError) as second:
            await manager.aclose()
        assert second.value is state_error
        assert client.close_calls == state.close_calls == 2
        with pytest.raises(OpenSandboxStateError) as third:
            await manager.aclose()
        assert third.value is state_error
        assert client.close_calls == 2
        assert state.close_calls == 3
        assert state.shutdown_calls == 1
        assert client.destroy_calls == []
    finally:
        client.close_error = state.close_error = None
        await manager.aclose()
