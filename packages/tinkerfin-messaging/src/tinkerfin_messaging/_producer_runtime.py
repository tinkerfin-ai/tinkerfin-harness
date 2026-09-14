"""Owned producer, settlement, recovery, and lease supervision."""

from __future__ import annotations

__all__ = [
    "_codec_id",
    "_open_recoverable_source",
    "_producer_finished",
    "_start_producer",
    "_start_producer_task",
    "_start_recoverable_producer",
]

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Literal, TypeAlias, TypeVar, cast

from ._messaging_boundary import (
    _await_backend,
    _ContextCancelCallback,
    _derived_message_id,
    _invoke_cancel,
    _read_backend,
)
from ._messaging_ledger import PreparedRun
from .errors import BackendOwnershipLost
from .models import RecoverableMessage, RecoveryCheckpoint
from .protocols import (
    MessageCodec,
    MessagePublicationPolicy,
    MessageSource,
    RecoverableSource,
)

if TYPE_CHECKING:
    from .messaging import CommittedCallback, Messaging

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProducedT = TypeVar("ProducedT")

_ProducerFailureStage: TypeAlias = Literal[
    "callback",
    "commit",
    "finish",
    "lease",
    "settlement",
    "shutdown",
    "source",
    "source_close",
    "tail",
]
_LeaseRenewalPhase: TypeAlias = Literal["owner"]
_LeaseRenewalOutcome: TypeAlias = Literal[
    "backend_exception",
    "ownership_rejected",
]

logger = logging.getLogger("tinkerfin.messaging.producer")


@dataclass(slots=True)
class _ProducerState:
    cancel_requested: bool = False
    settling: bool = False
    ownership_lost: bool = False
    ownership_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _ProducedMessage(Generic[SourceT]):
    data: SourceT
    message_id: str
    checkpoint: RecoveryCheckpoint | None


@dataclass(slots=True)
class _PendingCommit(Generic[ProducedT]):
    item: ProducedT
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None


def _lease_schedule(self: Messaging) -> tuple[float | None, float | None]:
    """Read one internally consistent renewal interval and failure budget.

    Backends may disable leases entirely. A timeout without renewal or a timeout no
    greater than the interval cannot prove ownership and is rejected before a producer
    task starts.
    """

    interval_value = _read_backend(
        "lease_renew_interval",
        lambda: self._runtime_backend.lease_renew_interval,
    )
    timeout_value = _read_backend(
        "lease_timeout",
        lambda: self._runtime_backend.lease_timeout,
    )
    if interval_value is None:
        if timeout_value is not None:
            raise ValueError("lease_timeout requires lease_renew_interval")
        return None, None
    if isinstance(interval_value, bool) or not isinstance(interval_value, int | float):
        raise TypeError("lease_renew_interval must be numeric or None")
    interval = float(interval_value)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("lease_renew_interval must be finite and positive")
    if timeout_value is None:
        return interval, None
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, int | float):
        raise TypeError("lease_timeout must be numeric or None")
    timeout = float(timeout_value)
    if not math.isfinite(timeout) or timeout <= interval:
        raise ValueError("lease_timeout must be finite and greater than renew interval")
    return interval, timeout


async def _renew_lease_forever(
    self: Messaging,
    *,
    prepared: PreparedRun,
    phase: _LeaseRenewalPhase,
    interval: float,
    timeout: float | None,
    initial_last_success: float,
) -> None:
    """Renew one owner and log the first failure with scheduler-safe evidence."""

    loop = asyncio.get_running_loop()
    last_success = initial_last_success
    wake_target = loop.time() + interval
    attempt = 0
    while True:
        expected_target = last_success + interval
        await asyncio.sleep(max(0.0, wake_target - loop.time()))
        started = loop.time()
        attempt += 1
        try:
            renewed = await _await_backend(
                "renew",
                self._runtime_backend.renew(prepared.handle),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            finished = loop.time()
            _log_lease_failure(
                phase=phase,
                outcome="backend_exception",
                attempt=attempt,
                scheduler_delay=max(0.0, started - expected_target),
                command_duration=max(0.0, finished - started),
                since_last_success=max(0.0, finished - last_success),
                timeout=timeout,
                error=error,
            )
            raise
        finished = loop.time()
        if not renewed:
            error = BackendOwnershipLost(
                f"Producer for run {prepared.handle.identity.run_id!r} lost its lease"
            )
            _log_lease_failure(
                phase=phase,
                outcome="ownership_rejected",
                attempt=attempt,
                scheduler_delay=max(0.0, started - expected_target),
                command_duration=max(0.0, finished - started),
                since_last_success=max(0.0, finished - last_success),
                timeout=timeout,
                error=error,
            )
            raise error
        last_success = finished
        wake_target = last_success + interval


def _log_lease_failure(
    *,
    phase: _LeaseRenewalPhase,
    outcome: _LeaseRenewalOutcome,
    attempt: int,
    scheduler_delay: float,
    command_duration: float,
    since_last_success: float,
    timeout: float | None,
    error: BaseException,
) -> None:
    """Record trusted scheduling evidence without serializing message payloads.

    Scheduler delay, command duration, and time since the last success distinguish an
    overloaded event loop from a backend rejection. The log is operational telemetry
    and never enters durable envelopes or the user Trace Ledger.
    """

    deadline_elapsed = timeout is not None and since_last_success >= timeout
    logger.error(
        "Messaging producer lease renewal failed",
        extra={
            "tinkerfin_renewal_phase": phase,
            "tinkerfin_renewal_outcome": outcome,
            "tinkerfin_attempt": attempt,
            "tinkerfin_scheduler_delay_seconds": scheduler_delay,
            "tinkerfin_command_duration_seconds": command_duration,
            "tinkerfin_seconds_since_last_success": since_last_success,
            "tinkerfin_lease_timeout_seconds": timeout,
            "tinkerfin_deadline_elapsed": deadline_elapsed,
            "tinkerfin_error_type": type(error).__name__,
        },
    )


class _OwnerLease:
    """Keep one acquired owner alive through preparation, publication and cleanup.

    The preflight owns this supervisor until it synchronously transfers it to the
    producer. A failure interrupts only the currently protected operation; settlement
    disarms that interruption and still joins the renewal task. No caller must renew
    or coordinate a handoff.
    """

    def __init__(self, messaging: Messaging, prepared: PreparedRun) -> None:
        self.error: BaseException | None = None
        self._interrupt: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        interval, timeout = _lease_schedule(messaging)
        if interval is not None:
            baseline = asyncio.get_running_loop().time()

            async def renew() -> None:
                try:
                    await _renew_lease_forever(
                        messaging,
                        prepared=prepared,
                        phase="owner",
                        interval=interval,
                        timeout=timeout,
                        initial_last_success=baseline,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as error:  # noqa: BLE001 - returned to the owner
                    self.error = error
                    interrupt = self._interrupt
                    if interrupt is not None:
                        interrupt()

            self._task = asyncio.create_task(
                renew(),
                name=f"tinkerfin-messaging-lease:{prepared.handle.identity.run_id}",
            )

    def protect(self, interrupt: Callable[[], None] | None) -> None:
        """Transfer failure interruption without stopping or restarting renewal."""

        self._interrupt = interrupt
        if interrupt is not None and self.error is not None:
            interrupt()

    def check(self) -> None:
        """Reject publication after a renewal failure, even during handoff."""

        if self.error is not None:
            raise self.error.with_traceback(self.error.__traceback__)

    async def aclose(self) -> None:
        """Join the only renewal task after durable or failed settlement."""

        self.protect(None)
        task = self._task
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _start_producer(
    self: Messaging,
    *,
    prepared: PreparedRun,
    lease: _OwnerLease,
    source: MessageSource[ProducedT],
    codec: MessageCodec[SourceT, ReplayT],
    codec_input: Callable[[ProducedT], SourceT] | None,
    cancel: _ContextCancelCallback[ProducedT] | None,
    on_committed: CommittedCallback | None,
) -> asyncio.Event:
    """Start a direct producer with one optional source-owned normalization hook.

    ``codec_input`` runs in the bounded commit path and settles before another source
    pull. This ordering lets a Runtime transfer a single-use canonical frame without
    copying or reparsing its live provider object in Messaging.
    """

    def prepare_item(item: ProducedT, ordinal: int) -> _ProducedMessage[SourceT]:
        data = cast(SourceT, item) if codec_input is None else codec_input(item)
        return _ProducedMessage(
            data=data,
            message_id=_derived_message_id(prepared.handle.identity, ordinal),
            checkpoint=None,
        )

    return self._start_producer_task(
        prepared=prepared,
        lease=lease,
        source=source,
        codec=codec,
        cancel=cancel,
        on_committed=on_committed,
        prepare_item=prepare_item,
    )


@dataclass(frozen=True, slots=True)
class _SourceOpenOutcome(Generic[SourceT]):
    source: MessageSource[RecoverableMessage[SourceT]] | None = None
    error: BaseException | None = None


async def _open_recoverable_source(
    messaging: Messaging,
    source: RecoverableSource[SourceT],
    prepared: PreparedRun,
    lease: _OwnerLease,
) -> MessageSource[RecoverableMessage[SourceT]]:
    """Join reconstruction and close any late result when ownership is lost.

    Reconstruction has a separate task so simultaneous opening and lease failures
    both remain observable. Exceptions travel as data because asyncio.shield may log
    a late child failure after cancellation (Python 3.14). The caller retains renewal
    throughout reconstruction and cleanup.
    """

    if lease._task is None:
        return await source.open(prepared.checkpoint)

    async def open_source() -> _SourceOpenOutcome[SourceT]:
        try:
            return _SourceOpenOutcome(source=await source.open(prepared.checkpoint))
        except BaseException as error:  # noqa: BLE001 - re-raised by the owner
            return _SourceOpenOutcome(error=error)

    opening = asyncio.create_task(
        open_source(),
        name=f"tinkerfin-messaging-source-open:{prepared.handle.identity.run_id}",
    )
    # The child owns reconstruction, including its cancellation cleanup. Naming it
    # temporarily lets an opener close Messaging without waiting on its own preflight.
    # The parent resumes ownership only after that child settles, before source close
    # or ready callbacks execute.
    parent = asyncio.current_task()
    registration = next(
        (item for item in messaging._preflight_tasks if item.owner is parent),
        None,
    )
    if registration is not None:
        messaging._bind_preflight_owner(
            registration, cast(asyncio.Task[object], opening)
        )
    claimed = False
    failure: BaseException | None = None
    try:
        outcome = await asyncio.shield(opening)
        if outcome.error is not None:
            raise outcome.error
        lease.check()
        assert outcome.source is not None
        claimed = True
        return outcome.source
    except BaseException as error:
        failure = error
        if (
            isinstance(error, asyncio.CancelledError)
            and lease.error is not None
            and opening.done()
            and not opening.cancelled()
        ):
            opening_error = opening.result().error
            if opening_error is not None:
                failure = opening_error
                raise opening_error
        raise
    finally:
        if not claimed:
            lease.protect(None)
        if not opening.done():
            opening.cancel()
        try:
            (result,) = await asyncio.gather(opening, return_exceptions=True)
        finally:
            if registration is not None:
                assert parent is not None
                messaging._bind_preflight_owner(
                    registration, cast(asyncio.Task[object], parent)
                )
        if not claimed and isinstance(result, _SourceOpenOutcome):
            if result.source is not None:
                await result.source.aclose()
            secondary = result.error
            if (
                failure is not None
                and secondary is not None
                and secondary is not failure
                and not isinstance(secondary, asyncio.CancelledError)
            ):
                failure.add_note(
                    "Source reconstruction cleanup also failed: "
                    f"{type(secondary).__name__}: {secondary}"
                )


def _start_recoverable_producer(
    self: Messaging,
    *,
    prepared: PreparedRun,
    lease: _OwnerLease,
    source: MessageSource[RecoverableMessage[SourceT]],
    codec: MessageCodec[SourceT, ReplayT],
    cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None,
    on_committed: CommittedCallback | None,
) -> asyncio.Event:
    """Start a producer whose caller supplies stable IDs and atomic checkpoints.

    Recovery ignores local ordinals: each ``RecoverableMessage`` must bind its own ID
    to the checkpoint written in the same backend append, making retry idempotency and
    source resume position one transaction.
    """

    def prepare_item(
        item: RecoverableMessage[SourceT],
        ordinal: int,
    ) -> _ProducedMessage[SourceT]:
        del ordinal
        if item.checkpoint.last_message_id != item.message_id:
            raise ValueError(
                "RecoverableMessage checkpoint.last_message_id must match message_id"
            )
        return _ProducedMessage(
            data=item.data,
            message_id=item.message_id,
            checkpoint=item.checkpoint,
        )

    return self._start_producer_task(
        prepared=prepared,
        lease=lease,
        source=source,
        codec=codec,
        cancel=cancel,
        on_committed=on_committed,
        prepare_item=prepare_item,
    )


def _start_producer_task(
    self: Messaging,
    *,
    prepared: PreparedRun,
    lease: _OwnerLease,
    source: MessageSource[ProducedT],
    codec: MessageCodec[SourceT, ReplayT],
    cancel: _ContextCancelCallback[ProducedT] | None,
    on_committed: CommittedCallback | None,
    prepare_item: Callable[[ProducedT, int], _ProducedMessage[SourceT]],
) -> asyncio.Event:
    """Own source pull, bounded commit, lease, cancellation, and final settlement.

    At most one item occupies the commit path, and the source is not pulled again until
    that append acknowledges, preserving backpressure and any single-use codec sidecar.
    Backend settlement arbitrates natural completion versus remote cancellation; only
    the winner may append a cancellation tail. Lease loss fences further commits, while
    every task and source is joined or closed before the producer records its terminal
    status.
    """

    from .messaging import CancelContext

    state = _ProducerState()
    started = asyncio.Event()
    cancel_context = CancelContext(
        channel=prepared.handle.channel,
        identity=prepared.handle.identity,
    )

    async def produce() -> None:
        # `wrap()` waits for this first turn so later task cancellation always
        # enters the producer's cleanup path and cannot strand source ownership.
        started.set()
        producer_task = asyncio.current_task()
        if producer_task is None:
            raise RuntimeError("Messaging producer requires an asyncio task")
        status: Literal["completed", "cancelled", "failed"] = "completed"
        error: BaseException | None = None
        error_stage: _ProducerFailureStage | None = None
        cancel_watcher: asyncio.Task[Iterable[ProducedT] | None] | None = None
        cancel_callback_task: asyncio.Task[Iterable[ProducedT] | None] | None = None
        settlement_task: asyncio.Task[bool] | None = None
        pending_commits: asyncio.Queue[_PendingCommit[ProducedT]] = asyncio.Queue(
            maxsize=1
        )
        commit_capacity = asyncio.Semaphore(1)
        commit_error: BaseException | None = None
        next_ordinal = 1
        shutdown_requested = False

        def retain_secondary_failure(
            *,
            stage: _ProducerFailureStage,
            secondary: BaseException,
        ) -> None:
            primary = error
            primary_stage = error_stage
            if primary is None or primary_stage is None:
                raise RuntimeError(
                    "Messaging cannot retain a secondary failure without a primary"
                )
            primary.add_note(
                f"Messaging {stage} also failed after {primary_stage}: "
                f"{type(secondary).__name__}: {secondary}"
            )
            logger.error(
                "Messaging producer retained a secondary failure",
                extra={
                    "tinkerfin_primary_stage": primary_stage,
                    "tinkerfin_secondary_stage": stage,
                    "tinkerfin_ownership_lost": isinstance(
                        secondary,
                        BackendOwnershipLost,
                    ),
                    "tinkerfin_error_type": type(secondary).__name__,
                },
            )

        def record_failure(
            *,
            stage: _ProducerFailureStage,
            failure: BaseException,
        ) -> None:
            nonlocal error, error_stage, status
            if error is None:
                error = failure
                error_stage = stage
                status = "failed"
                return
            if (
                error_stage == "shutdown"
                and isinstance(error, asyncio.CancelledError)
                and not isinstance(failure, asyncio.CancelledError)
            ):
                failure.add_note(
                    "Messaging shutdown also cancelled the producer before "
                    f"{stage} failed: {error}"
                )
                error = failure
                error_stage = stage
                status = "failed"
                return
            if failure is error:
                return
            retain_secondary_failure(stage=stage, secondary=failure)

        async def commit_messages() -> None:
            nonlocal commit_error, next_ordinal
            while True:
                pending = await pending_commits.get()
                try:
                    if commit_error is not None:
                        pending.error = commit_error
                        continue
                    produced = prepare_item(pending.item, next_ordinal)
                    payload = codec.encode(produced.data)
                    if not isinstance(payload, bytes):
                        raise TypeError("MessageCodec.encode() must return bytes")
                    envelope = await _await_backend(
                        "append",
                        self._runtime_backend.append(
                            prepared.handle,
                            message_id=produced.message_id,
                            codec=self._codec_id(codec),
                            payload=payload,
                            checkpoint=produced.checkpoint,
                            opens_publication=(
                                not isinstance(codec, MessagePublicationPolicy)
                                or cast(
                                    MessagePublicationPolicy[SourceT], codec
                                ).starts_publication(
                                    produced.data, identity=prepared.handle.identity
                                )
                            ),
                            closes_publication=(
                                isinstance(codec, MessagePublicationPolicy)
                                and cast(
                                    MessagePublicationPolicy[SourceT], codec
                                ).ends_publication(
                                    produced.data, identity=prepared.handle.identity
                                )
                            ),
                        ),
                    )
                    next_ordinal += 1
                    if on_committed is not None:
                        try:
                            await on_committed(envelope)
                        except Exception as observer_error:  # noqa: BLE001 - observer isolation
                            logger.error(
                                "Messaging committed hook failed",
                                extra={
                                    "tinkerfin_error_type": type(
                                        observer_error
                                    ).__name__,
                                },
                            )
                except asyncio.CancelledError as append_cancellation:
                    pending.error = append_cancellation
                    raise
                except BaseException as append_error:  # noqa: BLE001 - producer outcome
                    if commit_error is None:
                        commit_error = append_error
                    pending.error = commit_error
                finally:
                    pending.acknowledged.set()
                    commit_capacity.release()
                    pending_commits.task_done()

        committer = asyncio.create_task(
            commit_messages(),
            name=(f"tinkerfin-messaging-committer:{prepared.handle.identity.run_id}"),
        )

        def enqueue_acquired(item: ProducedT) -> _PendingCommit[ProducedT]:
            pending = _PendingCommit(item=item)
            try:
                pending_commits.put_nowait(pending)
            except BaseException:
                commit_capacity.release()
                raise
            return pending

        async def wait_committed(pending: _PendingCommit[ProducedT]) -> None:
            await pending.acknowledged.wait()
            if pending.error is not None:
                raise pending.error.with_traceback(pending.error.__traceback__)

        async def submit_tail(item: ProducedT) -> None:
            await commit_capacity.acquire()
            pending = enqueue_acquired(item)
            await wait_committed(pending)

        async def consume_source() -> None:
            consumer_task = asyncio.current_task()
            if consumer_task is None:
                raise RuntimeError("Producer source requires an asyncio task")
            iterator = aiter(source)
            while True:
                await commit_capacity.acquire()
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    commit_capacity.release()
                    return
                except BaseException:
                    commit_capacity.release()
                    raise
                pending = enqueue_acquired(item)
                await wait_committed(pending)
                if consumer_task.cancelling():
                    raise asyncio.CancelledError

        source_consumer = asyncio.create_task(
            consume_source(),
            name=(f"tinkerfin-messaging-source:{prepared.handle.identity.run_id}"),
        )

        def claim_settlement() -> asyncio.Task[bool]:
            nonlocal settlement_task
            existing = settlement_task
            if existing is not None:
                return existing
            settlement_task = asyncio.create_task(
                _await_backend(
                    "begin_settlement",
                    self._runtime_backend.begin_settlement(prepared.handle),
                ),
                name=(
                    f"tinkerfin-messaging-settlement:{prepared.handle.identity.run_id}"
                ),
            )
            return settlement_task

        def claim_cancel_callback() -> tuple[
            asyncio.Task[Iterable[ProducedT] | None],
            bool,
        ]:
            nonlocal cancel_callback_task
            existing = cancel_callback_task
            if existing is not None:
                return existing, False
            callback = cancel
            if callback is None:
                raise RuntimeError(
                    "backend accepted cancellation without a registered callback"
                )
            state.cancel_requested = True
            self._settling_producers.add(cast(asyncio.Task[None], producer_task))

            async def invoke() -> Iterable[ProducedT] | None:
                try:
                    return await _invoke_cancel(callback, cancel_context)
                except BaseException:
                    if not source_consumer.done():
                        source_consumer.cancel()
                    raise

            cancel_callback_task = asyncio.create_task(
                invoke(),
                name=(
                    "tinkerfin-messaging-cancel-callback:"
                    f"{prepared.handle.identity.run_id}"
                ),
            )
            return cancel_callback_task, True

        async def watch_cancel() -> Iterable[ProducedT] | None:
            requested = await _await_backend(
                "wait_for_cancel",
                self._runtime_backend.wait_for_cancel(prepared.handle),
            )
            if not requested:
                return None
            cancel_accepted = await asyncio.shield(claim_settlement())
            if not cancel_accepted:
                return None
            callback_task, _ = claim_cancel_callback()
            return await callback_task

        if cancel is not None:
            cancel_watcher = asyncio.create_task(
                watch_cancel(),
                name=(f"tinkerfin-messaging-cancel:{prepared.handle.identity.run_id}"),
            )

        def ownership_lost() -> None:
            state.ownership_lost = True
            state.ownership_error = lease.error
            source_consumer.cancel()

        lease.protect(ownership_lost)
        try:
            await asyncio.shield(source_consumer)
            if state.cancel_requested:
                status = "cancelled"
        except asyncio.CancelledError as cancellation:
            producer_task = asyncio.current_task()
            shutdown_requested = (
                producer_task is not None and producer_task.cancelling() > 0
            )
            if state.ownership_lost:
                record_failure(
                    stage="lease",
                    failure=state.ownership_error or cancellation,
                )
            else:
                status = "cancelled" if state.cancel_requested else "failed"
                if state.cancel_requested:
                    error = None
                    error_stage = None
                else:
                    error = cancellation
                    error_stage = "shutdown" if shutdown_requested else "source"
        except BackendOwnershipLost as ownership_error:
            state.ownership_lost = True
            record_failure(stage="lease", failure=ownership_error)
        except BaseException as producer_error:  # noqa: BLE001 - record producer outcome
            record_failure(
                stage=("commit" if producer_error is commit_error else "source"),
                failure=producer_error,
            )
        finally:
            producer_task = asyncio.current_task()
            if producer_task is not None:
                self._settling_producers.add(cast(asyncio.Task[None], producer_task))
            cancel_won = False
            try:
                cancel_won = await asyncio.shield(claim_settlement())
            except BackendOwnershipLost as ownership_error:
                state.ownership_lost = True
                record_failure(stage="settlement", failure=ownership_error)
            except BaseException as settlement_error:  # noqa: BLE001
                record_failure(stage="settlement", failure=settlement_error)
            state.settling = True
            lease.protect(None)
            cancel_watcher_settled = False
            cancel_error: BaseException | None = None
            cancel_tail: Iterable[ProducedT] | None = None

            if cancel_won:
                if not state.ownership_lost and (
                    status == "completed"
                    or shutdown_requested
                    or isinstance(error, asyncio.CancelledError)
                ):
                    status = "cancelled"
                    error = None
                    error_stage = None
                try:
                    callback_task, claimed_here = claim_cancel_callback()
                except BaseException as callback_error:  # noqa: BLE001
                    cancel_error = callback_error
                else:
                    if (
                        claimed_here
                        and cancel_watcher is not None
                        and not cancel_watcher.done()
                    ):
                        cancel_watcher.cancel()
                        await asyncio.gather(
                            cancel_watcher,
                            return_exceptions=True,
                        )
                        cancel_watcher_settled = True
                    try:
                        cancel_tail = await callback_task
                    except BaseException as callback_error:  # noqa: BLE001
                        cancel_error = callback_error
                    if cancel_watcher is not None and not cancel_watcher_settled:
                        await asyncio.gather(
                            cancel_watcher,
                            return_exceptions=True,
                        )
                        cancel_watcher_settled = True
            elif cancel_watcher is not None:
                if not cancel_watcher.done():
                    cancel_watcher.cancel()
                await asyncio.gather(
                    cancel_watcher,
                    return_exceptions=True,
                )
                cancel_watcher_settled = True

            if not source_consumer.done():
                source_consumer.cancel()
            await asyncio.gather(source_consumer, return_exceptions=True)

            if shutdown_requested and not cancel_won:
                if cancel_watcher is not None:
                    cancel_watcher_settled = True
                if not committer.done():
                    committer.cancel()
                await asyncio.gather(committer, return_exceptions=True)
                while True:
                    try:
                        pending = pending_commits.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    pending.error = asyncio.CancelledError()
                    pending.acknowledged.set()
                    commit_capacity.release()
                    pending_commits.task_done()
            if cancel_error is not None:
                record_failure(stage="callback", failure=cancel_error)
            elif cancel_tail is not None:
                try:
                    for item in cancel_tail:
                        await submit_tail(item)
                except BaseException as tail_error:  # noqa: BLE001
                    record_failure(
                        stage=("commit" if tail_error is commit_error else "tail"),
                        failure=tail_error,
                    )
            await pending_commits.join()
            if commit_error is not None:
                record_failure(stage="commit", failure=commit_error)
            if not committer.done():
                committer.cancel()
            committer_result = await asyncio.gather(
                committer,
                return_exceptions=True,
            )
            unexpected_committer_error = next(
                (
                    result
                    for result in committer_result
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ),
                None,
            )
            if unexpected_committer_error is not None:
                record_failure(
                    stage="commit",
                    failure=unexpected_committer_error,
                )
            try:
                await source.aclose()
            except BaseException as close_error:  # noqa: BLE001 - cleanup is part of outcome
                record_failure(stage="source_close", failure=close_error)
            try:
                await _await_backend(
                    "finish",
                    self._runtime_backend.finish(
                        prepared.handle,
                        status=status,
                        error=error,
                    ),
                )
            except BaseException as finish_error:
                if error is None:
                    raise
                record_failure(stage="finish", failure=finish_error)
                assert error is not None
                raise error.with_traceback(error.__traceback__)
            finally:
                if cancel_watcher is not None and not cancel_watcher_settled:
                    if not cancel_watcher.done():
                        cancel_watcher.cancel()
                    await asyncio.gather(
                        cancel_watcher,
                        return_exceptions=True,
                    )
                await lease.aclose()

    task = asyncio.create_task(
        produce(),
        name=(f"tinkerfin-messaging-producer:{prepared.handle.identity.run_id}"),
    )
    self._producer_tasks.add(task)
    task.add_done_callback(self._producer_finished)
    return started


def _producer_finished(self: Messaging, task: asyncio.Task[None]) -> None:
    """Remove and consume a settled owned task to prevent orphan warnings."""

    self._producer_tasks.discard(task)
    self._settling_producers.discard(task)
    if not task.cancelled():
        task.exception()


def _codec_id(codec: MessageCodec[SourceT, ReplayT]) -> str:
    return codec.codec_id
