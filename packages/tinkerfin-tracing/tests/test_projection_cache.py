"""Custom Projection prefixes, isolated state, and query-owned cancellation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    CapturedValue,
    InMemoryTraceStore,
    MessageFact,
    RunFact,
    SqlAlchemyTraceStore,
    TraceEvent,
    TraceLimits,
    TraceProjectionCheckpoint,
    TraceProjectionFailed,
    Tracer,
    TraceSemanticFact,
    TraceStore,
    TraceThread,
    TraceThreadKey,
    TraceThreadNotFound,
    TraceWriter,
    TurnFact,
)

_NOW = datetime(2026, 9, 27, tzinfo=UTC)
_THREAD = ThreadIdentity(namespace="test", thread_id="projection-cache")


class _State(BaseModel):
    observations: list[str] = Field(default_factory=list)
    values: list[dict[str, str]] = Field(default_factory=list)


class _Projection:
    name = "business-history"
    state_type = _State
    result_type = _State

    def __init__(self) -> None:
        self.applied: list[str] = []

    def initial_state(self) -> _State:
        return _State()

    def apply(self, state: _State, fact: TraceSemanticFact) -> _State:
        self.applied.append(fact.source_observation_id)
        state.observations.append(fact.source_observation_id)
        if isinstance(fact, MessageFact) and fact.content is not None:
            value = fact.content.value
            assert isinstance(value, dict)
            text = value["text"]
            assert isinstance(text, str)
            state.values.append({"text": text})
        return state

    def finish(self, state: _State) -> _State:
        return state


class _Store(InMemoryTraceStore):
    """Expose public read boundaries without consulting disposable custom checkpoints."""

    def __init__(self, *, page_size: int = 256) -> None:
        super().__init__(limits=TraceLimits(follow_batch_size=page_size))
        self.reads: list[tuple[int, int, int]] = []
        self.pause_next_read = False
        self.read_entered = asyncio.Event()
        self.release_read = asyncio.Event()

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int = 1000,
    ) -> tuple[TraceEvent, ...]:
        self.reads.append((after_seq, as_of_seq, limit))
        if self.pause_next_read:
            self.pause_next_read = False
            self.read_entered.set()
            await self.release_read.wait()
        return await super().read_events(
            key, after_seq=after_seq, as_of_seq=as_of_seq, limit=limit
        )

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        assert projection_name == "tinkerfin.core.summary"
        return await super().load_projection_checkpoint(
            key, projection_name=projection_name, run_id=run_id, as_of_seq=as_of_seq
        )

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        assert checkpoint.projection_name == "tinkerfin.core.summary"
        return await super().save_projection_checkpoint(
            checkpoint, expected_as_of_seq=expected_as_of_seq
        )


class _OtherProjection(_Projection):
    name = "other-business-history"


def _identity(run_id: str, thread: ThreadIdentity = _THREAD) -> RunIdentity:
    return RunIdentity(
        namespace=thread.namespace, thread_id=thread.thread_id, run_id=run_id
    )


def _message(identity: RunIdentity, name: str) -> MessageFact:
    value: dict[str, JsonValue] = {"text": name}
    return MessageFact(
        identity=identity,
        source_observation_id=name,
        occurred_at=_NOW,
        monotonic_ns=1,
        phase="completed",
        message_id=name,
        source_message_id=name,
        role="assistant",
        content=CapturedValue(
            disposition="inline",
            value=value,
            safe_size_bytes=len(json.dumps(value, separators=(",", ":")).encode()),
        ),
    )


@asynccontextmanager
async def _run(
    store: TraceStore,
    run_id: str,
    *,
    thread: ThreadIdentity = _THREAD,
    parent: str | None = None,
) -> AsyncIterator[TraceWriter]:
    identity = _identity(run_id, thread)
    writer = await store.open_writer(identity)
    try:
        await writer.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id=f"{run_id}-start",
                    occurred_at=_NOW,
                    monotonic_ns=1,
                    phase="started",
                    input_kind="ordinary",
                    parent_run_id=parent,
                ),
                TurnFact(
                    identity=identity,
                    source_observation_id=f"{run_id}-turn",
                    occurred_at=_NOW,
                    monotonic_ns=1,
                    turn_id=f"turn-{run_id}",
                    user_message_id=f"user-{run_id}",
                ),
                _message(identity, f"{run_id}-answer"),
            )
        )
        yield writer
    finally:
        await writer.aclose()


async def _get(
    tracer: Tracer, projection: _Projection, *, head: str | None = None
) -> TraceThread:
    return await tracer.get(_THREAD, head_run_id=head, projections=(projection.name,))


def _result(trace: TraceThread, projection: _Projection) -> _State:
    value = trace.projections[projection.name]
    assert isinstance(value, _State)
    return value


async def test_queries_reuse_detached_state_without_custom_checkpoint_io() -> None:
    store = _Store(page_size=2)
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        await tracer.get(_THREAD)
        store.reads.clear()
        first = await _get(tracer, projection)
        assert projection.applied == ["root-start", "root-turn", "root-answer"]
        assert store.reads == [(0, 3, 2), (2, 3, 2)]
        exposed = _result(first, projection)
        exposed.observations.clear()
        exposed.values[0]["text"] = "modified"
        projection.applied.clear()
        store.reads.clear()
        again = await _get(tracer, projection)
        assert projection.applied == []
        assert store.reads == []
        assert _result(again, projection).observations == [
            "root-start",
            "root-turn",
            "root-answer",
        ]
        assert _result(again, projection).values == [{"text": "root-answer"}]


@pytest.mark.parametrize("second_seed", ["cold", "intermediate", "current"])
async def test_shared_pages_preserve_each_projections_independent_prefix(
    second_seed: Literal["cold", "intermediate", "current"],
) -> None:
    store = _Store(page_size=2)
    first, second = _Projection(), _OtherProjection()
    tracer = Tracer(store=store, projections=(first, second))
    async with _run(store, "root") as writer:
        await _get(tracer, first)
        await writer.append((_message(_identity("root"), "middle"),))
        if second_seed == "intermediate":
            await _get(tracer, second)
        await writer.append((_message(_identity("root"), "tail"),))
        if second_seed == "current":
            await _get(tracer, second)
        await tracer.get(_THREAD)
        first.applied.clear()
        second.applied.clear()
        store.reads.clear()
        result = await tracer.get(_THREAD, projections=(first.name, second.name))
        assert _result(result, first) == _result(result, second)
        assert _result(result, first).observations == [
            "root-start",
            "root-turn",
            "root-answer",
            "middle",
            "tail",
        ]
        assert first.applied == ["middle", "tail"]
        if second_seed == "cold":
            assert second.applied == _result(result, second).observations
            assert store.reads == [(0, 5, 2), (2, 5, 2), (4, 5, 2)]
        else:
            assert second.applied == (["tail"] if second_seed == "intermediate" else [])
            assert store.reads == [(3, 5, 2)]


async def test_child_query_extends_only_a_complete_parent_prefix() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        await _get(tracer, projection)
        projection.applied.clear()
        async with _run(store, "child", parent="root"):
            child = await _get(tracer, projection)
            assert projection.applied == ["child-start", "child-turn", "child-answer"]
            assert len(_result(child, projection).observations) == 6


@pytest.mark.parametrize("cached_head", ["root", "child"])
async def test_cached_prefix_cannot_skip_facts_before_ancestry_was_bound(
    cached_head: str,
) -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"), _run(store, "child") as child_writer:
        parent = await _get(tracer, projection, head=cached_head)
        assert parent.as_of_seq == 6
        assert _result(parent, projection).observations == [
            f"{cached_head}-start",
            f"{cached_head}-turn",
            f"{cached_head}-answer",
        ]
        await child_writer.append(
            (
                RunFact(
                    identity=_identity("child"),
                    source_observation_id="child-parent-bound",
                    occurred_at=_NOW,
                    monotonic_ns=2,
                    phase="input",
                    input_kind="ordinary",
                    parent_run_id="root",
                    input=CapturedValue(
                        disposition="inline", value={}, safe_size_bytes=2
                    ),
                    config=CapturedValue(
                        disposition="inline", value={}, safe_size_bytes=2
                    ),
                ),
            )
        )
        projection.applied.clear()
        child = await _get(tracer, projection, head="child")
        assert _result(child, projection).observations == [
            "root-start",
            "root-turn",
            "root-answer",
            "child-start",
            "child-turn",
            "child-answer",
            "child-parent-bound",
        ]
        assert projection.applied == _result(child, projection).observations


async def test_historical_run_start_does_not_consume_or_replace_future_state() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        current = await _get(tracer, projection)
        before = await tracer.get(
            _THREAD, projections=(projection.name,), at_run_start=True
        )
        assert before.as_of_seq == 2
        assert _result(before, projection).observations == ["root-start", "root-turn"]
        projection.applied.clear()
        again = await _get(tracer, projection)
        assert projection.applied == []
        assert _result(again, projection) == _result(current, projection)


async def test_history_cursor_replays_its_old_prefix_after_the_head_advances() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"), _run(store, "child", parent="root") as writer:
        old = await tracer.get(_THREAD, projections=(projection.name,), limit=1)
        cursor = old.history_cursor
        assert cursor is not None
        await writer.append((_message(_identity("child"), "new-answer"),))
        current = await _get(tracer, projection)
        expanded = await tracer.get(
            _THREAD, projections=(projection.name,), history_cursor=cursor, limit=1
        )
        assert expanded.as_of_seq == old.as_of_seq
        assert _result(expanded, projection) == _result(old, projection)
        assert expanded.observed_at == old.observed_at
        projection.applied.clear()
        assert _result(await _get(tracer, projection), projection) == _result(
            current, projection
        )
        assert projection.applied == []


async def test_late_older_query_does_not_replace_a_higher_cached_watermark() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root") as writer:
        store.pause_next_read = True
        older = asyncio.create_task(_get(tracer, projection))
        try:
            await store.read_entered.wait()
            await writer.append((_message(_identity("root"), "new-answer"),))
            newer = await _get(tracer, projection)
            store.release_read.set()
            old = await older
            assert old.as_of_seq == 3
            assert newer.as_of_seq == 4
            projection.applied.clear()
            assert _result(await _get(tracer, projection), projection) == _result(
                newer, projection
            )
            assert projection.applied == []
        finally:
            store.release_read.set()
            if not older.done():
                older.cancel()
            await asyncio.gather(older, return_exceptions=True)


async def test_deleted_generation_cannot_reuse_a_projection_state() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        old = await _get(tracer, projection)
    await old.delete()
    with pytest.raises(TraceThreadNotFound):
        await _get(tracer, projection)
    async with _run(store, "root"):
        projection.applied.clear()
        replacement = await _get(tracer, projection)
        assert replacement.key.generation != old.key.generation
        assert projection.applied == ["root-start", "root-turn", "root-answer"]


async def _evict_history(tracer: Tracer, store: InMemoryTraceStore) -> None:
    for index in range(64):
        thread = ThreadIdentity(namespace="test", thread_id=f"other-{index}")
        async with _run(store, "other", thread=thread):
            await tracer.get(thread, projections=(_Projection.name,))


async def test_evicted_history_replays_and_independent_tracers_do_not_share_states() -> (
    None
):
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        original = await _get(tracer, projection)
        independent = _Projection()
        other = Tracer(store=store, projections=(independent,))
        assert _result(await _get(other, independent), independent) == _result(
            original, projection
        )
        assert independent.applied == ["root-start", "root-turn", "root-answer"]
        await _evict_history(tracer, store)
        projection.applied.clear()
        rebuilt = await _get(tracer, projection)
        assert _result(rebuilt, projection) == _result(original, projection)
        assert projection.applied == ["root-start", "root-turn", "root-answer"]


class _PaddedState(BaseModel):
    count: int = 0
    padding: str = ""


class _PaddedProjection:
    name = "large-business-state"
    state_type = _PaddedState
    result_type = _PaddedState

    def __init__(self) -> None:
        self.calls = 0
        # The canonical body fits by eight bytes, but its identity and lineage
        # do not. Admission must account for both without rejecting the query.
        empty_size = len(_PaddedState().model_dump_json().encode())
        self.padding = "x" * (16 * 1024 * 1024 - empty_size - 8)

    def initial_state(self) -> _PaddedState:
        return _PaddedState(padding=self.padding)

    def apply(self, state: _PaddedState, fact: TraceSemanticFact) -> _PaddedState:
        del fact
        self.calls += 1
        state.count += 1
        return state

    def finish(self, state: _PaddedState) -> _PaddedState:
        return state


async def test_cache_admission_counts_metadata_without_limiting_query_results() -> None:
    store = _Store()
    projection = _PaddedProjection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        for _ in range(2):
            trace = await tracer.get(_THREAD, projections=(projection.name,))
            state = trace.projections[projection.name]
            assert isinstance(state, _PaddedState)
            assert state.count == 3
        assert projection.calls == 6


@pytest.mark.parametrize("interference", ["newer-query", "eviction"])
async def test_follow_keeps_its_own_prefix_when_shared_seeds_are_unusable(
    interference: str,
) -> None:
    store = _Store(page_size=1)
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root") as writer:
        initial = await _get(tracer, projection)
        await writer.append(
            (
                _message(_identity("root"), "next-1"),
                _message(_identity("root"), "next-2"),
            )
        )
        if interference == "newer-query":
            await _get(tracer, projection)
        else:
            await _evict_history(tracer, store)
        projection.applied.clear()
        async with initial.follow() as updates:
            first = await anext(updates)
            assert first.as_of_seq == 4
            assert projection.applied == ["next-1"]
            assert _State.model_validate(
                first.projections[projection.name]
            ).observations == ["root-start", "root-turn", "root-answer", "next-1"]
            second = await anext(updates)
            assert second.as_of_seq == 5
            assert projection.applied == ["next-1", "next-2"]


async def test_follow_advances_over_unselected_runs_without_reapplying_old_facts() -> (
    None
):
    store = _Store(page_size=1)
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root") as writer:
        initial = await _get(tracer, projection, head="root")
        async with _run(store, "sibling"):
            await writer.append((_message(_identity("root"), "next"),))
            projection.applied.clear()
            async with initial.follow() as updates:
                update = await anext(updates)
                assert update.as_of_seq == 7
                assert projection.applied == ["next"]
                assert _State.model_validate(
                    update.projections[projection.name]
                ).observations == ["root-start", "root-turn", "root-answer", "next"]


async def test_follow_refreshes_ownership_without_reapplying_projection_facts() -> None:
    store = _Store()
    projection = _Projection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        initial = await _get(tracer, projection)
    projection.applied.clear()
    async with initial.follow() as updates:
        update = await anext(updates)
        assert update.as_of_seq == initial.as_of_seq
        assert initial.summary.status.execution == "running"
        assert update.summary.status.execution == "unknown"
        assert update.events == ()
        assert projection.applied == []


class _MutatingProjection(_Projection):
    name = "mutating-input"

    def apply(self, state: _State, fact: TraceSemanticFact) -> _State:
        if isinstance(fact, MessageFact) and fact.content is not None:
            value = fact.content.value
            assert isinstance(value, dict)
            value["text"] = "changed-by-extension"
        return super().apply(state, fact)


class _MutatingFinish(_Projection):
    def finish(self, state: _State) -> _State:
        state.observations.append("finish-only")
        return state


async def test_cold_projections_share_pages_without_sharing_extension_inputs() -> None:
    store = _Store(page_size=2)
    first, second = _MutatingProjection(), _Projection()
    tracer = Tracer(store=store, projections=(first, second))
    async with _run(store, "root"):
        await tracer.get(_THREAD)
        store.reads.clear()
        result = await tracer.get(_THREAD, projections=(first.name, second.name))
        assert store.reads == [(0, 3, 2), (2, 3, 2)]
        assert (
            first.applied
            == second.applied
            == [
                "root-start",
                "root-turn",
                "root-answer",
            ]
        )
        assert _result(result, first).values == [{"text": "changed-by-extension"}]
        assert _result(result, second).values == [{"text": "root-answer"}]


async def test_finishing_does_not_mutate_the_state_retained_for_other_queries() -> None:
    store = _Store()
    projection = _MutatingFinish()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        for _ in range(2):
            trace = await _get(tracer, projection)
            assert _result(trace, projection).observations == [
                "root-start",
                "root-turn",
                "root-answer",
                "finish-only",
            ]
        assert projection.applied == ["root-start", "root-turn", "root-answer"]


async def test_follow_extensions_cannot_mutate_each_other_or_public_facts() -> None:
    store = _Store()
    first, second = _MutatingProjection(), _Projection()
    tracer = Tracer(store=store, projections=(first, second))
    async with _run(store, "root") as writer:
        initial = await tracer.get(_THREAD, projections=(first.name, second.name))
        await writer.append((_message(_identity("root"), "original"),))
        store.reads.clear()
        async with initial.follow() as updates:
            update = await anext(updates)
            observed = _State.model_validate(update.projections[second.name])
            assert observed.values[-1] == {"text": "original"}
            fact = update.events[0].fact
            assert isinstance(fact, MessageFact) and fact.content is not None
            assert fact.content.value == {"text": "original"}
            assert store.reads == []


class _ValidatedState(BaseModel):
    count: int = Field(default=0, ge=0)


class _InvalidTransition:
    name = "invalid-transition"
    state_type = _ValidatedState
    result_type = _ValidatedState

    def __init__(self) -> None:
        self.calls = 0

    def initial_state(self) -> _ValidatedState:
        return _ValidatedState()

    def apply(self, state: _ValidatedState, fact: TraceSemanticFact) -> _ValidatedState:
        del fact
        self.calls += 1
        return state.model_copy(update={"count": -1 if self.calls == 1 else 1})

    def finish(self, state: _ValidatedState) -> _ValidatedState:
        return state


async def test_every_transition_is_validated_before_a_later_fact_can_repair_it() -> (
    None
):
    store = _Store()
    projection = _InvalidTransition()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        with pytest.raises(TraceProjectionFailed):
            await tracer.get(_THREAD, projections=(projection.name,))
        assert projection.calls == 1
        await tracer.get(_THREAD, projections=(projection.name,))
        assert projection.calls == 4


class _CancellingProjection(_Projection):
    def apply(self, state: _State, fact: TraceSemanticFact) -> _State:
        result = super().apply(state, fact)
        if len(self.applied) == 1:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return result


async def test_shared_scan_cancellation_discards_each_partial_projection_state() -> (
    None
):
    store = _Store()
    first, second = _OtherProjection(), _CancellingProjection()
    tracer = Tracer(store=store, projections=(first, second))
    names = (first.name, second.name)
    async with _run(store, "root"):
        operation = asyncio.create_task(tracer.get(_THREAD, projections=names))
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert first.applied == second.applied == ["root-start"]
        result = await tracer.get(_THREAD, projections=names)
        assert _result(result, first) == _result(result, second)
        assert _result(result, first).observations == [
            "root-start",
            "root-turn",
            "root-answer",
        ]
        assert (
            first.applied
            == second.applied
            == [
                "root-start",
                "root-start",
                "root-turn",
                "root-answer",
            ]
        )


async def test_cold_fold_observes_cancellation_without_leaving_partial_cached_state() -> (
    None
):
    store = _Store()
    projection = _CancellingProjection()
    tracer = Tracer(store=store, projections=(projection,))
    async with _run(store, "root"):
        operation = asyncio.create_task(_get(tracer, projection))
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert projection.applied == ["root-start"]
        result = await _get(tracer, projection)
        assert _result(result, projection).observations == [
            "root-start",
            "root-turn",
            "root-answer",
        ]
        assert projection.applied == [
            "root-start",
            "root-start",
            "root-turn",
            "root-answer",
        ]


@pytest.mark.docker_integration
async def test_projection_queries_and_follow_observe_other_mysql_instances(
    trace_mysql_url: str,
) -> None:
    first_engine = create_async_engine(trace_mysql_url)
    second_engine = create_async_engine(trace_mysql_url)
    first_store = SqlAlchemyTraceStore(first_engine)
    second_store = SqlAlchemyTraceStore(second_engine)
    first_projection, second_projection = _Projection(), _Projection()
    first_other, second_other = _OtherProjection(), _OtherProjection()
    first = Tracer(store=first_store, projections=(first_projection, first_other))
    second = Tracer(store=second_store, projections=(second_projection, second_other))
    names = (first_projection.name, first_other.name)
    try:
        async with _run(first_store, "root") as writer:
            original = await first.get(_THREAD, projections=names)
            await second.get(_THREAD, projections=names)
            assert first_projection.applied == second_projection.applied
            assert first_other.applied == second_other.applied
            await writer.append((_message(_identity("root"), "remote-update"),))
            latest = await second.get(_THREAD, projections=names)
            assert (
                _result(latest, second_projection).observations[-1] == "remote-update"
            )
            assert _result(latest, second_projection) == _result(latest, second_other)
            assert len(second_projection.applied) == 4
            first_projection.applied.clear()
            first_other.applied.clear()
            async with original.follow() as updates:
                update = await anext(updates)
                assert update.as_of_seq == latest.as_of_seq
                assert (
                    first_projection.applied == first_other.applied == ["remote-update"]
                )
            first_projection.applied.clear()
            first_other.applied.clear()
            retained = await first.get(_THREAD, projections=names)
            assert _result(retained, first_projection) == _result(
                latest, second_projection
            )
            assert _result(retained, first_other) == _result(latest, second_other)
            assert first_projection.applied == first_other.applied == []
        await latest.delete()
        with pytest.raises(TraceThreadNotFound):
            await first.get(_THREAD, projections=names)
        async with _run(second_store, "root"):
            replacement = await first.get(_THREAD, projections=names)
            assert replacement.key.generation != original.key.generation
            assert (
                first_projection.applied
                == first_other.applied
                == [
                    "root-start",
                    "root-turn",
                    "root-answer",
                ]
            )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()
