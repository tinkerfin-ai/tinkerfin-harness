"""Shared observable Trace Store contract for in-memory and SQLite implementations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    RunFact,
    SqlAlchemyTraceStore,
    TraceLimits,
    TraceProjectionCheckpoint,
    TraceStore,
    TraceStoreOptions,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)


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
