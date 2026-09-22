"""Lossless follow notifications and independent subscriber cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Literal

import pytest

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    DurableTraceStore,
    InMemoryTraceStore,
    RunFact,
    TraceEvent,
    TraceStore,
    TraceStoreOptions,
    TraceStoreUpdate,
)
from tinkerfin_tracing.backend import StoredTraceEventPage, TraceEventPageRequest


async def _wait_for_notification(
    notification: Coroutine[None, None, bool], *, timeout: float
) -> bool:
    del timeout
    return await notification


def _fact(identity: RunIdentity, phase: Literal["started", "terminal"]) -> RunFact:
    return RunFact(
        identity=identity,
        source_observation_id=f"{identity.run_id}-{phase}",
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase == "terminal" else None,
    )


async def _write_run(store: TraceStore, *, thread: str, run: str) -> None:
    identity = RunIdentity(namespace="test", thread_id=thread, run_id=run)
    writer = await store.open_writer(identity)
    try:
        await writer.append((_fact(identity, "started"),))
        await writer.append((_fact(identity, "terminal"),), mandatory=True)
    finally:
        await writer.aclose()


async def _next_events(
    follower: AsyncIterator[TraceStoreUpdate],
) -> tuple[TraceEvent, ...]:
    async for update in follower:
        if update.events:
            return update.events
    raise AssertionError("follower ended before event delivery")


async def test_commit_between_empty_read_and_wait_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTraceStore(
        InMemoryTraceStore().backend, options=TraceStoreOptions(follow_poll_seconds=10)
    )
    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change",
        _wait_for_notification,
    )
    await _write_run(store, thread="followed", run="first")
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="followed")
    )
    empty_read = asyncio.Event()
    release_read = asyncio.Event()
    original_read = store.backend.read_event_page
    reads = 0

    async def gate_empty_page(request: TraceEventPageRequest) -> StoredTraceEventPage:
        nonlocal reads
        page = await original_read(request)
        reads += 1
        if reads == 1:
            assert not page.events
            empty_read.set()
            await release_read.wait()
        return page

    monkeypatch.setattr(store.backend, "read_event_page", gate_empty_page)
    follower = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    pending = asyncio.create_task(_next_events(follower))
    try:
        await empty_read.wait()
        await _write_run(store, thread="followed", run="second")
        release_read.set()
        batch = await pending
        assert [item.trace_seq for item in batch] == [3, 4]
        assert {item.fact.identity.run_id for item in batch} == {"second"}
    finally:
        release_read.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()


async def test_closing_one_follower_preserves_the_other_local_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTraceStore(
        InMemoryTraceStore().backend, options=TraceStoreOptions(follow_poll_seconds=10)
    )
    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change",
        _wait_for_notification,
    )
    await _write_run(store, thread="shared", run="first")
    snapshot = await store.snapshot(
        ThreadIdentity(namespace="test", thread_id="shared")
    )
    first = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    second = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    first_initial, second_initial = await asyncio.gather(anext(first), anext(second))
    assert first_initial.events == second_initial.events == ()
    first_pull = asyncio.create_task(_next_events(first))
    second_pull = asyncio.create_task(_next_events(second))
    try:
        first_pull.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_pull
        await first.aclose()
        await _write_run(store, thread="shared", run="second")
        batch = await second_pull
        sequences = [item.trace_seq for item in batch]
        if sequences == [3]:
            sequences.extend(item.trace_seq for item in await _next_events(second))
        assert sequences == [3, 4]
    finally:
        for pending in (first_pull, second_pull):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(first_pull, second_pull, return_exceptions=True)
        await first.aclose()
        await second.aclose()
