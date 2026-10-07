"""Shared observable Trace Store contract for in-memory and SQLite implementations."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from tests.support.sql_faults import after_sql_command

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceLimits,
    TraceProjectionCheckpoint,
    TraceStore,
    TraceStoreOptions,
    TraceThreadKey,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.sql_store import _SqlAlchemyTraceLedgerBackend


def _identity(run_id: str = "run-contract") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-contract", run_id=run_id)


def _fact(
    run_id: str,
    phase: Literal["started", "terminal", "closed"],
) -> RunFact:
    return RunFact(
        source_observation_id=f"contract-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase in {"terminal", "closed"} else None,
    )


async def _exercise_store_contract(store: TraceStore) -> None:
    assert isinstance(store, TraceStore)

    empty = await store.open_writer(_identity())
    empty_generation = empty.key.generation
    await asyncio.gather(empty.aclose(), empty.aclose())
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(_identity().thread)

    first = await store.open_writer(_identity())
    second = await store.open_writer(_identity("run-second"))
    assert first.key.generation != empty_generation
    assert first.key == second.key
    with pytest.raises(TraceRunConflict):
        await store.open_writer(_identity())

    first_events, second_events = await asyncio.gather(
        first.append((_fact("run-contract", "started"),)),
        second.append((_fact("run-second", "started"),)),
    )
    assert {first_events[0].trace_seq, second_events[0].trace_seq} == {1, 2}
    snapshot = await store.snapshot(_identity().thread)
    assert snapshot.active_run_ids == ("run-contract", "run-second")

    await first.append((_fact("run-contract", "terminal"),), mandatory=True)
    with pytest.raises(TraceStoreProtocolError, match="cannot follow"):
        await first.append((_fact("run-contract", "started"),))
    await first.append((_fact("run-contract", "closed"),), mandatory=True)
    with pytest.raises(TraceStoreProtocolError, match="already committed"):
        await first.append((_fact("run-contract", "closed"),), mandatory=True)
    await first.aclose()
    await second.append(
        (
            _fact("run-second", "terminal"),
            _fact("run-second", "closed"),
        ),
        mandatory=True,
    )
    await second.aclose()

    snapshot = await store.snapshot(_identity().thread)
    assert snapshot.as_of_seq == 6
    assert snapshot.active_writers == ()
    assert [
        event.trace_seq
        for event in await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
    ] == [1, 2, 3, 4, 5, 6]
    assert [
        event.trace_seq
        for event in await store.read_events_reverse(
            snapshot.key,
            before_seq=7,
            limit=2,
        )
    ] == [6, 5]

    with pytest.raises(ValueError):
        await store.read_events(snapshot.key, after_seq=-1, as_of_seq=6, limit=1)
    with pytest.raises(ValueError):
        await store.read_events_reverse(snapshot.key, before_seq=8, limit=1)
    wrong_namespace = snapshot.key.model_copy(update={"namespace": "another"})
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot_key(wrong_namespace)

    checkpoint = TraceProjectionCheckpoint(
        key=snapshot.key,
        projection_name="contract.projection",
        run_id="run-contract",
        as_of_seq=4,
        state={"count": 4},
    )
    assert (
        await store.save_projection_checkpoint(
            checkpoint,
            expected_as_of_seq=None,
        )
        == checkpoint
    )
    # An exact retry proves an unknown commit instead of creating another row.
    assert (
        await store.save_projection_checkpoint(
            checkpoint.model_copy(deep=True),
            expected_as_of_seq=None,
        )
        == checkpoint
    )
    with pytest.raises(TraceStoreProtocolError, match="idempotency"):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"state": {"count": 5}}),
            expected_as_of_seq=None,
        )
    with pytest.raises(TraceProjectionCheckpointConflict):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"as_of_seq": 5}),
            expected_as_of_seq=None,
        )
    with pytest.raises(TraceStoreProtocolError, match="committed Trace prefix"):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"as_of_seq": 7}),
            expected_as_of_seq=4,
        )

    await store.delete(snapshot.key)
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot_key(snapshot.key)
    replacement = await store.open_writer(_identity())
    assert replacement.key.generation != snapshot.key.generation
    await replacement.aclose()


async def test_in_memory_store_satisfies_shared_contract() -> None:
    await _exercise_store_contract(InMemoryTraceStore())


async def _exercise_checkpoint_retention(store: TraceStore) -> None:
    writer = await store.open_writer(_identity())
    try:
        events = await writer.append((_fact("run-contract", "started"),))
        events += await writer.append(
            (
                _fact("run-contract", "terminal"),
                _fact("run-contract", "closed"),
            ),
            mandatory=True,
        )
        for scope in (None, "run-contract"):
            for sequence in (1, 2, 3):
                await store.save_projection_checkpoint(
                    TraceProjectionCheckpoint(
                        key=writer.key,
                        projection_name="retained",
                        run_id=scope,
                        as_of_seq=sequence,
                        state={"count": sequence},
                    ),
                    expected_as_of_seq=None if sequence == 1 else sequence - 1,
                )
            assert (
                await store.load_projection_checkpoint(
                    writer.key, projection_name="retained", run_id=scope, as_of_seq=1
                )
                is None
            )
            retained = await store.load_projection_checkpoint(
                writer.key, projection_name="retained", run_id=scope, as_of_seq=2
            )
            assert retained is not None and retained.state == {"count": 2}
            current = await store.load_projection_checkpoint(
                writer.key, projection_name="retained", run_id=scope, as_of_seq=3
            )
            assert current is not None and current.state == {"count": 3}
            assert (
                await store.save_projection_checkpoint(current, expected_as_of_seq=2)
                == current
            )
        assert (
            await store.read_events(writer.key, after_seq=0, as_of_seq=3, limit=3)
            == events
        )
    finally:
        await writer.aclose()


async def test_in_memory_retains_bounded_checkpoint_history_and_all_events() -> None:
    await _exercise_checkpoint_retention(
        InMemoryTraceStore(limits=TraceLimits(max_projection_checkpoints_per_scope=2))
    )


async def test_sql_retains_bounded_checkpoint_history_and_all_events(
    trace_sql_engine: AsyncEngine,
) -> None:
    await _exercise_checkpoint_retention(
        SqlAlchemyTraceStore(
            trace_sql_engine, limits=TraceLimits(max_projection_checkpoints_per_scope=2)
        )
    )


async def test_sql_store_satisfies_shared_contract(
    trace_sql_engine: AsyncEngine,
) -> None:
    await _exercise_store_contract(SqlAlchemyTraceStore(trace_sql_engine))


async def test_sql_backend_satisfies_cross_instance_verifier(
    trace_sql_engine: AsyncEngine,
) -> None:
    options = TraceStoreOptions(
        commit_retry_attempts=15, commit_retry_delay_seconds=0.02
    )
    primary = SqlAlchemyTraceStore(trace_sql_engine, options=options)
    peer = SqlAlchemyTraceStore(trace_sql_engine, options=options)
    await verify_trace_ledger_backend(
        primary.backend, peer.backend, namespace="backend-contract", options=options
    )


async def test_sqlite_backend_satisfies_public_cross_instance_verifier(
    tmp_path: Path,
) -> None:
    database = tmp_path / "backend-contract.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    options = TraceStoreOptions(
        commit_retry_attempts=15, commit_retry_delay_seconds=0.05
    )
    primary = _SqlAlchemyTraceLedgerBackend(first_engine, options=options)
    peer = _SqlAlchemyTraceLedgerBackend(second_engine, options=options)
    try:
        await verify_trace_ledger_backend(
            primary,
            peer,
            namespace="sqlite-backend-contract",
            options=options,
        )
    finally:
        try:
            await first_engine.dispose()
        finally:
            await second_engine.dispose()


async def test_sqlite_concurrent_schema_first_start_uses_one_current_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "concurrent-setup.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    first = SqlAlchemyTraceStore(first_engine)
    second = SqlAlchemyTraceStore(second_engine)
    schema_locked = asyncio.Event()
    release_schema = asyncio.Event()
    retry_started = asyncio.Event()
    first_prepared = asyncio.Event()

    async def hold_schema() -> None:
        schema_locked.set()
        await release_schema.wait()

    async def wait_for_prepared_schema(_delay: float) -> None:
        retry_started.set()
        await first_prepared.wait()

    execute = AsyncConnection.exec_driver_sql

    async def execute_without_peer_lock_wait(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> CursorResult[Any]:
        if connection.engine is second_engine and statement == "BEGIN IMMEDIATE":
            await execute(connection, "PRAGMA busy_timeout = 0")
        return await execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(
        AsyncConnection, "exec_driver_sql", execute_without_peer_lock_wait
    )
    controlled_asyncio = SimpleNamespace(**vars(asyncio))
    controlled_asyncio.sleep = wait_for_prepared_schema
    monkeypatch.setattr("tinkerfin_tracing.sql_store.asyncio", controlled_asyncio)
    try:
        with after_sql_command(first_engine, "BEGIN IMMEDIATE", hold_schema):
            first_setup = asyncio.create_task(first.setup())
            second_setup: asyncio.Task[None] | None = None
            try:
                await schema_locked.wait()
                second_setup = asyncio.create_task(second.setup())
                await retry_started.wait()
                assert not first_setup.done()
                assert not second_setup.done()
                release_schema.set()
                await first_setup
                first_prepared.set()
                await second_setup
            finally:
                release_schema.set()
                first_prepared.set()
                setup_tasks = [first_setup]
                if second_setup is not None:
                    setup_tasks.append(second_setup)
                for setup_task in setup_tasks:
                    if not setup_task.done():
                        setup_task.cancel()
                await asyncio.gather(*setup_tasks, return_exceptions=True)
        async with AsyncExitStack() as writers:
            first_writer = await first.open_writer(_identity("first"))
            writers.push_async_callback(first_writer.aclose)
            second_writer = await second.open_writer(_identity("second"))
            writers.push_async_callback(second_writer.aclose)
            assert first_writer.key == second_writer.key
    finally:
        try:
            await first_engine.dispose()
        finally:
            await second_engine.dispose()


def test_trace_store_key_is_the_only_generation_handle() -> None:
    """Keep the shared contract free of SQL connection or third-party models."""

    assert set(TraceThreadKey.model_fields) == {"namespace", "thread_id", "generation"}
