"""Verify the same persistent Sandbox guarantees on each supported SQL database."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.support.sql_faults import after_sql_commit

from tinkerfin_sandbox import (
    OpenSandboxStateCommitUncertainError,
    OpenSandboxStateConfigurationError,
    OpenSandboxStateError,
    SQLAlchemyOpenSandboxState,
    UnexpectedOpenSandboxStateError,
)


async def test_sql_binding_availability_warm_pool_and_cleanup_survive_restart(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    engine = sandbox_sql_engine
    first = SQLAlchemyOpenSandboxState(engine=engine, namespace="service")
    peer = SQLAlchemyOpenSandboxState(engine=engine, namespace="service")
    try:
        await asyncio.gather(
            first.start(warm_pool_size=1), peer.start(warm_pool_size=1)
        )
        warm_claims = await asyncio.gather(
            first.claim_warm_slot(), peer.claim_warm_slot()
        )
        occupied = [claim for claim in warm_claims if claim is not None]
        assert len(occupied) == 1
        producer = first if warm_claims[0] is not None else peer
        await producer.publish_warm(occupied[0], "shared-sandbox")
        claim = await first.acquire_owner("projects/project-1")
        binding = await first.consume_warm(claim)
        assert binding is not None and binding.sandbox_id == "shared-sandbox"
        holder = str(uuid4())
        running = await first.register_holder(claim, holder)
        draining = await first.change_availability(claim, running, phase="draining")
        assert not await first.holders_are_idle(claim, draining)
        assert await peer.acknowledge_idle(holder, draining)
        assert await first.holders_are_idle(claim, draining)
        await first.change_availability(claim, draining, phase="pausing")
        current = await peer.read_availability("projects/project-1")
        assert current is not None and current.phase == "pausing"
        await first.release_owner(claim)
        await asyncio.gather(
            first.enqueue_cleanup("orphan"), peer.enqueue_cleanup("orphan")
        )
        cleanup_claims = await asyncio.gather(
            first.claim_cleanup(), peer.claim_cleanup()
        )
        cleanup = [claim for claim in cleanup_claims if claim is not None]
        assert len(cleanup) == 1 and cleanup[0].sandbox_id == "orphan"
        await first.release_cleanup(cleanup[0])
    finally:
        await asyncio.gather(first.aclose(), peer.aclose())
    reopened = SQLAlchemyOpenSandboxState(engine=engine, namespace="service")
    try:
        await reopened.start(warm_pool_size=1)
        assert await reopened.read_binding("projects/project-1") == binding
        recovered = await reopened.read_availability("projects/project-1")
        assert recovered == current
        target = await reopened.claim_cleanup()
        assert target is not None and target.sandbox_id == "orphan"
        await reopened.complete_cleanup(target)
        assert await reopened.claim_cleanup() is None
    finally:
        await reopened.aclose()
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_opensandbox_workers")
            )
            == 0
        )


async def test_sql_state_capacity_conflict_can_retry_after_competing_worker_closes(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    second = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    try:
        await first.start(warm_pool_size=1)
        with pytest.raises(OpenSandboxStateConfigurationError):
            await second.start(warm_pool_size=2)
        await first.aclose()
        await second.start(warm_pool_size=2)
        claims = await asyncio.gather(
            second.claim_warm_slot(), second.claim_warm_slot()
        )
        assert all(claim is not None for claim in claims)
        assert claims[0] != claims[1]
    finally:
        await asyncio.gather(first.aclose(), second.aclose())


async def test_sql_state_unknown_commit_never_replays_a_binding(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    try:
        await state.start(warm_pool_size=0)
        claim = await state.acquire_owner("user")
        attempts = 0
        error = OperationalError("COMMIT", None, RuntimeError("acknowledgement lost"))

        async def unknown() -> None:
            nonlocal attempts
            attempts += 1
            raise error

        with after_sql_commit(sandbox_sql_engine, unknown):
            with pytest.raises(OpenSandboxStateCommitUncertainError) as captured:
                await state.bind_owner(claim, "sandbox", purpose="commands")
        assert attempts == 1 and captured.value.cause is error
        binding = await state.read_binding("user")
        availability = await state.read_availability("user")
        assert binding is not None and binding.sandbox_id == "sandbox"
        assert (
            availability is not None
            and availability.binding_generation == binding.generation
        )
    finally:
        await state.aclose()


async def test_sql_cancelled_start_waits_for_registration_and_close_removes_it(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def committed() -> None:
        reached.set()
        await release.wait()

    with after_sql_commit(sandbox_sql_engine, committed):
        starting = asyncio.create_task(state.start(warm_pool_size=1))
        try:
            await reached.wait()
            starting.cancel("caller stopped")
            release.set()
            with pytest.raises(asyncio.CancelledError, match="caller stopped"):
                await starting
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
    await state.aclose()
    await state.aclose()
    async with sandbox_sql_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_opensandbox_workers")
            )
            == 0
        )
    peer = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    try:
        await peer.start(warm_pool_size=2)
    finally:
        await peer.aclose()


async def test_sql_confirmed_start_cleanup_failure_does_not_repeat_registration(
    sandbox_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SQLAlchemyOpenSandboxState(engine=sandbox_sql_engine)
    close = AsyncConnection.close
    failures = 0

    async def fail_once(connection: AsyncConnection) -> None:
        nonlocal failures
        await close(connection)
        if connection.engine is sandbox_sql_engine and failures == 0:
            failures += 1
            raise OperationalError("close", None, RuntimeError("pool close failed"))

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "close", fail_once)
        with pytest.raises(UnexpectedOpenSandboxStateError, match="settlement failed"):
            await state.start(warm_pool_size=1)
    async with sandbox_sql_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_opensandbox_workers")
            )
            == 1
        )
    await state.aclose()
    async with sandbox_sql_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_opensandbox_workers")
            )
            == 0
        )


async def test_sql_state_rejects_a_time_column_that_discards_lease_precision(
    sandbox_sql_engine: AsyncEngine,
) -> None:
    engine = sandbox_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite DateTime storage has no declared fractional precision")
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    await state.aclose()
    async with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_opensandbox_owners MODIFY COLUMN "
                "lease_expires_at DATETIME NULL "
                "COMMENT 'UTC expiry of the current owner transition lease'"
            )
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_opensandbox_owners ALTER COLUMN "
                "lease_expires_at TYPE TIMESTAMP(0) WITHOUT TIME ZONE"
            )
    invalid = SQLAlchemyOpenSandboxState(engine=engine)
    try:
        with pytest.raises(OpenSandboxStateError, match="incompatible type"):
            await invalid.start(warm_pool_size=0)
    finally:
        await invalid.aclose()
