"""Share bounded Trace reads without owning subscriber cursors or a background loop."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace

from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .backend import (
    StoredTraceEvent,
    StoredTraceEventPage,
    TraceEventPageRequest,
    TraceLedgerBackend,
)
from .store import TraceThreadKey

_MAX_CACHED_BYTES = 64 * 1024 * 1024
_MAX_CACHED_RECORDS = 8192
_MAX_CACHED_PAGES = 1024
_MAX_CONCURRENT_READS = 8


def _monotonic() -> float:
    return asyncio.get_running_loop().time()


@dataclass(frozen=True, slots=True)
class _ReadKey:
    key: TraceThreadKey
    after_seq: int
    revision: int


@dataclass(frozen=True, slots=True)
class _FollowPage:
    page: StoredTraceEventPage
    expires_at: float
    read_order: int
    completed_order: int


class _SharedRead:
    """Keep one accepted read alive until its final waiter has settled it."""

    def __init__(
        self, task: asyncio.Task[TaskOutcome[_FollowPage]], *, order: int
    ) -> None:
        self.task = task
        self.order = order
        self.waiters = 0


class _FollowReads:
    """Coalesce concurrent pulls and reuse validated canonical pages briefly.

    The caller retains each subscriber's cursor and last completed read. Reads are
    shared only within an exact generation and local commit revision. The cache
    bounds retained canonical bytes, records, and pages across the whole Store;
    oversized results are delivered without retention. Decoded facts never enter
    this cache. Each subscriber therefore owns its mutable payload values.

    There is no prefetch task. Active callers jointly own each read, and the last
    departing waiter cancels and joins it before returning. The backend remains
    borrowed. Eight concurrent reads bound database work; existing backend page
    limits still determine transient result memory independently of the cache.

    Read order is local operation order, independent of the storage wall clock.
    A subscriber can keep reading the same cached prefix or use a read created
    after its previous read completed. Older concurrent reads cannot restore
    stale ownership, even when their storage timestamps happen to be equal.
    """

    def __init__(self, backend: TraceLedgerBackend, *, poll_seconds: float) -> None:
        self._backend = backend
        self._poll_seconds = poll_seconds
        self._slots = asyncio.Semaphore(_MAX_CONCURRENT_READS)
        self._reads: dict[_ReadKey, _SharedRead] = {}
        self._pages: OrderedDict[_ReadKey, _FollowPage] = OrderedDict()
        self._cached_bytes = 0
        self._cached_records = 0
        self._read_order = 0

    async def read(
        self,
        request: TraceEventPageRequest,
        *,
        revision: int,
        previous: _FollowPage | None,
    ) -> _FollowPage:
        """Return a prefix without reusing evidence from an earlier independent read."""

        key = _ReadKey(request.key, request.after_seq or 0, revision)
        cached = self._cached_page(key, request.limit, previous=previous)
        if cached is not None:
            return cached
        shared = self._reads.get(key)
        if (
            shared is None
            or shared.task.done()
            or (previous is not None and shared.order < previous.completed_order)
        ):
            self._read_order += 1
            order = self._read_order
            shared = _SharedRead(
                asyncio.create_task(
                    capture(self._read_page(request, order=order)),
                    name="tinkerfin-trace-follow-read",
                ),
                order=order,
            )
            self._reads[key] = shared
        return await self._wait_for_read(key, shared)

    async def _read_page(
        self, request: TraceEventPageRequest, *, order: int
    ) -> _FollowPage:
        async with self._slots:
            started_at = _monotonic()
            page = await self._backend.read_event_page(request)
        # Strip backend-specific subclasses, including the memory backend's
        # decoded-event shortcut. Only immutable canonical evidence is retained.
        records = tuple(
            StoredTraceEvent(
                event_id=record.event_id,
                trace_seq=record.trace_seq,
                run_id=record.run_id,
                fact_kind=record.fact_kind,
                occurred_at=record.occurred_at,
                canonical_payload=record.canonical_payload,
                payload_digest=record.payload_digest,
                persisted_bytes=record.persisted_bytes,
            )
            for record in page.events
        )
        self._read_order += 1
        return _FollowPage(
            page=replace(page, events=records),
            expires_at=started_at + self._poll_seconds,
            read_order=order,
            completed_order=self._read_order,
        )

    async def _wait_for_read(self, key: _ReadKey, shared: _SharedRead) -> _FollowPage:
        shared.waiters += 1
        primary_error: BaseException | None = None
        try:
            await asyncio.shield(shared.task)
            return await join_owned_task(shared.task, cancel_operation=False)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            shared.waiters -= 1
            if not shared.waiters:
                if self._reads.get(key) is shared:
                    self._reads.pop(key)
                cancel_handle = None
                if not shared.task.done():
                    # Let capture enter before cancellation so even immediate
                    # caller cancellation has an owned, observable task outcome.
                    cancel_handle = asyncio.get_running_loop().call_soon(
                        shared.task.cancel
                    )
                try:
                    await join_owned_task(shared.task, cancel_operation=False)
                except BaseException as close_error:
                    if primary_error is None:
                        raise
                    failure = select_failure(primary_error, close_error)
                    if failure is not primary_error:
                        raise failure from primary_error
                finally:
                    if cancel_handle is not None:
                        cancel_handle.cancel()

    def remember(
        self, request: TraceEventPageRequest, result: _FollowPage, *, revision: int
    ) -> None:
        """Retain a validated page without extending its original observation age."""

        if result.expires_at <= _monotonic():
            return
        page = result.page
        size = sum(len(record.canonical_payload) for record in page.events)
        count = len(page.events)
        if size > _MAX_CACHED_BYTES or count > _MAX_CACHED_RECORDS:
            return
        key = _ReadKey(request.key, request.after_seq or 0, revision)
        previous = self._pages.get(key)
        if previous is not None and previous.read_order > result.read_order:
            return
        self._remove_page(key)
        self._pages[key] = result
        self._cached_bytes += size
        self._cached_records += count
        while (
            self._cached_bytes > _MAX_CACHED_BYTES
            or self._cached_records > _MAX_CACHED_RECORDS
            or len(self._pages) > _MAX_CACHED_PAGES
        ):
            self._remove_page(next(iter(self._pages)))

    def discard(self, key: TraceThreadKey) -> None:
        """Forget cached ownership and events after a commit or final departure."""

        for cached_key in tuple(self._pages):
            if cached_key.key == key:
                self._remove_page(cached_key)

    @staticmethod
    def remaining(result: _FollowPage) -> float:
        """Return only the unused part of the original cross-instance read interval."""

        return max(0.0, result.expires_at - _monotonic())

    def _cached_page(
        self, key: _ReadKey, limit: int, *, previous: _FollowPage | None
    ) -> _FollowPage | None:
        now = _monotonic()
        for cached_key, result in reversed(tuple(self._pages.items())):
            if result.expires_at <= now:
                self._remove_page(cached_key)
                continue
            if cached_key.key != key.key or cached_key.revision != key.revision:
                continue
            page = result.page
            if (
                previous is not None
                and result.read_order != previous.read_order
                and result.read_order < previous.completed_order
            ):
                continue
            if key.after_seq > page.tail_seq:
                continue
            if key.after_seq == page.tail_seq:
                records: tuple[StoredTraceEvent, ...] = ()
            elif (
                not page.events
                or key.after_seq < page.events[0].trace_seq - 1
                or key.after_seq >= page.events[-1].trace_seq
            ):
                continue
            else:
                offset = key.after_seq - page.events[0].trace_seq + 1
                records = page.events[offset : offset + limit]
            self._pages.move_to_end(cached_key)
            return replace(result, page=replace(page, events=records))
        return None

    def _remove_page(self, key: _ReadKey) -> None:
        result = self._pages.pop(key, None)
        if result is not None:
            self._cached_bytes -= sum(
                len(record.canonical_payload) for record in result.page.events
            )
            self._cached_records -= len(result.page.events)


__all__ = ["_FollowPage", "_FollowReads"]
