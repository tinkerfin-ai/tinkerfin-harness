"""Bounded asynchronous Trace batching with explicit settlement ownership."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .codec import CanonicalTracePayloadCodec
from .errors import TraceQuotaExceeded, TraceStoreProtocolError
from .facts import TraceEvent, TraceSemanticFact
from .store import TraceWriter

CommittedBatchObserver = Callable[[tuple[TraceEvent, ...]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TraceWritePolicy:
    """Configure bounded batching without changing Store transaction semantics.

    Attributes:
        max_batch_events: Greatest number of facts combined in one Store append.
        max_batch_bytes: Approximate encoded fact bytes combined in one append. A
            single larger fact is still submitted alone for the Store's exact event
            limit check.
        max_pending_events: Maximum accepted facts across queued and in-flight work.
        max_pending_bytes: Maximum encoded fact bytes across queued and in-flight work.
            The default is the framework's 64 MiB backpressure budget.
        max_batch_delay_seconds: Maximum time the worker waits for additional facts
            after receiving the first item of a batch.
    """

    max_batch_events: int = 256
    max_batch_bytes: int = 4 * 1024 * 1024
    max_pending_events: int = 4096
    max_pending_bytes: int = 64 * 1024 * 1024
    max_batch_delay_seconds: float = 0.01

    def __post_init__(self) -> None:
        """Reject policies that cannot provide a finite queue and transaction batch."""

        for name in (
            "max_batch_events",
            "max_batch_bytes",
            "max_pending_events",
            "max_pending_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_batch_events > self.max_pending_events:
            raise ValueError("max_batch_events must not exceed max_pending_events")
        if self.max_batch_bytes > self.max_pending_bytes:
            raise ValueError("max_batch_bytes must not exceed max_pending_bytes")
        delay = self.max_batch_delay_seconds
        if (
            isinstance(delay, bool)
            or not isinstance(delay, int | float)
            or delay < 0
            or delay == float("inf")
            or delay != delay
        ):
            raise ValueError("max_batch_delay_seconds must be finite and non-negative")


@dataclass(slots=True)
class _PendingFacts:
    facts: tuple[TraceSemanticFact, ...]
    encoded_bytes: int
    committed: asyncio.Future[None]


class TraceBatchWriter:
    """Own one Run's queue, worker task, commits, and close settlement.

    Ordinary submissions return after bounded admission. Store or checkpoint failures
    surface through ``failure_waiter`` and at every force boundary. Mandatory terminal
    facts first force all ordinary work, then use their own Store transaction so the
    Store's reserved-capacity contract remains exact.
    """

    def __init__(
        self,
        writer: TraceWriter,
        *,
        policy: TraceWritePolicy,
        on_committed: CommittedBatchObserver,
    ) -> None:
        """Bind one owned Store writer, immutable policy, and checkpoint observer."""

        if not isinstance(writer, TraceWriter):
            raise TypeError("writer must implement TraceWriter")
        if not isinstance(policy, TraceWritePolicy):
            raise TypeError("policy must be a TraceWritePolicy")
        if not callable(on_committed):
            raise TypeError("on_committed must be callable")
        self._writer = writer
        self._admission_codec = CanonicalTracePayloadCodec()
        self._policy = policy
        self._on_committed = on_committed
        self._condition = asyncio.Condition()
        self._queue: deque[_PendingFacts] = deque()
        self._pending_events = 0
        self._pending_bytes = 0
        self._queued_events = 0
        self._queued_bytes = 0
        self._force_requested = False
        self._closing = False
        self._closed = False
        self._last_commit: asyncio.Future[None] | None = None
        self._failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )
        self._worker: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._mandatory_lock = asyncio.Lock()

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        """Return the active background failure signal owned by this writer."""

        return self._failure

    async def submit(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool,
    ) -> None:
        """Admit ordinary facts or synchronously settle a mandatory terminal batch."""

        if not facts:
            return
        if mandatory:
            async with self._mandatory_lock:
                await self.force()
                self._raise_if_failed()
                try:
                    events = await self._writer.append(facts, mandatory=True)
                    await self._notify_committed(events)
                except BaseException as error:
                    self._record_failure(error)
                    raise
            return
        snapshotted_facts = tuple(fact.model_copy(deep=True) for fact in facts)
        encoded_bytes = sum(
            len(self._admission_codec.encode_fact(fact).data)
            for fact in snapshotted_facts
        )
        if (
            len(facts) > self._policy.max_pending_events
            or encoded_bytes > self._policy.max_pending_bytes
        ):
            raise TraceQuotaExceeded(
                "Trace observation exceeds the pending writer budget",
                context={"resource": "writer_pending"},
            )
        committed = asyncio.get_running_loop().create_future()
        async with self._condition:
            self._require_open()
            while (
                self._pending_events + len(facts) > self._policy.max_pending_events
                or self._pending_bytes + encoded_bytes > self._policy.max_pending_bytes
            ):
                self._raise_if_failed()
                await self._condition.wait()
                self._require_open()
            pending = _PendingFacts(
                facts=snapshotted_facts,
                encoded_bytes=encoded_bytes,
                committed=committed,
            )
            self._queue.append(pending)
            self._pending_events += len(facts)
            self._pending_bytes += encoded_bytes
            self._queued_events += len(facts)
            self._queued_bytes += encoded_bytes
            self._last_commit = committed
            self._ensure_worker()
            self._condition.notify_all()

    async def force(self) -> None:
        """Wait until every fact accepted before this call is durably checkpointed."""

        async with self._condition:
            self._raise_if_failed()
            target = self._last_commit
            if target is None or target.done():
                if target is not None:
                    target.result()
                return
            self._force_requested = True
            self._condition.notify_all()
        await asyncio.shield(target)
        self._raise_if_failed()

    async def aclose(self) -> None:
        """Retain one close task so caller cancellation cannot orphan settlement."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name=f"tinkerfin-trace-writer-close:{self._writer.run_id}",
            )
            task.add_done_callback(self._close_finished)
            self._close_task = task
        await asyncio.shield(task)

    async def _close_once(self) -> None:
        primary: BaseException | None = None
        try:
            await self.force()
        except BaseException as error:  # noqa: BLE001 - close must retain cancellation
            recorded = (
                self._failure.result()
                if self._failure.done() and not self._failure.cancelled()
                else None
            )
            if error is not recorded:
                primary = error
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
        worker = self._worker
        if worker is not None:
            try:
                await worker
            except BaseException as error:  # noqa: BLE001 - owned worker settlement
                if primary is None:
                    primary = error
                elif error is not primary:
                    primary.add_note(
                        "Trace batch worker also failed during close: "
                        f"{type(error).__name__}: {error}"
                    )
        try:
            await self._writer.aclose()
        except BaseException as error:  # noqa: BLE001 - Store writer must still close
            if primary is None:
                primary = error
            else:
                primary.add_note(
                    "Trace Store writer close also failed: "
                    f"{type(error).__name__}: {error}"
                )
        self._closed = True
        if not self._failure.done():
            self._failure.cancel()
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)

    async def _run(self) -> None:
        try:
            while True:
                batch = await self._next_batch()
                if batch is None:
                    return
                facts = tuple(fact for item in batch for fact in item.facts)
                try:
                    events = await self._writer.append(facts, mandatory=False)
                    await self._notify_committed(events)
                except BaseException as error:  # noqa: BLE001 - producer outcome
                    async with self._condition:
                        self._record_failure(error)
                        self._settle_failed(batch, error)
                        while self._queue:
                            self._settle_failed((self._pop_queued(),), error)
                        self._condition.notify_all()
                    return
                async with self._condition:
                    for item in batch:
                        self._pending_events -= len(item.facts)
                        self._pending_bytes -= item.encoded_bytes
                        if not item.committed.done():
                            item.committed.set_result(None)
                    self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            async with self._condition:
                self._record_failure(error)
                while self._queue:
                    self._settle_failed((self._pop_queued(),), error)
                self._condition.notify_all()
            raise

    async def _next_batch(self) -> tuple[_PendingFacts, ...] | None:
        async with self._condition:
            while not self._queue:
                if self._closing:
                    return None
                await self._condition.wait()
            deadline = (
                asyncio.get_running_loop().time() + self._policy.max_batch_delay_seconds
            )
            while not self._force_requested and not self._closing:
                if (
                    self._queued_events >= self._policy.max_batch_events
                    or self._queued_bytes >= self._policy.max_batch_bytes
                ):
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    async with asyncio.timeout(remaining):
                        await self._condition.wait()
                except TimeoutError:
                    break
            selected: list[_PendingFacts] = []
            selected_events = 0
            selected_bytes = 0
            while self._queue:
                candidate = self._queue[0]
                would_exceed = selected and (
                    selected_events + len(candidate.facts)
                    > self._policy.max_batch_events
                    or selected_bytes + candidate.encoded_bytes
                    > self._policy.max_batch_bytes
                )
                if would_exceed:
                    break
                selected.append(self._pop_queued())
                selected_events += len(candidate.facts)
                selected_bytes += candidate.encoded_bytes
            if not self._queue:
                self._force_requested = False
            return tuple(selected)

    def _pop_queued(self) -> _PendingFacts:
        """Remove one queued submission and update only queue-local counters."""

        item = self._queue.popleft()
        self._queued_events -= len(item.facts)
        self._queued_bytes -= item.encoded_bytes
        return item

    async def _notify_committed(self, events: tuple[TraceEvent, ...]) -> None:
        if not events:
            raise TraceStoreProtocolError(
                "Trace Store committed an empty batch for non-empty facts"
            )
        await self._on_committed(events)

    def _settle_failed(
        self,
        batch: tuple[_PendingFacts, ...],
        error: BaseException,
    ) -> None:
        for item in batch:
            self._pending_events -= len(item.facts)
            self._pending_bytes -= item.encoded_bytes
            if not item.committed.done():
                item.committed.set_exception(error)
                item.committed.exception()

    def _record_failure(self, error: BaseException) -> None:
        if not self._failure.done():
            self._failure.set_result(error)

    def _raise_if_failed(self) -> None:
        if self._failure.done() and not self._failure.cancelled():
            error = self._failure.result()
            raise error.with_traceback(error.__traceback__)

    def _require_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("Trace batch writer is closed")
        self._raise_if_failed()

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        worker = asyncio.create_task(
            self._run(),
            name=f"tinkerfin-trace-writer:{self._writer.run_id}",
        )
        worker.add_done_callback(self._worker_finished)
        self._worker = worker

    @staticmethod
    def _worker_finished(task: asyncio.Task[None]) -> None:
        """Consume an owned worker failure after its failure Future is signalled."""

        if not task.cancelled():
            task.exception()

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure when no caller retries close."""

        if not task.cancelled():
            task.exception()


__all__ = ["TraceWritePolicy"]
