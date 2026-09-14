"""In-memory Store sequencing, quotas, following, and generation isolation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from pydantic import JsonValue, ValidationError

from tinkerfin_contracts import RunIdentity, RunSourceContext, ThreadIdentity
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec
from tinkerfin_tracing.durable_store import InMemoryTraceStore
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.facts import MessageFact, RunFact, TraceSemanticFact
from tinkerfin_tracing.limits import TraceLimits
from tinkerfin_tracing.store import (
    StoreThreadSnapshot,
    TraceProjectionCheckpoint,
    TraceThreadKey,
)
from tinkerfin_tracing.tracer import Tracer


def _identity(run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)


def _captured(value: JsonValue) -> CapturedValue:
    encoded = CanonicalTracePayloadCodec().encode_json(value)
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(encoded.data),
        value=value,
    )


def _fact(
    run_id: str,
    phase: Literal["started", "input", "terminal", "closed"] = "started",
) -> RunFact:
    input_kind = "ordinary" if phase in {"started", "input"} else None
    capture = (
        CapturedValue(disposition="inline", safe_size_bytes=2, value={})
        if phase == "input"
        else None
    )
    outcome = "succeeded" if phase in {"terminal", "closed"} else None
    return RunFact(
        source_observation_id=f"observation-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind=input_kind,
        input=capture,
        config=capture,
        outcome=outcome,
    )


async def test_store_assigns_one_contiguous_sequence_across_concurrent_runs() -> None:
    store = InMemoryTraceStore()
    first = await store.open_writer(_identity("run-1"))
    second = await store.open_writer(_identity("run-2"))

    first_events, second_events = await asyncio.gather(
        first.append((_fact("run-1"),)),
        second.append((_fact("run-2"),)),
    )
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-1")
    )

    assert {first_events[0].trace_seq, second_events[0].trace_seq} == {1, 2}
    assert snapshot.as_of_seq == 2
    assert snapshot.active_run_ids == ("run-1", "run-2")
    assert {item.committed_events for item in snapshot.active_writers} == {1}
    stored = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=snapshot.as_of_seq,
        limit=10,
    )
    assert [event.trace_seq for event in stored] == [1, 2]
    assert snapshot.persisted_bytes == sum(event.persisted_bytes for event in stored)
    await first.aclose()
    await second.aclose()


async def test_in_memory_reads_reuse_framework_validated_event_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    await writer.append((_fact("run-1"),))
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-1")
    )
    decode_calls = 0
    original = CanonicalTracePayloadCodec.decode_fact

    def count_decode(
        codec: CanonicalTracePayloadCodec,
        payload: bytes,
    ) -> TraceSemanticFact:
        nonlocal decode_calls
        decode_calls += 1
        return original(codec, payload)

    monkeypatch.setattr(CanonicalTracePayloadCodec, "decode_fact", count_decode)

    forward = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=1,
        limit=1,
    )
    reverse = await store.read_events_reverse(
        snapshot.key,
        before_seq=2,
        limit=1,
    )

    assert forward == reverse
    assert decode_calls == 0
    await writer.aclose()


async def test_store_rejects_duplicate_run_and_active_generation_delete() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    with pytest.raises(TraceRunConflict):
        await store.open_writer(_identity())
    key = writer.key
    with pytest.raises(TraceRunConflict):
        await store.delete(key)
    await writer.append((_fact("run-1"),))
    await writer.aclose()
    await store.delete(key)
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot_key(key)

    replacement = await store.open_writer(_identity())
    assert replacement.key.generation != key.generation
    await replacement.aclose()


async def test_concurrent_writer_close_is_idempotent_and_releases_ownership() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())

    await asyncio.gather(writer.aclose(), writer.aclose())
    retry = await store.open_writer(_identity())

    await retry.aclose()


async def test_follow_yields_bounded_new_batches_and_releases_on_cancel() -> None:
    store = InMemoryTraceStore(limits=TraceLimits(follow_batch_size=1))
    writer = await store.open_writer(_identity())
    follower = store.follow(writer.key, after_seq=0)
    current = await anext(follower)
    assert current.events == ()
    assert current.active_run_ids == (writer.run_id,)
    waiting = asyncio.ensure_future(anext(follower))
    await asyncio.sleep(0)

    await writer.append((_fact("run-1"), _fact("run-1", "input")))
    first = await waiting
    second = await anext(follower)

    assert [event.trace_seq for event in first.events] == [1]
    assert [event.trace_seq for event in second.events] == [2]
    await follower.aclose()
    await writer.aclose()


async def test_reverse_reads_and_projection_checkpoint_history_are_generation_bound() -> (
    None
):
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    events = await writer.append(
        (
            _fact("run-1"),
            _fact("run-1", "input"),
            _fact("run-1", "input"),
        )
    )
    key = writer.key

    reverse = await store.read_events_reverse(key, before_seq=4, limit=2)
    assert [event.trace_seq for event in reverse] == [3, 2]

    first = TraceProjectionCheckpoint(
        key=key,
        projection_name="tests.projection",
        run_id="run-1",
        as_of_seq=1,
        state={"count": 1},
    )
    latest = first.model_copy(update={"as_of_seq": 3, "state": {"count": 3}})
    await store.save_projection_checkpoint(first, expected_as_of_seq=None)
    await store.save_projection_checkpoint(latest, expected_as_of_seq=1)

    historical = await store.load_projection_checkpoint(
        key,
        projection_name="tests.projection",
        run_id="run-1",
        as_of_seq=2,
    )
    assert historical == first
    with pytest.raises(TraceProjectionCheckpointConflict):
        await store.save_projection_checkpoint(
            latest.model_copy(update={"as_of_seq": events[-2].trace_seq}),
            expected_as_of_seq=1,
        )
    assert (
        await store.save_projection_checkpoint(latest, expected_as_of_seq=1) == latest
    )
    await writer.aclose()


async def test_regular_append_preserves_mandatory_terminal_reserve() -> None:
    limits = TraceLimits(
        max_event_bytes=1024,
        max_thread_events=16,
        max_thread_bytes=512 * 1024,
        max_tracer_threads=2,
        max_tracer_bytes=1024 * 1024,
        terminal_reserve_events_per_run=2,
        terminal_reserve_bytes_per_run=2048,
    )
    store = InMemoryTraceStore(limits=limits)
    writer = await store.open_writer(_identity())

    await writer.append(tuple(_fact("run-1", "input") for _index in range(14)))
    with pytest.raises(TraceQuotaExceeded):
        await writer.append((_fact("run-1", "input"),))
    terminal = await writer.append(
        (_fact("run-1", "terminal"), _fact("run-1", "closed")),
        mandatory=True,
    )

    assert [event.trace_seq for event in terminal] == [15, 16]
    await writer.aclose()


async def test_ordinary_batch_rejects_mixed_terminal_fact() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    try:
        with pytest.raises(TraceStoreProtocolError, match="mandatory append"):
            await writer.append((_fact("run-1"), _fact("run-1", "terminal")))
        assert (
            await store.snapshot(ThreadIdentity(namespace="test", thread_id="thread-1"))
        ).as_of_seq == 0
    finally:
        await writer.aclose()


async def test_closing_a_zero_fact_writer_allows_the_same_run_to_retry() -> None:
    store = InMemoryTraceStore()
    first = await store.open_writer(_identity())
    first_generation = first.key.generation
    await first.aclose()

    retry = await store.open_writer(_identity())

    assert retry.key.generation != first_generation
    await retry.aclose()


async def test_failed_new_thread_reservation_does_not_leave_an_empty_thread() -> None:
    limits = TraceLimits(
        max_event_bytes=16 * 1024,
        max_thread_events=100,
        max_thread_bytes=64 * 1024,
        max_tracer_threads=2,
        max_tracer_bytes=64 * 1024,
        terminal_reserve_events_per_run=2,
        terminal_reserve_bytes_per_run=32 * 1024,
    )
    store = InMemoryTraceStore(limits=limits)
    first = await store.open_writer(_identity())
    await first.append(
        tuple(
            MessageFact(
                source_observation_id=f"large-observation-{index}",
                identity=_identity(),
                occurred_at=datetime.now(UTC),
                monotonic_ns=1,
                phase="reconciled",
                message_id=f"message:large:{index}",
                source_message_id=f"large-{index}",
                role="assistant",
                content=CapturedValue(
                    disposition="inline",
                    safe_size_bytes=15_402,
                    value="x" * 15_400,
                ),
            )
            for index in range(2)
        )
    )
    await first.append(
        (_fact("run-1", "terminal"), _fact("run-1", "closed")),
        mandatory=True,
    )
    await first.aclose()

    with pytest.raises(TraceQuotaExceeded):
        await store.open_writer(
            RunIdentity(namespace="test", thread_id="thread-2", run_id="run-2")
        )
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(ThreadIdentity(namespace="test", thread_id="thread-2"))


async def test_one_writer_cannot_consume_another_writers_terminal_reserve() -> None:
    limits = TraceLimits(
        max_event_bytes=8 * 1024,
        max_thread_events=16,
        max_thread_bytes=512 * 1024,
        max_tracer_threads=2,
        max_tracer_bytes=1024 * 1024,
        terminal_reserve_events_per_run=2,
        terminal_reserve_bytes_per_run=16 * 1024,
    )
    store = InMemoryTraceStore(limits=limits)
    first = await store.open_writer(_identity("run-1"))
    second = await store.open_writer(_identity("run-2"))
    await first.append(tuple(_fact("run-1", "input") for _index in range(12)))

    with pytest.raises(TraceQuotaExceeded, match="event quota"):
        await first.append((_fact("run-1", "input"),))
    with pytest.raises(TraceStoreProtocolError, match="exactly-once"):
        await first.append(
            tuple(_fact("run-1", "terminal") for _index in range(3)),
            mandatory=True,
        )

    await first.append(
        (_fact("run-1", "terminal"), _fact("run-1", "closed")),
        mandatory=True,
    )
    await second.append(
        (_fact("run-2", "terminal"), _fact("run-2", "closed")),
        mandatory=True,
    )
    await first.aclose()
    await second.aclose()


def test_terminal_byte_reserve_covers_every_reserved_max_size_event() -> None:
    with pytest.raises(ValidationError, match="cover every reserved max-size event"):
        TraceLimits(
            max_event_bytes=8 * 1024,
            terminal_reserve_events_per_run=2,
            terminal_reserve_bytes_per_run=(16 * 1024) - 1,
        )


async def test_writer_rejects_facts_from_another_run() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity("run-1"))

    with pytest.raises(TraceStoreProtocolError):
        await writer.append((_fact("run-2"),))

    await writer.aclose()


async def test_append_copies_nested_fact_values_before_returning() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    content: JsonValue = {"nested": {"value": "original"}}
    fact = MessageFact(
        source_observation_id="copy-observation",
        identity=_identity(),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="reconciled",
        message_id="message:copy",
        source_message_id="copy",
        role="assistant",
        content=_captured(content),
    )
    await writer.append((fact,))
    captured = fact.content
    assert captured is not None and isinstance(captured.value, dict)
    nested = captured.value["nested"]
    assert isinstance(nested, dict)
    nested["value"] = "tampered"

    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="thread-1")
    )
    stored = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=snapshot.as_of_seq,
        limit=10,
    )

    assert "tampered" not in snapshot.model_dump_json(by_alias=True)
    assert "tampered" not in stored[0].model_dump_json(by_alias=True)
    assert "original" in stored[0].model_dump_json(by_alias=True)
    await writer.aclose()


async def test_mandatory_append_accepts_only_terminal_and_closed_run_facts() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())

    with pytest.raises(TraceStoreProtocolError, match="mandatory append"):
        await writer.append((_fact("run-1", "input"),), mandatory=True)
    with pytest.raises(TraceStoreProtocolError, match="mandatory append"):
        await writer.append((_fact("run-1", "terminal"),))

    await writer.aclose()


async def test_open_run_releases_writer_when_snapshot_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    original = store.snapshot_key
    calls = 0

    async def fail_once(key: TraceThreadKey) -> StoreThreadSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("snapshot failed")
        return await original(key)

    monkeypatch.setattr(store, "snapshot_key", fail_once)
    context = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )

    with pytest.raises(RuntimeError, match="snapshot failed"):
        await tracer.open_run(context)
    retry = await tracer.open_run(context)
    await retry.aclose()


async def test_tracer_rejects_invalid_store_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    context = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )

    async def invalid_writer(_identity: RunIdentity) -> Any:
        return object()

    monkeypatch.setattr(store, "open_writer", invalid_writer)
    with pytest.raises(TraceStoreProtocolError, match="invalid writer"):
        await tracer.open_run(context)

    async def invalid_snapshot(_thread_id: str) -> Any:
        return object()

    monkeypatch.setattr(store, "snapshot", invalid_snapshot)
    with pytest.raises(TraceStoreProtocolError, match="invalid thread snapshot"):
        await tracer.get(ThreadIdentity(namespace="test", thread_id="thread-1"))


async def test_cancelled_follow_wait_releases_the_condition() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    follower = store.follow(writer.key, after_seq=0)
    assert (await anext(follower)).events == ()
    waiting = asyncio.ensure_future(anext(follower))
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await follower.aclose()
    events = await writer.append((_fact("run-1"),))

    assert events[0].trace_seq == 1
    await writer.aclose()


async def test_delete_wakes_old_followers_and_recreation_uses_a_new_generation() -> (
    None
):
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    await writer.append((_fact("run-1"),))
    key = writer.key
    await writer.aclose()
    follower = store.follow(key, after_seq=1)
    assert (await anext(follower)).events == ()
    waiting = asyncio.ensure_future(anext(follower))
    await asyncio.sleep(0)

    await store.delete(key)
    with pytest.raises(TraceThreadNotFound):
        await waiting
    await follower.aclose()
    replacement = await store.open_writer(_identity())

    assert replacement.key.generation != key.generation
    await replacement.aclose()
