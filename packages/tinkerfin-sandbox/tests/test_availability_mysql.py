"""Real MySQL availability coordination using only fixture-owned databases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox.lifecycle.availability import OpenSandboxAvailability

pytestmark = [pytest.mark.docker_integration, pytest.mark.mysql_integration]


async def test_mysql_availability_service_identity(
    mysql_sandbox_url: str,
    docker_test_run_id: str,
    record_property: Callable[[str, object], None],
) -> None:
    """Record the actual test service version and cleanup ownership identity."""
    engine = create_async_engine(mysql_sandbox_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT VERSION(), @@hostname, @@transaction_isolation, DATABASE()"
                    )
                )
            ).one()
        assert str(row[0]).startswith("8.4.")
        assert str(row[3]).startswith("tinkerfin_sandbox_")
        record_property("mysql_version", str(row[0]))
        record_property("mysql_hostname", str(row[1]))
        record_property("transaction_isolation", str(row[2]))
        record_property("docker_test_run_id", docker_test_run_id)
    finally:
        await engine.dispose()


async def test_mysql_all_holders_acknowledge_while_pause_claim_is_held(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    owner = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    observer = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await owner.start(warm_pool_size=0)
    await observer.start(warm_pool_size=0)
    claim = await owner.acquire_owner("owner")
    try:
        await owner.bind_owner(claim, "sandbox")
        running = await owner.register_holder(claim, "manager-a")
        await observer.register_holder(claim, "manager-b")
        draining = await owner.change_availability(claim, running, phase="draining")
        await owner.bind_owner(claim, "sandbox")
        assert await observer.read_availability("owner") == draining
        with pytest.raises(OpenSandboxStateOwnershipError):
            await observer.register_holder(claim, "late-manager")
        assert await observer.read_availability("owner") == draining
        updates = await observer.get_holder_updates("manager-b")
        assert updates[0].availability == draining
        assert not await owner.holders_are_idle(claim, draining)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await owner.change_availability(claim, draining, phase="pausing")
        assert await observer.acknowledge_idle("manager-b", draining)
        assert await observer.acknowledge_idle("manager-b", draining)
        assert not await owner.holders_are_idle(claim, draining)
        assert await owner.acknowledge_idle("manager-a", draining)
        assert await owner.holders_are_idle(claim, draining)
        pausing = await owner.change_availability(claim, draining, phase="pausing")
        paused = await owner.change_availability(claim, pausing, phase="paused")
        assert await observer.read_availability("owner") == paused
    finally:
        await owner.release_owner(claim)
        await asyncio.gather(owner.aclose(), observer.aclose())


async def test_mysql_stale_intents_and_binding_releases_cannot_affect_successors(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    second = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await first.start(warm_pool_size=0)
    await second.start(warm_pool_size=0)
    claim = await first.acquire_owner("owner")
    current_claim = claim
    try:
        await first.bind_owner(claim, "same-id")
        running = await first.register_holder(claim, "manager")
        old_drain = await first.change_availability(claim, running, phase="draining")
        assert await second.acknowledge_idle("manager", old_drain)
        restored = await first.change_availability(claim, old_drain, phase="running")
        current_drain = await first.change_availability(
            claim, restored, phase="draining"
        )
        assert not await second.acknowledge_idle("manager", old_drain)
        assert not await first.holders_are_idle(claim, current_drain)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await second.change_availability(claim, old_drain, phase="running")
        with pytest.raises(OpenSandboxStateOwnershipError):
            await second.change_availability(
                claim, replace(current_drain, connection_generation=1), phase="running"
            )
        await first.release_owner(claim)
        current_claim = await second.acquire_owner("owner")
        await second.bind_owner(current_claim, "same-id")
        current = await second.register_holder(current_claim, "manager")
        assert current.binding_generation != running.binding_generation
        await first.unregister_holder("manager", running)
        updates = await first.get_holder_updates("manager")
        assert len(updates) == 1
        assert updates[0].binding_generation == current.binding_generation
        assert not await first.acknowledge_idle("manager", current_drain)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await first.unbind_owner(claim)
        new_drain = await second.change_availability(
            current_claim, current, phase="draining"
        )
        assert not await second.holders_are_idle(current_claim, new_drain)
        assert await first.acknowledge_idle("manager", new_drain)
        pausing = await second.change_availability(
            current_claim, new_drain, phase="pausing"
        )
        paused = await second.change_availability(
            current_claim, pausing, phase="paused"
        )
        resuming = await second.change_availability(
            current_claim, paused, phase="resuming"
        )
        resumed = await second.change_availability(
            current_claim, resuming, phase="running", refresh_connection=True
        )
        assert resumed.connection_generation == 1
        assert (await first.get_holder_updates("manager"))[0].availability == resumed
    finally:
        await second.release_owner(current_claim)
        await asyncio.gather(first.aclose(), second.aclose())


@pytest.mark.parametrize("register_first", [True, False])
async def test_mysql_registration_and_drain_are_atomic_across_states(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
    register_first: bool,
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    second = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await first.start(warm_pool_size=0)
    await second.start(warm_pool_size=0)
    try:
        for index in range(4):
            claim = await first.acquire_owner(f"owner-{index}")
            try:
                await first.bind_owner(claim, f"sandbox-{index}")
                running = await first.read_availability(f"owner-{index}")
                assert running is not None
                register = second.register_holder(claim, f"manager-{index}")
                drain = first.change_availability(claim, running, phase="draining")
                results = await asyncio.gather(
                    *((register, drain) if register_first else (drain, register)),
                    return_exceptions=True,
                )
                registered, drained = results if register_first else reversed(results)
                assert isinstance(drained, OpenSandboxAvailability)
                if isinstance(registered, OpenSandboxStateOwnershipError):
                    assert await second.get_holder_updates(f"manager-{index}") == ()
                    assert await first.holders_are_idle(claim, drained)
                else:
                    assert registered == running
                    assert not await first.holders_are_idle(claim, drained)
            finally:
                await first.release_owner(claim)
    finally:
        await asyncio.gather(first.aclose(), second.aclose())


async def test_mysql_reopen_retains_unconfirmed_holders_after_worker_closes(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    first = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await first.start(warm_pool_size=0)
    claim = await first.acquire_owner("owner")
    try:
        await first.bind_owner(claim, "sandbox")
        running = await first.register_holder(claim, "lost-manager")
    finally:
        await first.release_owner(claim)
        await first.aclose()
    reopened = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await reopened.start(warm_pool_size=0)
    current = await reopened.acquire_owner("owner")
    try:
        assert await reopened.read_availability("owner") == running
        draining = await reopened.change_availability(
            current, running, phase="draining"
        )
        assert not await reopened.holders_are_idle(current, draining)
        assert (await reopened.get_holder_updates("lost-manager"))[
            0
        ].acknowledged_sequence is None
        with pytest.raises(OpenSandboxStateOwnershipError):
            await reopened.change_availability(current, draining, phase="pausing")
    finally:
        await reopened.release_owner(current)
        await reopened.aclose()


async def test_mysql_binding_warm_consumption_and_unbinding_are_atomic(
    mysql_sandbox_url: str,
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=1)
    claim = await state.acquire_owner("owner")
    try:
        warm = await state.claim_warm_slot()
        assert warm is not None
        await state.publish_warm(warm, "warm-sandbox")
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TRIGGER reject_availability BEFORE INSERT ON "
                "tinkerfin_opensandbox_availability FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'availability unavailable'"
            )
        with pytest.raises(OpenSandboxStateError):
            await state.bind_owner(claim, "sandbox")
        assert await state.read_binding("owner") is None
        with pytest.raises(OpenSandboxStateError):
            await state.consume_warm(claim)
        assert await state.read_binding("owner") is None
        assert await state.warm_pool_ready()
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER reject_availability")
        binding = await state.consume_warm(claim)
        assert binding is not None
        running = await state.register_holder(claim, "manager")
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TRIGGER reject_availability_removal BEFORE DELETE ON "
                "tinkerfin_opensandbox_availability FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'availability removal unavailable'"
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


async def test_mysql_expired_owner_claim_cannot_dispatch_or_cancel(
    mysql_sandbox_url: str,
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    state = SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("owner")
    try:
        await state.bind_owner(claim, "sandbox")
        running = await state.read_availability("owner")
        assert running is not None
        draining = await state.change_availability(claim, running, phase="draining")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_opensandbox_owners "
                    "SET lease_expires_at = '2000-01-01 00:00:00'"
                )
            )
        for phase in ("pausing", "running"):
            with pytest.raises(OpenSandboxStateOwnershipError):
                await state.change_availability(claim, draining, phase=phase)
        assert await state.read_availability("owner") == draining
    finally:
        await state.release_owner(claim)
        await state.aclose()
        await engine.dispose()


async def test_mysql_distinct_owners_publish_availability_concurrently(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    """Unrelated bindings can initialize an empty availability table in parallel."""
    first = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    second = SQLAlchemyOpenSandboxState(engine=sql_engine(mysql_sandbox_url))
    await first.start(warm_pool_size=0)
    await second.start(warm_pool_size=0)
    try:
        for index in range(8):
            first_claim = await first.acquire_owner(f"first-{index}")
            second_claim = await second.acquire_owner(f"second-{index}")
            try:
                bindings = await asyncio.gather(
                    first.bind_owner(first_claim, f"first-sandbox-{index}"),
                    second.bind_owner(second_claim, f"second-sandbox-{index}"),
                )
                assert bindings[0].sandbox_id == f"first-sandbox-{index}"
                assert bindings[1].sandbox_id == f"second-sandbox-{index}"
                assert (
                    await first.register_holder(first_claim, "first-manager")
                ).phase == "running"
                assert (
                    await second.register_holder(second_claim, "second-manager")
                ).phase == "running"
            finally:
                await asyncio.gather(
                    first.release_owner(first_claim), second.release_owner(second_claim)
                )
    finally:
        await asyncio.gather(first.aclose(), second.aclose())


async def test_mysql_namespaces_keep_holder_registration_and_drain_independent(
    sql_engine: SqlEngineFactory,
    mysql_sandbox_url: str,
) -> None:
    first = SQLAlchemyOpenSandboxState(
        engine=sql_engine(mysql_sandbox_url), namespace="namespace-a"
    )
    second = SQLAlchemyOpenSandboxState(
        engine=sql_engine(mysql_sandbox_url), namespace="namespace-b"
    )
    await first.start(warm_pool_size=0)
    await second.start(warm_pool_size=0)
    first_claim = await first.acquire_owner("same-owner")
    second_claim = await second.acquire_owner("same-owner")
    try:
        await first.bind_owner(first_claim, "same-sandbox-id")
        await second.bind_owner(second_claim, "same-sandbox-id")
        first_running = await first.register_holder(first_claim, "same-manager")
        second_running = await second.register_holder(second_claim, "same-manager")
        draining = await first.change_availability(
            first_claim, first_running, phase="draining"
        )
        assert not await second.acknowledge_idle("same-manager", draining)
        await second.unregister_holder("same-manager", first_running)
        assert (await second.get_holder_updates("same-manager"))[
            0
        ].availability == second_running
        assert (
            await second.register_holder(second_claim, "another-manager")
            == second_running
        )
        assert not await first.holders_are_idle(first_claim, draining)
    finally:
        await first.release_owner(first_claim)
        await second.release_owner(second_claim)
        await asyncio.gather(first.aclose(), second.aclose())
