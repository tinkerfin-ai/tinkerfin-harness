"""Manager shutdown preserves completed work and retries only failed finalizers."""

from __future__ import annotations

import asyncio

import pytest
from test_manager import _FakeBackend, _FakeClient
from test_workspace_readiness import (
    _causes,
    _client,
    _Clock,
    _WorkspaceService,
)

from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxManager,
)
from tinkerfin_sandbox.lifecycle import client as client_module


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
