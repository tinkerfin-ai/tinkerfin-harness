"""Capability ownership across State, provider metadata, and raw lifecycle APIs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from opensandbox.config import ConnectionConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from test_backend import _FakeOpenSandboxSDKManager, _FakeSandbox, _sandbox_info
from test_manager import _FakeClient, _FakeState, _new_manager, _resource_key
from test_pause_resume import _world
from test_recovery_policy import _fast_policy

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBinding,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxError,
    OpenSandboxErrorCode,
    OpenSandboxOwnerClaim,
    OpenSandboxPurpose,
    OpenSandboxPurposeError,
    OpenSandboxStateError,
    SQLAlchemyOpenSandboxState,
    UnexpectedOpenSandboxBackendError,
)


@pytest.mark.parametrize("purpose", ["commands", "workspaces"])
async def test_provider_creation_reserves_purpose_and_limits_isolation_extension(
    monkeypatch: pytest.MonkeyPatch, purpose: OpenSandboxPurpose
) -> None:
    sandbox = _FakeSandbox()
    created = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.Sandbox.create", created)
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )
    try:
        backend = await client.create(purpose=purpose)
        try:
            assert created.await_args is not None
            arguments = created.await_args.kwargs
            assert arguments["metadata"]["tinkerfin.ai/purpose"] == purpose
            assert arguments["extensions"] == (
                {"bootstrap.execd.isolation": "enable"}
                if purpose == "workspaces"
                else None
            )
        finally:
            await backend.aclose()
    finally:
        await client.aclose()


@pytest.mark.parametrize("operation", ["connect", "inspect"])
@pytest.mark.parametrize(
    ("expected", "observed"),
    [
        ("commands", "workspaces"),
        ("workspaces", "commands"),
        ("commands", None),
        ("workspaces", None),
        ("commands", "unrecognized"),
    ],
)
async def test_provider_mismatch_rejects_before_initialization_and_health_execution(
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["connect", "inspect"],
    expected: OpenSandboxPurpose,
    observed: str | None,
) -> None:
    sandbox = _FakeSandbox("existing")
    sandbox.info.metadata = (
        {} if observed is None else {"tinkerfin.ai/purpose": observed}
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    initialize = AsyncMock()
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
        initializers=[initialize],
    )
    try:
        with pytest.raises(OpenSandboxPurposeError) as captured:
            if operation == "connect":
                await client.connect("existing", purpose=expected)
            else:
                await client.inspect("existing", purpose=expected)
        assert captured.value.code is OpenSandboxErrorCode.PURPOSE_MISMATCH
        assert captured.value.cause is None
        assert captured.value.__cause__ is None
        initialize.assert_not_awaited()
        assert sandbox.commands.calls == []
        assert sandbox.files.created_directories == []
        assert sandbox.closed
        assert not sandbox.killed
    finally:
        await client.aclose()


@pytest.mark.parametrize("operation", ["get", "reconnect", "recreate", "reset"])
@pytest.mark.parametrize("remote_reason", ["not_found", "unreachable"])
async def test_raw_lifecycle_refuses_workspace_binding_before_any_remote_action(
    operation: Literal["get", "reconnect", "recreate", "reset"],
    remote_reason: Literal["not_found", "unreachable"],
) -> None:
    class UnavailableClient(_FakeClient):
        async def connect(
            self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
        ):
            self.connect_calls.append(sandbox_id)
            raise OpenSandboxBackendUnavailableError(
                "Remote instance is unavailable", context={"reason": remote_reason}
            )

    client = UnavailableClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    owner_key = _resource_key("owner")
    state = _FakeState({owner_key: "workspace-parent"})
    state.binding_purposes[owner_key] = "workspaces"
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(OpenSandboxPurposeError):
            match operation:
                case "get":
                    await manager.get("owner")
                case "reconnect":
                    await manager.reconnect("owner")
                case "recreate":
                    await manager.recreate("owner")
                case "reset":
                    await manager.reset("owner")
        assert client.create_calls == 0
        assert client.connect_calls == []
        assert client.inspect_calls == []
        assert client.destroy_calls == []
        assert state.save_calls == []
        assert state.delete_calls == []
        assert state.binding_purposes[owner_key] == "workspaces"


@pytest.mark.parametrize("purpose", ["commands", "workspaces"])
async def test_purpose_survives_worker_restart_and_blocks_reclassification(
    sandbox_sql_engine: AsyncEngine, purpose: OpenSandboxPurpose
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine, namespace="purpose")
    await first.start(warm_pool_size=1)
    owner = await first.acquire_owner("owner")
    try:
        original = await first.bind_owner(owner, "original", purpose=purpose)
    finally:
        await first.release_owner(owner)
        await first.aclose()

    successor = SQLAlchemyOpenSandboxState(
        engine=sandbox_sql_engine, namespace="purpose"
    )
    await successor.start(warm_pool_size=1)
    try:
        assert await successor.read_binding("owner") == original
        claim = await successor.acquire_owner("owner")
        try:
            assert claim.binding == original
            with pytest.raises(OpenSandboxPurposeError):
                await successor.bind_owner(
                    claim,
                    "different-capability",
                    purpose="commands" if purpose == "workspaces" else "workspaces",
                )
        finally:
            await successor.release_owner(claim)
        assert await successor.read_binding("owner") == original
    finally:
        await successor.aclose()


async def test_sql_owner_row_rejects_inconsistent_binding_purpose(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine, namespace="purpose")
    await state.start(warm_pool_size=0)
    try:
        claim = await state.acquire_owner("owner")
        await state.release_owner(claim)
        async with sandbox_sql_engine.begin() as connection:
            row = (
                await connection.execute(
                    text("SELECT sandbox_id, purpose FROM tinkerfin_opensandbox_owners")
                )
            ).one()
            assert tuple(row) == (None, None)
        with pytest.raises(DBAPIError, match="ck_tinkerfin_opensandbox_owners_purpose"):
            async with sandbox_sql_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE tinkerfin_opensandbox_owners SET purpose = 'workspaces'"
                    )
                )
        owner = await state.acquire_owner("owner")
        try:
            await state.bind_owner(owner, "sandbox", purpose="workspaces")
        finally:
            await state.release_owner(owner)
        with pytest.raises(DBAPIError, match="ck_tinkerfin_opensandbox_owners_purpose"):
            async with sandbox_sql_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE tinkerfin_opensandbox_owners SET purpose = NULL")
                )
    finally:
        await state.aclose()


@pytest.mark.parametrize("difference", ["purpose", "generation"])
async def test_lost_bind_response_requires_the_complete_binding_to_match(
    difference: Literal["purpose", "generation"],
) -> None:
    class ConflictingReadState(_FakeState):
        async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
            binding = await super().read_binding(owner_key)
            assert binding is not None
            return (
                replace(binding, purpose="workspaces")
                if difference == "purpose"
                else replace(binding, generation=binding.generation + 1)
            )

    state = ConflictingReadState()
    state.commit_before_save_error = True
    state.save_error = RuntimeError("binding response lost")
    client = _FakeClient()
    async with _new_manager(client=client, state=state) as manager:
        with pytest.raises(OpenSandboxStateError, match="fake state write failed"):
            await manager.get("owner")
        assert client.destroy_calls == []
        assert client.backends[0].close_calls == 1
        assert state.bindings[_resource_key("owner")] == "sandbox-1"


def test_purpose_error_has_stable_safe_immutable_context_and_preserves_cause() -> None:
    cause = ValueError("private provider details")
    context = {"expected_purpose": "commands", "actual_purpose": "workspaces"}
    error = OpenSandboxPurposeError("Capability mismatch", context=context, cause=cause)
    context["expected_purpose"] = "mutated"
    assert isinstance(error, OpenSandboxError)
    assert error.code is OpenSandboxErrorCode.PURPOSE_MISMATCH
    assert error.context["expected_purpose"] == "commands"
    assert not hasattr(error.context, "__setitem__")
    assert error.cause is error.__cause__ is cause
    assert "private" not in str(error)


async def test_custom_state_purpose_errors_cross_the_manager_boundary_unchanged() -> (
    None
):
    mismatch = OpenSandboxPurposeError("Capability mismatch")

    class RefusingState(_FakeState):
        async def bind_owner(
            self,
            claim: OpenSandboxOwnerClaim,
            sandbox_id: str,
            *,
            purpose: OpenSandboxPurpose,
        ) -> OpenSandboxBinding:
            raise mismatch

    client = _FakeClient()
    async with _new_manager(client=client, state=RefusingState()) as manager:
        with pytest.raises(OpenSandboxPurposeError) as captured:
            await manager.get("owner")
        assert captured.value is mismatch
        assert client.destroy_calls == ["sandbox-1"]


async def test_same_id_rebound_generation_cannot_skip_remote_purpose_validation() -> (
    None
):
    mismatch = OpenSandboxPurposeError("Provider purpose conflicts with the binding")

    class RefusingReconnectClient(_FakeClient):
        async def connect(
            self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
        ) -> OpenSandboxBackend:
            assert purpose == "commands"
            self.connect_calls.append(sandbox_id)
            raise mismatch

    client = RefusingReconnectClient()
    state = _FakeState()
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        handle = await manager.get("owner")
        original_binding = await state.read_binding(_resource_key("owner"))
        assert original_binding is not None
        claim = await state.acquire_owner(_resource_key("owner"))
        try:
            await state.unbind_owner(claim)
            replacement = await state.bind_owner(claim, handle.id, purpose="commands")
        finally:
            await state.release_owner(claim)
        assert replacement.generation != original_binding.generation
        assert replacement.sandbox_id == original_binding.sandbox_id
        with pytest.raises(OpenSandboxPurposeError) as captured:
            await manager.get("owner")
        assert captured.value is mismatch
        assert client.connect_calls == [handle.id]
        assert client.create_calls == 1
        assert client.destroy_calls == []


async def test_workspace_pause_and_resume_preserve_parent_without_returning_backend(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first = await world.add()
        second = await world.add()
        parent = await world.clients[0].create(purpose="workspaces")
        parent_id = parent.id
        state = world.states[0]
        claim = await state.acquire_owner(_resource_key("owner"))
        try:
            binding = await state.bind_owner(claim, parent_id, purpose="workspaces")
        finally:
            await state.release_owner(claim)
            await parent.aclose()

        assert await first.pause("owner") is None
        assert world.remote.states[parent_id] == "Paused"
        assert await second.resume("owner") is None
        details = await second.get_details("owner")
        assert details is not None and details.sandbox_id == parent_id
        assert details.healthy
        assert await second.pause("owner") is None
        assert await first.resume("owner") is None
        assert world.remote.states[parent_id] == "Running"
        assert await state.read_binding(_resource_key("owner")) == binding
        assert world.remote.created == 1
        assert world.remote.destroy_calls == []
        assert world.remote.resume_calls == [parent_id, parent_id]
        with pytest.raises(OpenSandboxPurposeError):
            await first.get("owner")


async def test_management_resume_never_allocates_an_unbound_owner() -> None:
    client = _FakeClient()
    async with _new_manager(
        client=client, state=_FakeState(), recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await manager.resume("unbound")
        assert captured.value.context["reason"] == "not_bound"
        assert client.create_calls == 0
        assert client.connect_calls == []
        assert client.destroy_calls == []


@pytest.mark.parametrize("candidate_count", [1, 2])
async def test_unknown_create_cannot_adopt_or_destroy_candidates_with_other_purpose(
    monkeypatch: pytest.MonkeyPatch, candidate_count: int
) -> None:
    token = UUID(int=1)
    sdk_manager = _FakeOpenSandboxSDKManager(
        [
            _sandbox_info(
                f"foreign-{index}",
                metadata={
                    "tinkerfin.ai/create-token": token.hex,
                    "tinkerfin.ai/purpose": "workspaces",
                },
            )
            for index in range(candidate_count)
        ]
    )
    original = RuntimeError("Create response was lost")
    connect = AsyncMock()
    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.uuid4", lambda: token)
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(side_effect=original),
    )
    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.Sandbox.connect", connect)
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.OpenSandboxSDKManager.create",
        AsyncMock(return_value=sdk_manager),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )
    try:
        with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
            await client.create(purpose="commands")
        assert captured.value.cause is original
        connect.assert_not_awaited()
        assert sdk_manager.killed_ids == []
        assert sdk_manager.closed
    finally:
        await client.aclose()
