"""Owned asynchronous supervisor for due Automation execution work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TypeAlias, TypeVar
from uuid import uuid4

from ._tasks import (
    TaskOutcome,
    capture,
    capture_call,
    join_owned_task,
    select_failure,
    stop_owned_tasks,
    task_result,
)
from .clock import AutomationClock, SystemClock
from .errors import (
    AutomationError,
    AutomationLifecycleError,
    InterruptCallbackError,
    TargetExecutionError,
    TargetNotFoundError,
)
from .models import (
    ExecutionFailure,
    ExecutionStatus,
    InterruptedExecution,
)
from .service import AutomationService
from .store import WorkItemClaim, WorkKind
from .targets import (
    AutomationTarget,
    ExecutionFailed,
    ExecutionInterrupted,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionSucceeded,
    ExecutionUncertain,
)

_ResultT = TypeVar("_ResultT")
OnInterrupt: TypeAlias = Callable[
    [InterruptedExecution], Awaitable[ExecutionFailure | None]
]


class _ExecutionDeadline(Exception):
    pass


class _ExecutionCancellation(Exception):
    pass


@dataclass(slots=True)
class _OwnedExecution:
    task: asyncio.Task[TaskOutcome[None]]
    cancel_requested: asyncio.Event


class AutomationEngine:
    """Supervise bounded target execution for one Automation service.

    The engine owns the service Scheduler lifecycle and every asyncio task it creates.
    Store claims reserve cross-worker capacity; the local task bound prevents one
    process from exceeding the configured global concurrency. Interrupts are recorded
    and optionally classified, but the engine never approves, rejects, or resumes a
    graph.
    """

    def __init__(
        self,
        service: AutomationService,
        *,
        targets: Mapping[str, AutomationTarget],
        worker_id: str | None = None,
        global_concurrency: int = 16,
        claim_batch_size: int = 16,
        lease_duration: timedelta = timedelta(minutes=1),
        poll_interval: timedelta = timedelta(seconds=1),
        drain_timeout: timedelta = timedelta(seconds=30),
        on_interrupt: OnInterrupt | None = None,
        clock: AutomationClock | None = None,
    ) -> None:
        """Create a stopped engine with bounded lifecycle settings.

        Args:
            service: Task and execution service whose Scheduler the engine owns.
            targets: Trusted executable targets keyed by execution-visible name.
            worker_id: Stable diagnostic worker identity for claims.
            global_concurrency: Maximum unfinished executions admitted globally.
            claim_batch_size: Maximum work items considered per dispatch cycle.
            lease_duration: Store ownership duration renewed during active work.
            poll_interval: Maximum delay before checking durable work after restart.
            drain_timeout: Graceful shutdown wait before cancelling owned tasks.
            on_interrupt: Optional classifier that cannot resume the graph.
            clock: Optional controllable clock used by lifecycle tests.

        Raises:
            ValueError: A limit is non-positive or target registration is invalid.
        """

        if not isinstance(service, AutomationService):
            raise TypeError("service must be an AutomationService")
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be at least 1")
        if claim_batch_size < 1:
            raise ValueError("claim_batch_size must be at least 1")
        for name, duration in (
            ("lease_duration", lease_duration),
            ("poll_interval", poll_interval),
            ("drain_timeout", drain_timeout),
        ):
            if duration <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if not targets:
            raise ValueError("targets must not be empty")
        for name, target in targets.items():
            if not name or name != name.strip():
                raise ValueError("target names must be canonical")
            if not hasattr(target, "run") or not hasattr(
                target, "cancellation_is_final"
            ):
                raise TypeError("targets must implement AutomationTarget")
        if on_interrupt is not None and not callable(on_interrupt):
            raise TypeError("on_interrupt must be callable")
        self._service = service
        self._targets = dict(targets)
        self._worker_id = worker_id or f"worker-{uuid4()}"
        self._global_concurrency = global_concurrency
        self._claim_batch_size = claim_batch_size
        self._lease_duration = lease_duration
        self._poll_interval = poll_interval
        self._drain_timeout = drain_timeout
        self._on_interrupt = on_interrupt
        self._clock = clock or SystemClock()
        self._wake = asyncio.Event()
        self._owned: dict[str, _OwnedExecution] = {}
        self._supervisor: asyncio.Task[TaskOutcome[None]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._failure: BaseException | None = None
        self._dispatches = 0
        self._dispatch_idle = asyncio.Event()
        self._dispatch_idle.set()
        self._started = False
        self._ready = False
        self._closing = False
        self._closed = False

    async def start(self) -> None:
        """Prepare storage, then start wakeups and one durable-work supervisor."""

        if self._closed:
            raise AutomationLifecycleError("Automation Engine is closed")
        if self._started:
            raise AutomationLifecycleError("Automation Engine is already started")
        self._started = True
        scheduler_started = False
        engine_bound = False
        try:
            await self._service._setup_store()
            self._ensure_startup_open()
            self._service._bind_engine(self._cancel_running, self._wake.set)
            engine_bound = True
            await self._service._task_scheduler.start(
                self._service._materialize_due_task
            )
            scheduler_started = True
            self._ensure_startup_open()
            await self._service._sync_all_tasks()
            self._ensure_startup_open()
            self._wake.set()
            self._supervisor = asyncio.create_task(
                capture(self._run_supervisor()),
                name="tinkerfin-automation-engine-supervisor",
            )
            self._ready = True
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = error
            if scheduler_started:
                cleanup = asyncio.create_task(
                    capture(self._service._task_scheduler.close()),
                    name="tinkerfin-automation-start-cleanup",
                )
                try:
                    await join_owned_task(cleanup)
                except BaseException as cleanup_error:  # noqa: BLE001 - retain cleanup failure
                    failure = select_failure(failure, cleanup_error)
            if engine_bound:
                self._service._unbind_engine(self._cancel_running)
            self._ready = False
            self._started = False
            raise failure

    async def check_ready(self) -> None:
        """Require a healthy started worker without dispatching or waiting for work.

        Raises:
            AutomationLifecycleError: The worker is stopped, closing, or failed.
        """
        self._ensure_running()

    async def run_ready(self) -> int:
        """Dispatch currently ready work without waiting for the next poll."""

        self._ensure_running()
        await self._reap_finished()
        return await self._dispatch_ready()

    async def wait_until_idle(self) -> None:
        """Wait until all currently ready work has been settled by this engine."""

        self._ensure_running()
        while True:
            self._ensure_running()
            await self._reap_finished()
            await self._dispatch_ready()
            # Another dispatcher may have committed a claim without publishing
            # its owned task yet. Its empty peer cannot establish engine idleness.
            await self._dispatch_idle.wait()
            self._ensure_running()
            if not self._owned:
                return
            # The engine owns execution tasks; cancelling this waiter only detaches it.
            await asyncio.wait(tuple(owned.task for owned in self._owned.values()))

    async def close(self) -> None:
        """Stop admission and settle every owned task before returning or raising.

        Concurrent callers share one shutdown. Caller cancellation waits for cleanup,
        then propagates with any independent shutdown failure. Targets receive the
        configured graceful drain interval before cancellation; their cleanup must
        finish before the engine releases ownership. The borrowed Store stays open.

        Raises:
            AutomationError: Supervision or shutdown failed after cleanup completed.
            asyncio.CancelledError: The caller cancelled its wait for shutdown.
            BaseException: An extension raised a process-control exception.
        """

        if self._close_task is None:
            self._closed = True
            self._ready = False
            self._closing = True
            self._wake.set()
            self._close_task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-automation-engine-close"
            )
        try:
            await join_owned_task(self._close_task)
        except AutomationError:
            raise
        except Exception as error:
            raise AutomationLifecycleError(
                "Automation Engine shutdown failed", cause=error
            ) from error

    async def _close_once(self) -> None:
        failure: BaseException | None = None
        try:
            await self._service._task_scheduler.close()
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = error
        if self._supervisor is not None:
            try:
                await join_owned_task(self._supervisor)
            except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
                failure = error if failure is None else select_failure(failure, error)
        # Explicit dispatch calls admitted before close also own their Store await.
        await self._dispatch_idle.wait()
        tasks = [owned.task for owned in self._owned.values()]
        drain_wait: asyncio.Task[TaskOutcome[None]] | None = None
        try:
            if tasks:
                drain_wait = asyncio.create_task(
                    capture(
                        self._clock.wait_until(self._clock.now() + self._drain_timeout)
                    ),
                    name="tinkerfin-automation-engine-drain",
                )
                pending = set(tasks)
                while pending and not drain_wait.done():
                    done, _ = await asyncio.wait(
                        {*pending, drain_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    pending.difference_update(done)
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = error if failure is None else select_failure(failure, error)
        children: list[asyncio.Task[TaskOutcome[object]]] = list(tasks)
        if drain_wait is not None:
            children.append(drain_wait)
        try:
            await stop_owned_tasks(children)
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = error if failure is None else select_failure(failure, error)
        finally:
            self._owned.clear()
            self._service._unbind_engine(self._cancel_running)
            self._started = False
        if failure is not None:
            raise failure

    async def __aenter__(self) -> AutomationEngine:
        """Start the engine and return it for application lifespan management."""

        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the engine and all tasks it owns."""

        await self.close()

    async def _run_supervisor(self) -> None:
        try:
            while not self._closing and self._failure is None:
                await self._reap_finished()
                await self._dispatch_ready()
                self._wake.clear()
                if self._closing:
                    break
                children: list[asyncio.Task[TaskOutcome[object]]] = []
                failure: BaseException | None = None
                try:
                    children.append(
                        asyncio.create_task(
                            capture_call(
                                partial(
                                    self._clock.wait_until,
                                    self._clock.now() + self._poll_interval,
                                )
                            ),
                            name="tinkerfin-automation-engine-poll",
                        )
                    )
                    children.append(
                        asyncio.create_task(
                            capture(self._wake.wait()),
                            name="tinkerfin-automation-engine-wake",
                        )
                    )
                    await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
                    failure = error
                await self._settle_children(children, failure)
            await self._reap_finished()
            if self._failure is not None:
                raise self._failure
        except BaseException as error:
            self._record_failure(error)
            raise
        finally:
            self._ready = False

    async def _dispatch_ready(self) -> int:
        if self._closing or self._failure is not None:
            return 0
        self._dispatches += 1
        self._dispatch_idle.clear()
        try:
            available = self._global_concurrency - len(self._owned)
            if available <= 0:
                return 0
            claims = await self._service._execution_store.claim_work(
                self._service.namespace,
                self._worker_id,
                limit=min(available, self._claim_batch_size),
                lease_duration=self._lease_duration,
                global_concurrency=self._global_concurrency,
            )
            for claim in claims:
                cancellation = asyncio.Event()
                task = asyncio.create_task(
                    capture(self._process_claim(claim, cancellation)),
                    name=f"tinkerfin-automation-execution-{claim.execution_id}",
                )
                task.add_done_callback(lambda completed: self._wake.set())
                self._owned[claim.execution_id] = _OwnedExecution(
                    task=task, cancel_requested=cancellation
                )
            return len(claims)
        except Exception as error:  # noqa: BLE001 - expose the stable lifecycle failure
            self._record_failure(error)
            assert self._failure is not None
            raise self._failure
        finally:
            self._dispatches -= 1
            if self._dispatches == 0:
                self._dispatch_idle.set()

    async def _reap_finished(self) -> None:
        completed = [
            (execution_id, owned.task)
            for execution_id, owned in self._owned.items()
            if owned.task.done()
        ]
        failure: BaseException | None = None
        for execution_id, task in completed:
            self._owned.pop(execution_id, None)
            try:
                task_result(task)
            except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
                failure = error if failure is None else select_failure(failure, error)
        if failure is not None:
            self._record_failure(failure)
            assert self._failure is not None
            raise self._failure

    def _record_failure(self, error: BaseException) -> None:
        if isinstance(error, Exception) and not isinstance(error, AutomationError):
            error = AutomationLifecycleError("Automation Engine stopped", cause=error)
        self._failure = (
            error if self._failure is None else select_failure(self._failure, error)
        )
        self._ready = False
        self._wake.set()

    def _ensure_running(self) -> None:
        if self._closed:
            raise AutomationLifecycleError("Automation Engine is closed")
        if self._failure is not None:
            raise self._failure
        if not self._ready:
            if self._supervisor is not None and self._supervisor.done():
                task_result(self._supervisor)
            raise AutomationLifecycleError("Automation Engine is not running")

    async def _cancel_running(self, execution_id: str) -> None:
        owned = self._owned.get(execution_id)
        if owned is not None:
            owned.cancel_requested.set()
            self._wake.set()

    def _ensure_startup_open(self) -> None:
        if self._closed:
            raise AutomationLifecycleError("Automation Engine closed during startup")

    async def _process_claim(
        self, claim: WorkItemClaim, cancel_requested: asyncio.Event
    ) -> None:
        if claim.kind is WorkKind.EXPIRE_INTERRUPT:
            await self._service._execution_store.finish_execution(
                claim,
                status=ExecutionStatus.TIMED_OUT,
                failure_code="automation.execution_timeout",
                failure_message="Interrupted execution exceeded its deadline",
            )
            return
        target: AutomationTarget | None = None
        authorization = None
        active_task: asyncio.Task[TaskOutcome[ExecutionOutcome]] | None = None
        current_claim = claim
        try:
            execution = await self._service._execution_store.get_scheduled_execution(
                self._service.namespace, claim.execution_id
            )
            target = self._targets.get(execution.target)
            if target is None:
                await self._service._execution_store.finish_execution(
                    claim,
                    status=ExecutionStatus.FAILED,
                    failure_code=TargetNotFoundError.code.value,
                    failure_message="Execution target is not registered",
                )
                return
            authorization = await self._service._execution_store.authorize_start(
                claim,
                execution_timeout=execution.limits.execution_timeout,
            )
            started = authorization.execution
            assert started.execution_deadline is not None
            active_task = asyncio.create_task(
                capture_call(
                    partial(
                        target.run,
                        ExecutionRequest(
                            execution=started, deadline=started.execution_deadline
                        ),
                    )
                ),
                name=f"tinkerfin-automation-target-{started.execution_id}",
            )
            outcome, current_claim = await self._wait_for_operation(
                active_task,
                claim=current_claim,
                deadline=started.execution_deadline,
                cancel_requested=cancel_requested,
            )
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                await self._service._execution_store.finish_execution(
                    current_claim,
                    status=ExecutionStatus.FAILED,
                    failure_code=(
                        outcome.code.value
                        if isinstance(outcome, TargetExecutionError)
                        else "automation.target_failed"
                    ),
                    failure_message=(
                        outcome.message
                        if isinstance(outcome, TargetExecutionError)
                        else "Execution target failed"
                    ),
                )
                return
            await self._apply_outcome(
                outcome.value,
                claim=current_claim,
                cancel_requested=cancel_requested,
            )
        except _ExecutionDeadline:
            assert active_task is not None
            status = (
                ExecutionStatus.TIMED_OUT
                if target is not None and target.cancellation_is_final
                else ExecutionStatus.NEEDS_ATTENTION
            )
            await self._service._execution_store.finish_execution(
                current_claim,
                status=status,
                failure_code="automation.execution_timeout",
                failure_message=(
                    "Execution exceeded its deadline"
                    if status is ExecutionStatus.TIMED_OUT
                    else "Execution timed out without confirmed external cleanup"
                ),
            )
        except _ExecutionCancellation:
            assert active_task is not None
            status = (
                ExecutionStatus.CANCELLED
                if target is not None and target.cancellation_is_final
                else ExecutionStatus.NEEDS_ATTENTION
            )
            await self._service._execution_store.finish_execution(
                current_claim,
                status=status,
                failure_code=(
                    None
                    if status is ExecutionStatus.CANCELLED
                    else "automation.cancellation_unconfirmed"
                ),
                failure_message=(
                    None
                    if status is ExecutionStatus.CANCELLED
                    else "Cancellation did not confirm external cleanup"
                ),
            )
        except asyncio.CancelledError as error:
            failure: BaseException = error
            if authorization is not None:
                cleanup = asyncio.create_task(
                    capture(
                        self._service._execution_store.finish_execution(
                            current_claim,
                            status=ExecutionStatus.NEEDS_ATTENTION,
                            failure_code="automation.worker_shutdown",
                            failure_message="Worker stopped before execution was settled",
                        )
                    ),
                    name=f"tinkerfin-automation-shutdown-{claim.execution_id}",
                )
                try:
                    await join_owned_task(cleanup)
                except BaseException as cleanup_error:  # noqa: BLE001 - retain cleanup failure
                    failure = select_failure(failure, cleanup_error)
            raise failure

    async def _apply_outcome(
        self,
        outcome: ExecutionOutcome,
        *,
        claim: WorkItemClaim,
        cancel_requested: asyncio.Event,
    ) -> None:
        if isinstance(outcome, ExecutionSucceeded):
            await self._service._execution_store.finish_execution(
                claim, status=ExecutionStatus.SUCCEEDED, result=outcome.result
            )
            return
        if isinstance(outcome, ExecutionFailed):
            await self._service._execution_store.finish_execution(
                claim,
                status=ExecutionStatus.FAILED,
                failure_code=outcome.failure.code,
                failure_message=outcome.failure.message,
            )
            return
        if isinstance(outcome, ExecutionUncertain):
            await self._service._execution_store.finish_execution(
                claim,
                status=ExecutionStatus.NEEDS_ATTENTION,
                failure_code=outcome.failure.code,
                failure_message=outcome.failure.message,
            )
            return
        assert isinstance(outcome, ExecutionInterrupted)
        execution = await self._service._execution_store.get_scheduled_execution(
            self._service.namespace, claim.execution_id
        )
        assert execution.execution_deadline is not None
        callback = self._on_interrupt
        if callback is not None:
            interrupted = InterruptedExecution(
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                identity=execution.identity,
                interrupt_ids=outcome.interrupt_ids,
                interrupted_at=await self._service._execution_store.current_time(),
                execution_deadline=execution.execution_deadline,
            )
            callback_task = asyncio.create_task(
                capture_call(partial(callback, interrupted)),
                name=f"tinkerfin-automation-interrupt-{execution.execution_id}",
            )
            try:
                callback_result, claim = await self._wait_for_operation(
                    callback_task,
                    claim=claim,
                    deadline=execution.execution_deadline,
                    cancel_requested=cancel_requested,
                )
            except _ExecutionDeadline:
                await self._service._execution_store.finish_execution(
                    claim,
                    status=ExecutionStatus.TIMED_OUT,
                    failure_code="automation.execution_timeout",
                    failure_message="Interrupt callback exceeded the execution deadline",
                )
                return
            except _ExecutionCancellation:
                await self._service._execution_store.finish_execution(
                    claim, status=ExecutionStatus.CANCELLED
                )
                return
            if isinstance(callback_result, BaseException):
                if not isinstance(callback_result, Exception):
                    raise callback_result
                await self._service._execution_store.finish_execution(
                    claim,
                    status=ExecutionStatus.FAILED,
                    failure_code=InterruptCallbackError.code.value,
                    failure_message="Interrupt callback failed",
                )
                return
            callback_result = callback_result.value
            if callback_result is not None and not isinstance(
                callback_result, ExecutionFailure
            ):
                await self._service._execution_store.finish_execution(
                    claim,
                    status=ExecutionStatus.FAILED,
                    failure_code=InterruptCallbackError.code.value,
                    failure_message="Interrupt callback returned an invalid result",
                )
                return
            if callback_result is not None:
                await self._service._execution_store.finish_execution(
                    claim,
                    status=ExecutionStatus.FAILED,
                    failure_code=callback_result.code,
                    failure_message=callback_result.message,
                )
                return
        await self._service._execution_store.mark_interrupted(
            claim, interrupt_ids=outcome.interrupt_ids
        )

    async def _wait_for_operation(
        self,
        operation: asyncio.Task[TaskOutcome[_ResultT]],
        *,
        claim: WorkItemClaim,
        deadline: datetime,
        cancel_requested: asyncio.Event,
    ) -> tuple[TaskOutcome[_ResultT], WorkItemClaim]:
        current_claim = claim
        waits: set[asyncio.Task[TaskOutcome[object]]] = set()
        failure: BaseException | None = None
        observed = False
        outcome: TaskOutcome[_ResultT] | None = None
        try:
            deadline_wait = asyncio.create_task(
                capture_call(partial(self._clock.wait_until, deadline)),
                name=f"tinkerfin-automation-deadline-{claim.execution_id}",
            )
            waits.add(deadline_wait)
            cancellation_wait = asyncio.create_task(
                capture(cancel_requested.wait()),
                name=f"tinkerfin-automation-cancel-{claim.execution_id}",
            )
            waits.add(cancellation_wait)
            renewal_wait = asyncio.create_task(
                capture_call(
                    partial(
                        self._clock.wait_until,
                        self._clock.now() + self._lease_duration / 2,
                    )
                ),
                name=f"tinkerfin-automation-renew-{claim.execution_id}",
            )
            waits.add(renewal_wait)
            while True:
                done, _ = await asyncio.wait(
                    {operation, *waits}, return_when=asyncio.FIRST_COMPLETED
                )
                if operation in done:
                    observed = True
                    outcome = operation.result()
                    break
                if cancellation_wait in done:
                    task_result(cancellation_wait)
                    raise _ExecutionCancellation
                if deadline_wait in done:
                    task_result(deadline_wait)
                    raise _ExecutionDeadline
                if renewal_wait in done:
                    task_result(renewal_wait)
                    waits.remove(renewal_wait)
                    current_claim = await self._service._execution_store.renew_claim(
                        current_claim, lease_duration=self._lease_duration
                    )
                    renewal_wait = asyncio.create_task(
                        capture(
                            self._clock.wait_until(
                                self._clock.now() + self._lease_duration / 2
                            )
                        ),
                        name=f"tinkerfin-automation-renew-{claim.execution_id}",
                    )
                    waits.add(renewal_wait)
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = error
        children = list(waits)
        if not observed:
            children.append(operation)
        try:
            await self._settle_children(children, failure)
        except BaseException as error:
            if isinstance(outcome, BaseException):
                raise select_failure(error, outcome)
            raise
        assert outcome is not None
        return outcome, current_claim

    @staticmethod
    async def _settle_children(
        children: list[asyncio.Task[TaskOutcome[object]]],
        failure: BaseException | None,
    ) -> None:
        cleanup = asyncio.create_task(
            capture(stop_owned_tasks(children)),
            name="tinkerfin-automation-child-cleanup",
        )
        try:
            await join_owned_task(cleanup)
        except BaseException as error:  # noqa: BLE001 - re-raise after owned cleanup
            failure = (
                error
                if failure is None
                else select_failure(error, failure)
                if isinstance(failure, (_ExecutionDeadline, _ExecutionCancellation))
                else select_failure(failure, error)
            )
        if failure is not None:
            raise failure


__all__ = ["AutomationEngine", "OnInterrupt"]
