"""Availability, registration, and drain evidence contracts for shared State."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox.errors import (
    OpenSandboxPurposeError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    UnexpectedOpenSandboxStateError,
)
from tinkerfin_sandbox.lifecycle.availability import OpenSandboxAvailability
from tinkerfin_sandbox.lifecycle.sqlalchemy import (
    SQLAlchemyOpenSandboxState,
    get_sqlalchemy_opensandbox_state_schema,
)
from tinkerfin_sandbox.lifecycle.state import (
    InMemoryOpenSandboxState,
    OpenSandboxState,
    _OpenSandboxStateBoundary,
)
from tinkerfin_sandbox.models import OpenSandboxPurpose


@pytest.fixture(params=["memory", "sqlite"])
async def state(
    sql_engine: SqlEngineFactory, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[OpenSandboxState]:
    instance: OpenSandboxState = (
        InMemoryOpenSandboxState(namespace="availability")
        if request.param == "memory"
        else SQLAlchemyOpenSandboxState(
            engine=sql_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"),
            namespace="availability",
        )
    )
    await instance.start(warm_pool_size=1)
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.mark.parametrize("purpose", ["commands", "workspaces"])
async def test_binding_purpose_survives_claims_and_requires_explicit_unbinding(
    state: OpenSandboxState, purpose: OpenSandboxPurpose
) -> None:
    other: OpenSandboxPurpose = "workspaces" if purpose == "commands" else "commands"
    claim = await state.acquire_owner("purpose-owner")
    try:
        original = await state.bind_owner(claim, "original", purpose=purpose)
        with pytest.raises(OpenSandboxPurposeError):
            await state.bind_owner(claim, "forbidden", purpose=other)
        repeated = await state.bind_owner(claim, "original", purpose=purpose)
        assert repeated == original
    finally:
        await state.release_owner(claim)
    assert await state.read_binding("purpose-owner") == original

    successor = await state.acquire_owner("purpose-owner")
    try:
        assert successor.binding == original
        replacement = await state.bind_owner(successor, "replacement", purpose=purpose)
        assert replacement.purpose == purpose
        assert replacement.generation == successor.generation
        with pytest.raises(OpenSandboxPurposeError):
            await state.bind_owner(successor, "forbidden", purpose=other)
        await state.unbind_owner(successor)
        changed = await state.bind_owner(successor, "new-purpose", purpose=other)
        assert changed.purpose == other
    finally:
        await state.release_owner(successor)
    assert await state.read_binding("purpose-owner") == changed


async def test_warm_consumption_cannot_change_workspace_purpose_or_consume_capacity(
    state: OpenSandboxState,
) -> None:
    warm = await state.claim_warm_slot()
    assert warm is not None
    await state.publish_warm(warm, "warm-command-sandbox")
    owner = await state.acquire_owner("workspace-owner")
    try:
        bound = await state.bind_owner(owner, "workspace-parent", purpose="workspaces")
        with pytest.raises(OpenSandboxPurposeError):
            await state.consume_warm(owner)
    finally:
        await state.release_owner(owner)
    assert await state.read_binding("workspace-owner") == bound
    recipient = await state.acquire_owner("command-owner")
    try:
        consumed = await state.consume_warm(recipient)
        assert consumed is not None
        assert consumed.sandbox_id == "warm-command-sandbox"
        assert consumed.purpose == "commands"
    finally:
        await state.release_owner(recipient)


async def test_binding_and_warm_consumption_publish_running_availability(
    state: OpenSandboxState,
) -> None:
    assert await state.read_availability("owner") is None
    claim = await state.acquire_owner("owner")
    try:
        binding = await state.bind_owner(claim, "sandbox", purpose="commands")
        available = await state.register_holder(claim, "manager")
        assert (available.sandbox_id, available.binding_generation) == (
            binding.sandbox_id,
            binding.generation,
        )
        assert (
            available.phase,
            available.sequence,
            available.connection_generation,
        ) == ("running", 0, 0)
        assert await state.read_availability("owner") == available
    finally:
        await state.release_owner(claim)

    warm = await state.claim_warm_slot()
    assert warm is not None
    await state.publish_warm(warm, "warm-sandbox")
    warm_owner = await state.acquire_owner("warm-owner")
    try:
        consumed = await state.consume_warm(warm_owner)
        assert consumed is not None
        current = await state.register_holder(warm_owner, "manager")
        assert current.sandbox_id == consumed.sandbox_id
        assert current.binding_generation == consumed.generation
    finally:
        await state.release_owner(warm_owner)
    assert len(await state.get_holder_updates("manager")) == 2


async def test_all_holders_must_acknowledge_before_dispatch(
    state: OpenSandboxState,
) -> None:
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.register_holder(claim, "manager-a")
        await state.register_holder(claim, "manager-b")
        draining = await state.change_availability(claim, running, phase="draining")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.register_holder(claim, "late-manager")
        assert not await state.holders_are_idle(claim, draining)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.change_availability(claim, draining, phase="pausing")
        updates = await state.get_holder_updates("manager-a")
        assert updates[0].availability == draining
        assert await state.acknowledge_idle("manager-a", draining)
        assert await state.acknowledge_idle("manager-a", draining)
        assert not await state.holders_are_idle(claim, draining)
        assert await state.acknowledge_idle("manager-b", draining)
        assert await state.holders_are_idle(claim, draining)
        pausing = await state.change_availability(claim, draining, phase="pausing")
        paused = await state.change_availability(claim, pausing, phase="paused")
        assert paused.sequence == 3
        assert not await state.acknowledge_idle("manager-a", draining)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.register_holder(claim, "paused-manager")
    finally:
        await state.release_owner(claim)


async def test_cancelled_drain_cannot_accept_late_ack_or_rollback_after_dispatch(
    state: OpenSandboxState,
) -> None:
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.register_holder(claim, "manager")
        first = await state.change_availability(claim, running, phase="draining")
        assert await state.acknowledge_idle("manager", first)
        restored = await state.change_availability(claim, first, phase="running")
        current = await state.change_availability(claim, restored, phase="draining")
        assert not await state.acknowledge_idle("manager", first)
        assert not await state.holders_are_idle(claim, current)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.change_availability(claim, first, phase="running")
        assert await state.acknowledge_idle("manager", current)
        pausing = await state.change_availability(claim, current, phase="pausing")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.change_availability(claim, current, phase="running")
        assert await state.read_availability("owner") == pausing
    finally:
        await state.release_owner(claim)


async def test_resume_publishes_connection_refresh_and_fences_full_snapshot(
    state: OpenSandboxState,
) -> None:
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.read_availability("owner")
        assert running is not None
        for stale in (
            replace(running, sequence=1),
            replace(running, sandbox_id="different"),
            replace(running, binding_generation=running.binding_generation + 1),
            replace(running, connection_generation=1),
            replace(running, phase="paused"),
        ):
            with pytest.raises(OpenSandboxStateOwnershipError):
                await state.change_availability(claim, stale, phase="draining")
        with pytest.raises(ValueError):
            await state.change_availability(
                claim, running, phase="draining", refresh_connection=True
            )
        draining = await state.change_availability(claim, running, phase="draining")
        pausing = await state.change_availability(claim, draining, phase="pausing")
        paused = await state.change_availability(claim, pausing, phase="paused")
        resuming = await state.change_availability(claim, paused, phase="resuming")
        current = await state.change_availability(
            claim, resuming, phase="running", refresh_connection=True
        )
        assert current.sequence == 5
        assert current.connection_generation == 1
        assert (current.sandbox_id, current.binding_generation) == (
            running.sandbox_id,
            running.binding_generation,
        )
    finally:
        await state.release_owner(claim)


async def test_replacement_rejects_old_ack_and_old_holder_release(
    state: OpenSandboxState,
) -> None:
    first = await state.acquire_owner("owner")
    await state.bind_owner(first, "same-remote-id", purpose="commands")
    old = await state.register_holder(first, "manager")
    old_drain = await state.change_availability(first, old, phase="draining")
    await state.release_owner(first)
    current = await state.acquire_owner("owner")
    try:
        await state.bind_owner(current, "same-remote-id", purpose="commands")
        observed = await state.get_holder_updates("manager")
        assert observed[0].binding_generation == old.binding_generation
        assert observed[0].availability.binding_generation == current.generation
        running = await state.register_holder(current, "manager")
        assert running.binding_generation != old.binding_generation
        await state.unregister_holder("manager", old)
        assert len(await state.get_holder_updates("manager")) == 1
        draining = await state.change_availability(current, running, phase="draining")
        assert not await state.acknowledge_idle("manager", old_drain)
        assert not await state.holders_are_idle(current, draining)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.unbind_owner(first)
        assert await state.read_availability("owner") == draining
        await state.unregister_holder("manager", running)
        assert await state.holders_are_idle(current, draining)
        await state.unbind_owner(current)
        assert await state.read_availability("owner") is None
        assert await state.get_holder_updates("manager") == ()
    finally:
        await state.release_owner(current)


async def test_rejected_remote_requests_can_restore_the_confirmed_preceding_state(
    state: OpenSandboxState,
) -> None:
    """A manager with rejection evidence may undo only its exact dispatched intent."""
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.read_availability("owner")
        assert running is not None
        draining = await state.change_availability(claim, running, phase="draining")
        pausing = await state.change_availability(claim, draining, phase="pausing")
        restored = await state.change_availability(claim, pausing, phase="running")
        assert restored.sequence == pausing.sequence + 1
        current = await state.change_availability(claim, restored, phase="draining")
        dispatched = await state.change_availability(claim, current, phase="pausing")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.change_availability(claim, pausing, phase="running")
        paused = await state.change_availability(claim, dispatched, phase="paused")
        resuming = await state.change_availability(claim, paused, phase="resuming")
        rejected = await state.change_availability(claim, resuming, phase="paused")
        assert rejected.sequence == resuming.sequence + 1
        assert rejected.connection_generation == 0
    finally:
        await state.release_owner(claim)


async def test_repeated_binding_does_not_reset_current_drain(
    state: OpenSandboxState,
) -> None:
    claim = await state.acquire_owner("owner")
    try:
        binding = await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.register_holder(claim, "manager")
        draining = await state.change_availability(claim, running, phase="draining")
        assert await state.bind_owner(claim, "sandbox", purpose="commands") == binding
        assert await state.read_availability("owner") == draining
    finally:
        await state.release_owner(claim)


async def test_explicit_resume_can_restore_an_externally_paused_running_binding(
    state: OpenSandboxState,
) -> None:
    """Confirmed external pause permits an explicit resume without a new binding."""
    claim = await state.acquire_owner("owner")
    try:
        binding = await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.read_availability("owner")
        assert running is not None
        resuming = await state.change_availability(claim, running, phase="resuming")
        restored = await state.change_availability(
            claim, resuming, phase="running", refresh_connection=True
        )
        assert restored.sequence == running.sequence + 2
        assert restored.connection_generation == running.connection_generation + 1
        assert restored.binding_generation == binding.generation
    finally:
        await state.release_owner(claim)


async def test_explicit_pause_drains_an_externally_resumed_paused_binding(
    state: OpenSandboxState,
) -> None:
    """Confirmed external resume requires a fresh drain before explicit pause."""
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.register_holder(claim, "manager")
        draining = await state.change_availability(claim, running, phase="draining")
        assert await state.acknowledge_idle("manager", draining)
        pausing = await state.change_availability(claim, draining, phase="pausing")
        paused = await state.change_availability(claim, pausing, phase="paused")
        current = await state.change_availability(claim, paused, phase="draining")
        assert current.sequence == paused.sequence + 1
        assert not await state.holders_are_idle(claim, current)
        assert not await state.acknowledge_idle("manager", draining)
        assert await state.acknowledge_idle("manager", current)
        assert await state.holders_are_idle(claim, current)
    finally:
        await state.release_owner(claim)


@pytest.mark.parametrize("register_first", [True, False])
async def test_registration_and_drain_have_one_atomic_order(
    state: OpenSandboxState, register_first: bool
) -> None:
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.read_availability("owner")
        assert running is not None
        register = state.register_holder(claim, "manager")
        drain = state.change_availability(claim, running, phase="draining")
        results = await asyncio.gather(
            *(register, drain) if register_first else (drain, register),
            return_exceptions=True,
        )
        registered, drained = results if register_first else reversed(results)
        assert isinstance(drained, OpenSandboxAvailability)
        if isinstance(registered, OpenSandboxStateOwnershipError):
            assert await state.get_holder_updates("manager") == ()
            assert await state.holders_are_idle(claim, drained)
        else:
            assert registered == running
            assert not await state.holders_are_idle(claim, drained)
    finally:
        await state.release_owner(claim)


async def test_released_owner_fence_cannot_change_availability(
    state: OpenSandboxState,
) -> None:
    old = await state.acquire_owner("owner")
    await state.bind_owner(old, "sandbox", purpose="commands")
    running = await state.register_holder(old, "manager")
    await state.release_owner(old)
    current = await state.acquire_owner("owner")
    try:
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.change_availability(old, running, phase="draining")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.register_holder(old, "late")
        draining = await state.change_availability(current, running, phase="draining")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await state.holders_are_idle(old, draining)
    finally:
        await state.release_owner(current)


async def test_sqlite_close_and_reopen_do_not_fabricate_holder_idle(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'durable.db'}"
    first = SQLAlchemyOpenSandboxState(engine=sql_engine(url))
    await first.start(warm_pool_size=0)
    claim = await first.acquire_owner("owner")
    await first.bind_owner(claim, "sandbox", purpose="commands")
    running = await first.register_holder(claim, "lost-manager")
    await first.release_owner(claim)
    await first.aclose()

    reopened = SQLAlchemyOpenSandboxState(engine=sql_engine(url))
    await reopened.start(warm_pool_size=0)
    successor = await reopened.acquire_owner("owner")
    try:
        assert await reopened.read_availability("owner") == running
        draining = await reopened.change_availability(
            successor, running, phase="draining"
        )
        assert not await reopened.holders_are_idle(successor, draining)
        updates = await reopened.get_holder_updates("lost-manager")
        assert updates[0].acknowledged_sequence is None
        with pytest.raises(OpenSandboxStateOwnershipError):
            await reopened.change_availability(successor, draining, phase="pausing")
    finally:
        await reopened.release_owner(successor)
        await reopened.aclose()


async def test_sqlite_other_state_acknowledges_while_owner_claim_remains_active(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}"
    owner = SQLAlchemyOpenSandboxState(engine=sql_engine(url))
    observer = SQLAlchemyOpenSandboxState(engine=sql_engine(url))
    await owner.start(warm_pool_size=0)
    await observer.start(warm_pool_size=0)
    claim = await owner.acquire_owner("owner")
    try:
        await owner.bind_owner(claim, "sandbox", purpose="commands")
        running = await owner.register_holder(claim, "observer")
        draining = await owner.change_availability(claim, running, phase="draining")
        assert await observer.read_availability("owner") == draining
        assert (await observer.get_holder_updates("observer"))[
            0
        ].availability == draining
        assert await observer.acknowledge_idle("observer", draining)
        assert await owner.holders_are_idle(claim, draining)
    finally:
        await owner.release_owner(claim)
        await asyncio.gather(owner.aclose(), observer.aclose())


async def test_sqlite_expired_claim_cannot_dispatch_or_cancel_drain(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fence.db'}")
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        running = await state.read_availability("owner")
        assert running is not None
        draining = await state.change_availability(claim, running, phase="draining")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_opensandbox_owners SET lease_expires_at = '2000-01-01 00:00:00'"
                )
            )
        for phase in ("pausing", "running"):
            with pytest.raises(OpenSandboxStateOwnershipError):
                await state.change_availability(claim, draining, phase=phase)
    finally:
        await state.release_owner(claim)
        await state.aclose()
        await engine.dispose()


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_schema_contains_complete_availability_and_holder_tables(
    dialect: Literal["mysql", "sqlite"],
) -> None:
    schema = get_sqlalchemy_opensandbox_state_schema(dialect=dialect)
    assert "tinkerfin_opensandbox_availability" in schema.table_names
    assert "tinkerfin_opensandbox_holders" in schema.table_names
    assert "PRIMARY KEY (namespace, holder_id, owner_digest)" in schema.ddl
    assert "ix_tinkerfin_opensandbox_holders_binding" in schema.ddl
    assert "acknowledged_sequence BIGINT" in schema.ddl


async def test_sqlite_rejects_missing_holder_index(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}")
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    await state.aclose()
    async with engine.begin() as connection:
        await connection.execute(
            text("DROP INDEX ix_tinkerfin_opensandbox_holders_binding")
        )
    reopened = SQLAlchemyOpenSandboxState(engine=engine)
    try:
        with pytest.raises(OpenSandboxStateError, match="indexes differ"):
            await reopened.start(warm_pool_size=0)
    finally:
        await reopened.aclose()
        await engine.dispose()


async def test_sqlite_missing_availability_fails_closed_for_an_existing_binding(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'missing-intent.db'}"
    )
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox", purpose="commands")
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tinkerfin_opensandbox_availability")
            )
        with pytest.raises(OpenSandboxStateError, match="inconsistent availability"):
            await state.read_availability("owner")
    finally:
        await state.release_owner(claim)
        await state.aclose()
        await engine.dispose()


async def test_custom_state_availability_failures_keep_public_error_boundary() -> None:
    class FailingState(InMemoryOpenSandboxState):
        async def read_availability(
            self, owner_key: str
        ) -> OpenSandboxAvailability | None:
            raise RuntimeError("private storage failure")

    boundary = _OpenSandboxStateBoundary(FailingState())
    with pytest.raises(UnexpectedOpenSandboxStateError) as captured:
        await boundary.read_availability("owner")
    assert isinstance(captured.value.__cause__, RuntimeError)


async def test_sqlite_binding_and_availability_commit_or_rollback_together(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'atomic-binding.db'}"
    )
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=1)
    claim = await state.acquire_owner("owner")
    try:
        warm = await state.claim_warm_slot()
        assert warm is not None
        await state.publish_warm(warm, "warm-sandbox")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_availability BEFORE INSERT ON "
                    "tinkerfin_opensandbox_availability BEGIN "
                    "SELECT RAISE(ABORT, 'availability storage unavailable'); END"
                )
            )
        with pytest.raises(OpenSandboxStateError):
            await state.bind_owner(claim, "sandbox", purpose="commands")
        assert await state.read_binding("owner") is None
        with pytest.raises(OpenSandboxStateError):
            await state.consume_warm(claim)
        assert await state.read_binding("owner") is None
        assert await state.warm_pool_ready()
        async with engine.begin() as connection:
            await connection.execute(text("DROP TRIGGER reject_availability"))
        binding = await state.consume_warm(claim)
        assert binding is not None
        running = await state.register_holder(claim, "manager")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_availability_removal BEFORE DELETE ON "
                    "tinkerfin_opensandbox_availability BEGIN "
                    "SELECT RAISE(ABORT, 'availability removal unavailable'); END"
                )
            )
        with pytest.raises(OpenSandboxStateError):
            await state.unbind_owner(claim)
        assert await state.read_binding("owner") == binding
        assert await state.read_availability("owner") == running
        assert len(await state.get_holder_updates("manager")) == 1
    finally:
        await state.release_owner(claim)
        await state.aclose()
        await engine.dispose()


async def test_unbinding_retires_all_holder_generations_without_touching_other_owners(
    state: OpenSandboxState,
) -> None:
    for owner, sandbox, holder in [
        ("owner", "old-sandbox", "lost-holder"),
        ("other", "other-sandbox", "other-holder"),
        ("owner", "new-sandbox", "active-holder"),
    ]:
        claim = await state.acquire_owner(owner)
        try:
            await state.bind_owner(claim, sandbox, purpose="commands")
            await state.register_holder(claim, holder)
        finally:
            await state.release_owner(claim)
    claim = await state.acquire_owner("owner")
    try:
        await state.unbind_owner(claim)
    finally:
        await state.release_owner(claim)
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "future-sandbox", purpose="commands")
    finally:
        await state.release_owner(claim)
    assert await state.get_holder_updates("lost-holder") == ()
    assert await state.get_holder_updates("active-holder") == ()
    assert len(await state.get_holder_updates("other-holder")) == 1
