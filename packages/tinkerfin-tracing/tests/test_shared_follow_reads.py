"""Shared Trace reads keep independent cursors, payloads, and cancellation owners."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    DurableTraceStore,
    InMemoryTraceStore,
    RunFact,
    StateRevisionFact,
    TraceLimits,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.backend import StoredTraceEventPage, TraceEventPageRequest
from tinkerfin_tracing.store import TraceStore, TraceWriter

_NOW = datetime(2026, 9, 21, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def controlled_time(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr("tinkerfin_tracing._follow_reads._monotonic", clock.time)

    async def notification_only(
        notification: Coroutine[None, None, bool], *, timeout: float
    ) -> bool:
        del timeout
        return await notification

    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change", notification_only
    )
    return clock


def _state(identity: RunIdentity, sequence: int) -> StateRevisionFact:
    value: dict[str, JsonValue] = {"items": [sequence]}
    return StateRevisionFact(
        identity=identity,
        source_observation_id=f"{identity.run_id}-state-{sequence}",
        occurred_at=_NOW,
        monotonic_ns=sequence,
        revision_id=f"revision-{sequence}",
        changes=CapturedValue(
            disposition="inline",
            value=value,
            safe_size_bytes=len(json.dumps(value, separators=(",", ":")).encode()),
        ),
    )


@asynccontextmanager
async def _run(
    store: TraceStore,
    *,
    thread: str = "thread",
    namespace: str = "test",
    run: str = "run",
) -> AsyncIterator[tuple[RunIdentity, TraceWriter]]:
    identity = RunIdentity(namespace=namespace, thread_id=thread, run_id=run)
    writer = await store.open_writer(identity)
    try:
        await writer.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id="start",
                    occurred_at=_NOW,
                    monotonic_ns=1,
                    phase="started",
                    input_kind="ordinary",
                ),
                *(_state(identity, index) for index in range(2, 5)),
            )
        )
        yield identity, writer
    finally:
        await writer.aclose()


def _count_reads(
    store: InMemoryTraceStore, monkeypatch: pytest.MonkeyPatch
) -> list[TraceEventPageRequest]:
    original = store.backend.read_event_page
    requests: list[TraceEventPageRequest] = []

    async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
        requests.append(request)
        return await original(request)

    monkeypatch.setattr(store.backend, "read_event_page", read)
    return requests


async def test_concurrent_same_cursor_reads_share_one_backend_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.backend.read_event_page
        reads = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            reads += 1
            entered.set()
            await release.wait()
            return await original(request)

        monkeypatch.setattr(store.backend, "read_event_page", read)
        first = store.follow(writer.key, after_seq=0)
        second = store.follow(writer.key, after_seq=0)
        pulls = [asyncio.create_task(anext(follower)) for follower in (first, second)]
        try:
            await entered.wait()
            release.set()
            updates = await asyncio.gather(*pulls)
            assert reads == 1
            assert [event.trace_seq for event in updates[0].events] == [1, 2, 3, 4]
            assert updates[0] == updates[1]
            assert updates[0].events[1].fact is not updates[1].events[1].fact
        finally:
            release.set()
            for pull in pulls:
                if not pull.done():
                    pull.cancel()
            await asyncio.gather(*pulls, return_exceptions=True)
            await first.aclose()
            await second.aclose()


async def test_cached_prefixes_isolate_payloads_and_expire_after_the_last_follower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        requests = _count_reads(store, monkeypatch)
        first = store.follow(writer.key, after_seq=0)
        second = store.follow(writer.key, after_seq=1)
        tail = store.follow(writer.key, after_seq=4)
        try:
            first_update = await anext(first)
            fact = first_update.events[1].fact
            assert isinstance(fact, StateRevisionFact)
            assert isinstance(fact.changes.value, dict)
            fact.changes.value["items"] = ["changed by one subscriber"]
            second_update = await anext(second)
            assert [event.trace_seq for event in second_update.events] == [2, 3, 4]
            second_fact = second_update.events[0].fact
            assert isinstance(second_fact, StateRevisionFact)
            assert second_fact.changes.value == {"items": [2]}
            assert (await anext(tail)).events == ()
            assert len(requests) == 1
            await first.aclose()
            third = store.follow(writer.key, after_seq=0)
            try:
                assert (await anext(third)).events[0].trace_seq == 1
                assert len(requests) == 1
            finally:
                await third.aclose()
        finally:
            await first.aclose()
            await second.aclose()
            await tail.aclose()
        reopened = store.follow(writer.key, after_seq=0)
        try:
            assert (await anext(reopened)).as_of_seq == 4
            assert len(requests) == 2
        finally:
            await reopened.aclose()


async def test_local_commit_invalidates_cached_tail_and_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (identity, writer):
        requests = _count_reads(store, monkeypatch)
        follower = store.follow(writer.key, after_seq=4)
        try:
            initial = await anext(follower)
            assert initial.events == () and initial.active_run_ids == (identity.run_id,)
            await writer.append((_state(identity, 5),))
            committed = await anext(follower)
            assert [event.trace_seq for event in committed.events] == [5]
            await writer.aclose()
            closed = await anext(follower)
            assert closed.events == () and closed.active_run_ids == ()
            assert closed.as_of_seq == 5
            assert len(requests) == 3
        finally:
            await follower.aclose()


async def test_cursor_beyond_cached_tail_reads_current_remote_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    peer = DurableTraceStore(store.backend)
    async with _run(store) as (_, writer):
        requests = _count_reads(store, monkeypatch)
        first = store.follow(writer.key, after_seq=0)
        try:
            assert (await anext(first)).active_run_ids == ("run",)
            async with _run(peer, run="remote") as (_, remote_writer):
                assert remote_writer.key == writer.key
                second = store.follow(writer.key, after_seq=8)
                try:
                    current = await anext(second)
                    assert current.events == () and current.as_of_seq == 8
                    assert current.active_run_ids == ("remote", "run")
                    assert len(requests) == 2
                finally:
                    await second.aclose()
        finally:
            await first.aclose()


async def test_cached_empty_reads_wait_only_the_remaining_poll_interval(
    monkeypatch: pytest.MonkeyPatch, controlled_time: _Clock
) -> None:
    store = InMemoryTraceStore()
    peer = DurableTraceStore(store.backend)
    async with _run(store) as (_, writer):
        requests = _count_reads(store, monkeypatch)
        first = store.follow(writer.key, after_seq=4)
        second = store.follow(writer.key, after_seq=4)
        try:
            assert (await anext(first)).events == ()
            controlled_time.now = 0.25
            async with _run(peer, run="remote"):
                cached = await anext(second)
                assert cached.events == () and cached.active_run_ids == ("run",)
                delays: list[float] = []

                async def expire(
                    notification: Coroutine[None, None, bool], *, timeout: float
                ) -> bool:
                    notification.close()
                    delays.append(timeout)
                    controlled_time.now += timeout
                    raise TimeoutError

                monkeypatch.setattr(
                    "tinkerfin_tracing.durable_store._wait_for_follow_change", expire
                )
                refreshed = await anext(second)
                assert [event.trace_seq for event in refreshed.events] == [5, 6, 7, 8]
                assert refreshed.active_run_ids == ("remote", "run")
                assert delays == [0.25]
                assert len(requests) == 2
        finally:
            await first.aclose()
            await second.aclose()


async def test_a_read_that_exhausted_its_poll_interval_rechecks_without_waiting(
    monkeypatch: pytest.MonkeyPatch, controlled_time: _Clock
) -> None:
    store = InMemoryTraceStore()
    peer = DurableTraceStore(store.backend)
    async with _run(store) as (_, writer):
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.backend.read_event_page
        reads = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            reads += 1
            page = await original(request)
            if reads == 1:
                entered.set()
                await release.wait()
            return page

        async def reject_wait(
            notification: Coroutine[None, None, bool], *, timeout: float
        ) -> bool:
            notification.close()
            raise AssertionError(f"Expired read requested another wait: {timeout}")

        monkeypatch.setattr(store.backend, "read_event_page", read)
        monkeypatch.setattr(
            "tinkerfin_tracing.durable_store._wait_for_follow_change", reject_wait
        )
        follower = store.follow(writer.key, after_seq=4)
        initial = asyncio.create_task(anext(follower))
        try:
            await entered.wait()
            controlled_time.now = 0.75
            async with _run(peer, run="remote"):
                release.set()
                assert (await initial).events == ()
                refreshed = await anext(follower)
                assert [event.trace_seq for event in refreshed.events] == [5, 6, 7, 8]
                assert reads == 2
        finally:
            release.set()
            if not initial.done():
                initial.cancel()
            await asyncio.gather(initial, return_exceptions=True)
            await follower.aclose()


@pytest.mark.parametrize("budget", ("bytes", "records", "pages"))
async def test_cache_budgets_are_shared_across_thread_namespaces(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    store = InMemoryTraceStore()
    async with _run(store, namespace="first") as (_, first_writer):
        async with _run(store, namespace="second") as (_, second_writer):
            if budget == "bytes":
                pages = [
                    await store.backend.read_event_page(
                        TraceEventPageRequest(
                            key=writer.key, direction="forward", after_seq=0, limit=256
                        )
                    )
                    for writer in (first_writer, second_writer)
                ]
                maximum = max(
                    sum(len(record.canonical_payload) for record in page.events)
                    for page in pages
                )
                monkeypatch.setattr(
                    "tinkerfin_tracing._follow_reads._MAX_CACHED_BYTES", maximum
                )
            elif budget == "records":
                monkeypatch.setattr(
                    "tinkerfin_tracing._follow_reads._MAX_CACHED_RECORDS", 4
                )
            else:
                monkeypatch.setattr(
                    "tinkerfin_tracing._follow_reads._MAX_CACHED_PAGES", 1
                )
            requests = _count_reads(store, monkeypatch)
            first = store.follow(first_writer.key, after_seq=0)
            second = store.follow(second_writer.key, after_seq=0)
            revisited = store.follow(first_writer.key, after_seq=0)
            try:
                assert (await anext(first)).events[0].fact.identity.namespace == "first"
                assert (await anext(second)).events[
                    0
                ].fact.identity.namespace == "second"
                assert (await anext(revisited)).events[
                    0
                ].fact.identity.namespace == "first"
                assert len(requests) == 3
            finally:
                await first.aclose()
                await second.aclose()
                await revisited.aclose()


@pytest.mark.parametrize("budget", ("bytes", "records"))
async def test_oversized_pages_are_delivered_without_retaining_them(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        monkeypatch.setattr(
            f"tinkerfin_tracing._follow_reads._MAX_CACHED_{budget.upper()}", 1
        )
        requests = _count_reads(store, monkeypatch)
        first = store.follow(writer.key, after_seq=0)
        second = store.follow(writer.key, after_seq=0)
        try:
            assert (await anext(first)).as_of_seq == 4
            assert (await anext(second)).as_of_seq == 4
            assert len(requests) == 2
        finally:
            await first.aclose()
            await second.aclose()


async def test_cancelling_one_waiter_keeps_the_shared_read_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = store.backend.read_event_page
        reads = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            reads += 1
            entered.set()
            try:
                await release.wait()
                return await original(request)
            finally:
                settled.set()

        monkeypatch.setattr(store.backend, "read_event_page", read)
        first = store.follow(writer.key, after_seq=0)
        second = store.follow(writer.key, after_seq=0)
        first_pull = asyncio.create_task(anext(first))
        second_pull = asyncio.create_task(anext(second))
        try:
            await entered.wait()
            first_pull.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_pull
            assert not settled.is_set()
            release.set()
            assert (await second_pull).as_of_seq == 4
            assert reads == 1 and settled.is_set()
        finally:
            release.set()
            for pull in (first_pull, second_pull):
                if not pull.done():
                    pull.cancel()
            await asyncio.gather(first_pull, second_pull, return_exceptions=True)
            await first.aclose()
            await second.aclose()


async def test_final_waiter_repeated_cancellation_joins_backend_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        release_cleanup, settled = asyncio.Event(), asyncio.Event()

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            del request
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release_cleanup.wait()
                raise
            finally:
                settled.set()
            raise AssertionError("cancelled read returned")

        monkeypatch.setattr(store.backend, "read_event_page", read)
        follower = store.follow(writer.key, after_seq=0)
        pull = asyncio.create_task(anext(follower))
        try:
            await entered.wait()
            pull.cancel()
            await cancelled.wait()
            pull.cancel()
            assert not pull.done() and not settled.is_set()
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await pull
            assert settled.is_set()
        finally:
            release_cleanup.set()
            if not pull.done():
                pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
            await follower.aclose()


class _ProcessStop(BaseException):
    pass


def _contains(error: BaseException, expected: BaseException) -> bool:
    if error is expected:
        return True
    if isinstance(error, BaseExceptionGroup) and any(
        _contains(item, expected) for item in error.exceptions
    ):
        return True
    return any(
        _contains(item, expected)
        for item in (error.__cause__, error.__context__)
        if item is not None and item is not error
    )


@pytest.mark.parametrize("control", (False, True))
async def test_final_cancellation_preserves_backend_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, control: bool
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        failure = (
            _ProcessStop("stop") if control else TraceStoreProtocolError("cleanup")
        )

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            del request
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise failure
            raise AssertionError("cancelled read returned")

        monkeypatch.setattr(store.backend, "read_event_page", read)
        follower = store.follow(writer.key, after_seq=0)
        pull = asyncio.create_task(anext(follower))
        try:
            await entered.wait()
            pull.cancel()
            await cancelled.wait()
            release.set()
            with pytest.raises(BaseException) as caught:
                await pull
            if control:
                assert caught.value is failure
            else:
                assert isinstance(caught.value, asyncio.CancelledError)
                assert _contains(caught.value, failure)
        finally:
            release.set()
            if not pull.done():
                pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
            await follower.aclose()


async def test_shared_failure_reaches_all_waiters_and_a_later_pull_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.backend.read_event_page
        failure = TraceStoreProtocolError("read failed")
        reads = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            reads += 1
            if reads == 1:
                entered.set()
                await release.wait()
                raise failure
            return await original(request)

        monkeypatch.setattr(store.backend, "read_event_page", read)
        followers = [store.follow(writer.key, after_seq=0) for _ in range(2)]
        pulls = [asyncio.create_task(anext(follower)) for follower in followers]
        try:
            await entered.wait()
            release.set()
            failures = await asyncio.gather(*pulls, return_exceptions=True)
            assert failures == [failure, failure]
            assert reads == 1
            later = store.follow(writer.key, after_seq=0)
            try:
                assert (await anext(later)).as_of_seq == 4
                assert reads == 2
            finally:
                await later.aclose()
        finally:
            release.set()
            for pull in pulls:
                if not pull.done():
                    pull.cancel()
            await asyncio.gather(*pulls, return_exceptions=True)
            for follower in followers:
                await follower.aclose()


@pytest.mark.parametrize("equal_timestamps", (False, True))
@pytest.mark.parametrize("completed_old_read", (False, True))
async def test_older_independent_observations_are_not_reused(
    monkeypatch: pytest.MonkeyPatch,
    equal_timestamps: bool,
    completed_old_read: bool,
) -> None:
    store = InMemoryTraceStore(limits=TraceLimits(follow_batch_size=2))
    async with _run(store) as (_, writer):
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.backend.read_event_page
        reads = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            reads += 1
            number = reads
            page = await original(request)
            if number == 1:
                entered.set()
                await release.wait()
            return replace(
                page,
                active_run_ids=("old",) if number == 1 else ("new",),
                observed_at=_NOW
                if equal_timestamps
                else _NOW + timedelta(seconds=number),
            )

        monkeypatch.setattr(store.backend, "read_event_page", read)
        older = store.follow(writer.key, after_seq=2)
        current = store.follow(writer.key, after_seq=0)
        old_pull = asyncio.create_task(anext(older))
        try:
            await entered.wait()
            initial = await anext(current)
            assert initial.active_run_ids == ("new",)
            if completed_old_read:
                release.set()
                assert (await old_pull).active_run_ids == ("old",)
            latest = await anext(current)
            assert latest.active_run_ids == ("new",)
            assert latest.observed_at >= initial.observed_at
            assert [event.trace_seq for event in latest.events] == [3, 4]
            assert reads == 3
            release.set()
            await old_pull
            later = store.follow(writer.key, after_seq=2)
            try:
                assert (await anext(later)).active_run_ids == ("new",)
                assert reads == 3
            finally:
                await later.aclose()
        finally:
            release.set()
            if not old_pull.done():
                old_pull.cancel()
            await asyncio.gather(old_pull, return_exceptions=True)
            await older.aclose()
            await current.aclose()


async def test_fresh_storage_clock_changes_keep_the_existing_read_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore(limits=TraceLimits(follow_batch_size=2))
    async with _run(store) as (_, writer):
        original = store.backend.read_event_page
        times = iter((_NOW + timedelta(seconds=1), _NOW))

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            return replace(await original(request), observed_at=next(times))

        monkeypatch.setattr(store.backend, "read_event_page", read)
        follower = store.follow(writer.key, after_seq=0)
        try:
            first = await anext(follower)
            second = await anext(follower)
            assert second.observed_at < first.observed_at
            assert [event.trace_seq for event in second.events] == [3, 4]
        finally:
            await follower.aclose()


async def test_backend_reads_have_a_shared_concurrency_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        occupied, ninth_entered = asyncio.Event(), asyncio.Event()
        releases = [asyncio.Event() for _ in range(9)]
        original = store.backend.read_event_page
        active = maximum = entered = 0

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal active, maximum, entered
            assert request.after_seq is not None
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if entered == 8:
                occupied.set()
            if entered == 9:
                ninth_entered.set()
            try:
                await releases[request.after_seq].wait()
                return await original(request)
            finally:
                active -= 1

        monkeypatch.setattr(store.backend, "read_event_page", read)
        followers = [store.follow(writer.key, after_seq=index) for index in range(9)]
        pulls = [asyncio.create_task(anext(follower)) for follower in followers]
        try:
            await occupied.wait()
            assert maximum == 8 and not ninth_entered.is_set()
            releases[0].set()
            await ninth_entered.wait()
            assert maximum == 8
            for release in releases:
                release.set()
            await asyncio.gather(*pulls)
            assert entered == 9 and active == 0
        finally:
            for release in releases:
                release.set()
            for pull in pulls:
                if not pull.done():
                    pull.cancel()
            await asyncio.gather(*pulls, return_exceptions=True)
            for follower in followers:
                await follower.aclose()


async def test_generation_replacement_never_reuses_an_old_cached_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _run(store) as (_, writer):
        requests = _count_reads(store, monkeypatch)
        old = store.follow(writer.key, after_seq=0)
        try:
            assert (await anext(old)).as_of_seq == 4
            await writer.aclose()
            await store.delete(writer.key)
            async with _run(store) as (_, replacement):
                assert replacement.key != writer.key
                current = store.follow(replacement.key, after_seq=0)
                try:
                    result = await anext(current)
                    assert all(
                        event.generation == replacement.key.generation
                        for event in result.events
                    )
                    with pytest.raises(TraceThreadNotFound):
                        await anext(old)
                    assert len(requests) == 3
                finally:
                    await current.aclose()
        finally:
            await old.aclose()
