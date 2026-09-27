"""Committed Trace invalidation and lease-aware cross-instance following."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

import pytest
from test_follow_notifications import _fact, _next_events, _write_run

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    NotificationLimits,
    Notifications,
    NotificationScope,
    NotificationSubscription,
    NotificationUnavailable,
)
from tinkerfin_tracing import (
    DurableTraceStore,
    InMemoryTraceStore,
    TraceStoreOptions,
    TraceThreadNotFound,
)
from tinkerfin_tracing.backend import (
    StoredTraceEventPage,
    TraceEventPageRequest,
    TraceLedgerChange,
    TraceLedgerCommitResult,
)


async def test_cancelled_follow_retains_readiness_cleanup_failure() -> None:
    entered = asyncio.Event()
    original = OSError("Controlled readiness cleanup failure")

    class FailedReadiness(MemoryBackend):
        async def wait_ready(self) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as cancellation:
                raise cancellation from NotificationUnavailable(
                    "Readiness cleanup failed", cause=original
                )

    async with Notifications(backend=FailedReadiness()) as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        writer = await store.open_writer(
            RunIdentity(namespace="account", thread_id="thread", run_id="run")
        )
        follower = store.follow(writer.key, after_seq=0)
        pending = asyncio.create_task(anext(follower))
        try:
            await entered.wait()
            pending.cancel("caller cancelled follow")
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            assert caught.value.args == ("caller cancelled follow",)
            failures: list[BaseException] = [caught.value]
            seen: set[int] = set()
            while failures:
                failure = failures.pop()
                if id(failure) in seen:
                    continue
                seen.add(id(failure))
                if isinstance(failure, BaseExceptionGroup):
                    failures.extend(failure.exceptions)
                cause = getattr(failure, "cause", None)
                failures.extend(
                    item
                    for item in (failure.__cause__, failure.__context__, cause)
                    if isinstance(item, BaseException)
                )
            assert id(original) in seen
        finally:
            await follower.aclose()
            await writer.aclose()


async def test_writer_append_close_and_delete_publish_only_committed_resource_facts() -> (
    None
):
    identity = RunIdentity(namespace="account", thread_id="thread", run_id="run")
    async with Notifications() as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        assert store.notifications is notifications
        async with notifications.subscribe() as changes:
            writer = await store.open_writer(identity)
            opened = await anext(changes)
            assert isinstance(opened, Notification)
            assert opened.topic == "trace.changed"
            assert opened.scope.namespace == identity.namespace
            assert opened.key == identity.thread_id
            assert opened.details == {
                "generation": writer.key.generation,
                "run_id": "run",
            }
            await writer.append((_fact(identity, "started"),))
            appended = await anext(changes)
            assert isinstance(appended, Notification)
            assert appended.details["trace_seq"] == 1
            await writer.aclose()
            closed = await anext(changes)
            assert isinstance(closed, Notification)
            assert not (await store.snapshot_key(writer.key)).active_run_ids
            await store.delete(writer.key)
            deleted = await anext(changes)
            assert isinstance(deleted, Notification)
            assert deleted.details["generation"] == writer.key.generation


async def test_publication_failure_cannot_turn_committed_facts_into_a_failed_append() -> (
    None
):
    attempted = asyncio.Event()

    class FailedPublisher(MemoryBackend):
        async def publish(self, payload: bytes) -> None:
            attempted.set()
            raise NotificationUnavailable("transport unavailable")

    async with Notifications(backend=FailedPublisher()) as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        await _write_run(store, thread="thread", run="run")
        await attempted.wait()
        snapshot = await store.snapshot(
            ThreadIdentity(namespace="test", thread_id="thread")
        )
        assert snapshot.as_of_seq == 2
        assert not snapshot.active_run_ids


async def test_writer_operations_do_not_wait_for_advisory_transport() -> None:
    publishing, release = asyncio.Event(), asyncio.Event()

    class HeldPublisher(MemoryBackend):
        held = False

        async def publish(self, payload: bytes) -> None:
            if not self.held:
                self.held = True
                publishing.set()
                await release.wait()
            await super().publish(payload)

    async with Notifications(backend=HeldPublisher()) as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
        opening = asyncio.create_task(store.open_writer(identity))
        await publishing.wait()
        try:
            assert opening.done()
            writer = await opening
            await writer.append((_fact(identity, "started"),))
            await writer.aclose()
            assert not (await store.snapshot_key(writer.key)).active_run_ids
        finally:
            release.set()


async def test_remote_change_during_baseline_read_invalidates_the_shared_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def notification_only(
        wait: Coroutine[None, None, bool], *, timeout: float
    ) -> bool:
        return await wait

    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change", notification_only
    )
    async with Notifications(
        limits=NotificationLimits(max_subscriptions=1)
    ) as notifications:
        origin = InMemoryTraceStore(notifications=notifications)
        follower_store = DurableTraceStore(origin.backend, notifications=notifications)
        await _write_run(origin, thread="thread", run="first")
        snapshot = await origin.snapshot(
            ThreadIdentity(namespace="test", thread_id="thread")
        )
        read_started, release_read = asyncio.Event(), asyncio.Event()
        original_read = origin.backend.read_event_page
        reads = 0

        async def held_read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal reads
            page = await original_read(request)
            reads += 1
            if reads == 1:
                read_started.set()
                await release_read.wait()
            return page

        monkeypatch.setattr(origin.backend, "read_event_page", held_read)
        first = follower_store.follow(snapshot.key, after_seq=2)
        second = follower_store.follow(snapshot.key, after_seq=2)
        pulls = [asyncio.create_task(_next_events(item)) for item in (first, second)]
        try:
            await read_started.wait()
            await _write_run(origin, thread="thread", run="second")
            release_read.set()
            results = await asyncio.gather(*pulls)
            assert [[item.trace_seq for item in result] for result in results] == [
                [3, 4],
                [3, 4],
            ]
            assert reads == 2
        finally:
            release_read.set()
            for pull in pulls:
                if not pull.done():
                    pull.cancel()
            await asyncio.gather(*pulls, return_exceptions=True)
            await first.aclose()
            await second.aclose()
        # Two followers used one transport listener, released by their final owner.
        async with notifications.subscribe():
            pass


async def test_writer_deadline_deducts_read_time_and_does_not_wait_for_the_repair_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    waits: list[float] = []
    monkeypatch.setattr("tinkerfin_tracing._follow_reads._monotonic", lambda: clock)
    async with Notifications() as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
        writer = await store.open_writer(identity)
        original_read = store.backend.read_event_page

        async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
            nonlocal clock
            page = await original_read(request)
            if clock == 0:
                clock = 2
                return replace(page, next_writer_lease_remaining_seconds=5.0)
            return replace(
                page, active_run_ids=(), next_writer_lease_remaining_seconds=None
            )

        async def expire(wait: Coroutine[None, None, bool], *, timeout: float) -> bool:
            nonlocal clock
            wait.close()
            waits.append(timeout)
            clock = 5.0
            raise TimeoutError

        monkeypatch.setattr(store.backend, "read_event_page", read)
        monkeypatch.setattr(
            "tinkerfin_tracing.durable_store._wait_for_follow_change", expire
        )
        follower = store.follow(writer.key, after_seq=0)
        try:
            assert (await anext(follower)).active_run_ids == ("run",)
            assert (await anext(follower)).active_run_ids == ()
            assert waits == [3.0]
        finally:
            await follower.aclose()
            await writer.aclose()


async def test_thirty_second_repair_reads_a_remote_commit_when_its_hint_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    timeout_entered, repair = asyncio.Event(), asyncio.Event()
    waits: list[float] = []
    monkeypatch.setattr("tinkerfin_tracing._follow_reads._monotonic", lambda: clock)

    async def controlled_repair(
        wait: Coroutine[None, None, bool], *, timeout: float
    ) -> bool:
        nonlocal clock
        wait.close()
        waits.append(timeout)
        timeout_entered.set()
        await repair.wait()
        clock += timeout
        raise TimeoutError

    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change", controlled_repair
    )
    async with Notifications() as notifications:
        origin = InMemoryTraceStore()
        follower_store = DurableTraceStore(origin.backend, notifications=notifications)
        await _write_run(origin, thread="thread", run="first")
        snapshot = await origin.snapshot(
            ThreadIdentity(namespace="test", thread_id="thread")
        )
        follower = follower_store.follow(snapshot.key, after_seq=2)
        assert (await anext(follower)).events == ()
        pull = asyncio.create_task(_next_events(follower))
        try:
            await timeout_entered.wait()
            await _write_run(origin, thread="thread", run="second")
            repair.set()
            assert [event.trace_seq for event in await pull] == [3, 4]
            assert waits == [30.0]
        finally:
            if not pull.done():
                pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
            await follower.aclose()


async def test_replacement_follower_waits_for_the_previous_transport_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    subscriptions = 0

    class ControlledNotifications(Notifications):
        def subscribe(
            self,
            *,
            scope: NotificationScope | None = None,
            topics: Collection[str] | None = None,
            key: str | None = None,
        ) -> NotificationSubscription:
            nonlocal subscriptions
            subscription = super().subscribe(scope=scope, topics=topics, key=key)
            subscriptions += 1
            if subscriptions == 1:
                original_close = subscription.aclose

                async def close() -> None:
                    closing.set()
                    await release.wait()
                    await original_close()

                monkeypatch.setattr(subscription, "aclose", close)
            return subscription

    async with ControlledNotifications(
        limits=NotificationLimits(max_subscriptions=1)
    ) as notifications:
        store = InMemoryTraceStore(notifications=notifications)
        await _write_run(store, thread="thread", run="run")
        snapshot = await store.snapshot(
            ThreadIdentity(namespace="test", thread_id="thread")
        )
        first = store.follow(snapshot.key, after_seq=2)
        assert (await anext(first)).events == ()
        old_close = asyncio.create_task(first.aclose())
        await closing.wait()
        second = store.follow(snapshot.key, after_seq=2)

        async def pull() -> None:
            entered.set()
            assert (await anext(second)).events == ()

        pending = asyncio.create_task(pull())
        try:
            await entered.wait()
            assert not pending.done()
            assert subscriptions == 1
            release.set()
            await old_close
            await pending
            assert subscriptions == 2
        finally:
            release.set()
            await old_close
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await second.aclose()


async def test_unreturned_writer_renews_while_change_publication_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = datetime(2026, 9, 28, tzinfo=UTC)
    publishing, release_publish, renewed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    renew_gate: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return instant.astimezone(tz)

    async def wait_for_renewal(_seconds: float) -> None:
        await renew_gate.get()

    class HeldPublisher(MemoryBackend):
        first = True

        async def publish(self, payload: bytes) -> None:
            if self.first:
                self.first = False
                publishing.set()
                await release_publish.wait()
            await super().publish(payload)

    monkeypatch.setattr("tinkerfin_tracing.durable_store.datetime", ControlledDateTime)
    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_writer_renewal", wait_for_renewal
    )
    async with Notifications(backend=HeldPublisher()) as notifications:
        backend = InMemoryTraceStore().backend
        original_commit = backend.commit_ledger_change

        async def commit(change: TraceLedgerChange) -> TraceLedgerCommitResult:
            result = await original_commit(change)
            if change.kind == "renew_writer":
                renewed.set()
            return result

        monkeypatch.setattr(backend, "commit_ledger_change", commit)
        store = DurableTraceStore(
            backend,
            notifications=notifications,
            options=TraceStoreOptions(
                writer_lease_seconds=5,
                writer_heartbeat_interval_seconds=1,
            ),
        )
        identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
        opening = asyncio.create_task(store.open_writer(identity))
        await publishing.wait()
        instant += timedelta(seconds=4)
        renew_gate.put_nowait(None)
        await renewed.wait()
        instant += timedelta(seconds=2)
        release_publish.set()
        writer = await opening
        try:
            events = await writer.append((_fact(identity, "started"),))
            assert events[0].trace_seq == 1
        finally:
            await writer.aclose()


async def test_coalesced_recreation_hint_invalidates_the_old_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = asyncio.Event()

    async def wait_for_hint(
        wait: Coroutine[None, None, bool], *, timeout: float
    ) -> bool:
        waiting.set()
        return await wait

    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store._wait_for_follow_change", wait_for_hint
    )
    async with Notifications() as notifications:
        origin = InMemoryTraceStore(notifications=notifications)
        consumer = DurableTraceStore(origin.backend, notifications=notifications)
        await _write_run(origin, thread="thread", run="first")
        snapshot = await origin.snapshot(
            ThreadIdentity(namespace="test", thread_id="thread")
        )
        follower = consumer.follow(snapshot.key, after_seq=2)
        assert (await anext(follower)).events == ()
        pull = asyncio.create_task(anext(follower))
        await waiting.wait()
        await origin.delete(snapshot.key)
        replacement = await origin.open_writer(
            RunIdentity(namespace="test", thread_id="thread", run_id="new")
        )
        try:
            with pytest.raises(TraceThreadNotFound):
                await pull
        finally:
            if not pull.done():
                pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
            await follower.aclose()
            await replacement.aclose()
