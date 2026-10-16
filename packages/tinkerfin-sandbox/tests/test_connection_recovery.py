"""Recover existing workspaces without polling known stopped instances."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_workspace_readiness import _client, _Clock, _WorkspaceService

import tinkerfin_sandbox.lifecycle.client as client_module
from tinkerfin_contracts import WorkspacePreparationFailure
from tinkerfin_sandbox import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxPausedError,
    OpenSandboxRecoveryPolicy,
)


@pytest.mark.parametrize("state", ["Terminated", "Failed", "Paused", "Pending"])
async def test_connect_rejects_unusable_control_state_before_endpoint_requests(
    state: str,
) -> None:
    service = _WorkspaceService()
    service.runtime_state = state
    client = _client(service)
    try:
        expected = (
            OpenSandboxPausedError
            if state == "Paused"
            else OpenSandboxBackendUnavailableError
        )
        with pytest.raises(expected) as captured:
            await client.connect("existing", purpose="workspaces")
        assert isinstance(captured.value, WorkspacePreparationFailure)
        assert captured.value.workspace_failure == (
            "paused"
            if state == "Paused"
            else "unavailable"
            if state == "Pending"
            else "stopped"
        )
        assert service.calls["GET", "/v1/sandboxes/existing"] == 1
        assert not any("/endpoints/" in path for _, path in service.calls)
        assert service.calls["POST", "/command"] == 0
        assert not any(method == "DELETE" for method, _ in service.calls)
    finally:
        await client.aclose()


async def test_one_connection_attempt_does_not_retry_its_readiness_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    service = _WorkspaceService()
    service.failures.append(httpx.ConnectError("test-owned disconnected endpoint"))
    client = _client(service)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await client.connect("existing", purpose="workspaces")
        assert captured.value.workspace_failure == "unavailable"
        assert service.calls["POST", "/command"] == 1
        assert not any(method == "DELETE" for method, _ in service.calls)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "failure_type", [httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_manager_retries_a_bound_workspace_once_and_preserves_its_binding(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[httpx.RequestError],
) -> None:
    from test_manager import _FakeState, _new_manager, _resource_key

    import tinkerfin_sandbox.lifecycle._manager_recovery as recovery_module

    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    monkeypatch.setattr(recovery_module, "asyncio", clock.module())
    service = _WorkspaceService()
    service.failures.extend(failure_type("test-owned outage") for _ in range(2))
    owner = _resource_key("owner")
    state = _FakeState({owner: "existing"})
    state.binding_purposes[owner] = "workspaces"
    manager = _new_manager(client=_client(service, connect_seconds=30), state=state)
    await manager.start()
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with manager.workspace("owner", workspace_key="project").open():
                raise AssertionError("An unreachable workspace must not be admitted")
        assert service.calls["POST", "/command"] == 2
        assert clock.delays == [15.0]
        assert state.bindings == {owner: "existing"}
        assert not any(method == "DELETE" for method, _ in service.calls)
    finally:
        await manager.aclose()


async def test_creation_readiness_retries_once_after_fifteen_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    service = _WorkspaceService()
    service.payloads.extend(['{"ready":false}', '{"ready":true}'])
    client = _client(service, ready_seconds=30)
    try:
        backend = await client.create(purpose="workspaces")
        assert backend.id == "owned-sandbox"
        assert service.calls["POST", "/v1/sandboxes"] == 1
        assert service.calls["POST", "/command"] == 2
        assert clock.delays == [15.0]
        await backend.aclose()
    finally:
        await client.aclose()


async def test_connection_cancellation_closes_the_readiness_response() -> None:
    service = _WorkspaceService(block="body")
    client = _client(service)
    task = asyncio.create_task(client.connect("existing", purpose="workspaces"))
    try:
        await service.body_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.body_closed.is_set()
        assert service.calls["POST", "/command"] == 1
        assert not any(method == "DELETE" for method, _ in service.calls)
    finally:
        service.release.set()
        await client.aclose()


async def test_cached_handle_checks_remote_stop_before_executing_health() -> None:
    from test_manager import _FakeClient, _FakeState, _new_manager, _runtime_info

    client = _FakeClient()
    state = _FakeState()
    manager = _new_manager(client=client, state=state)
    await manager.start()
    try:
        backend = await manager.get("owner")
        before = list(client.backends[0].execute_calls)
        client.inspection_results[backend.id] = _runtime_info(
            backend.id, state="Terminated", healthy=False
        )
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await manager.get("owner")
        assert captured.value.workspace_failure == "stopped"
        assert client.backends[0].execute_calls == before
        assert client.connect_calls == []
        assert client.destroy_calls == []
    finally:
        await manager.aclose()


async def test_new_instance_readiness_stops_after_two_probes_and_reclaims_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    service = _WorkspaceService()
    service.payload = '{"ready":false}'
    client = _client(service, ready_seconds=30)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await client.create(purpose="workspaces")
        assert service.calls["POST", "/v1/sandboxes"] == 1
        assert service.calls["POST", "/command"] == 2
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
        assert clock.delays == [15.0]
    finally:
        await client.aclose()


def test_default_recovery_has_one_delayed_retry_with_one_shared_budget() -> None:
    policy = OpenSandboxRecoveryPolicy()
    assert (
        policy.max_attempts,
        policy.initial_delay,
        policy.max_delay,
        policy.timeout,
    ) == (2, 15.0, 15.0, 30.0)
    assert policy.on_failure == "raise"


@pytest.mark.parametrize(
    "status, reason", [(401, "authentication"), (403, "permission")]
)
async def test_command_readiness_rejects_authorization_without_retry(
    monkeypatch: pytest.MonkeyPatch, status: int, reason: str
) -> None:
    clock = _Clock()
    monkeypatch.setattr(client_module, "asyncio", clock.module())
    service = _WorkspaceService()
    service.statuses.append(status)
    client = _client(service, ready_seconds=30)
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await client.create(purpose="commands")
        assert captured.value.context["reason"] == reason
        assert service.calls["POST", "/command"] == 1
        assert clock.delays == []
        assert service.calls["DELETE", "/v1/sandboxes/owned-sandbox"] == 1
    finally:
        await client.aclose()
