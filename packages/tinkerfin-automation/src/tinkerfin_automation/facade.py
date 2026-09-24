"""Register trusted work and own its task-management lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Literal, TypeGuard, TypeVar, overload

from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .clock import AutomationClock, SystemClock
from .engine import AutomationEngine, OnInterrupt
from .errors import AutomationLifecycleError
from .models import ExecutionFailure, InterruptedExecution
from .scheduler import AutomationScheduler, _require_external_close
from .schedules import ScheduleSpec
from .service import AutomationService
from .store import AutomationStore
from .targets import (
    AutomationTarget,
    ExecutionOutcome,
    ExecutionRequest,
    FunctionTarget,
    TargetCallable,
)

if TYPE_CHECKING:
    from .owner import AutomationOwner

_TargetT = TypeVar("_TargetT", bound=AutomationTarget | TargetCallable)


class _Activity:
    def __init__(self, automation: Automation) -> None:
        self.automation = automation
        self.active = True


_activities: ContextVar[tuple[_Activity, ...]] = ContextVar(
    "automation_activities", default=()
)


def _is_target(value: object) -> TypeGuard[AutomationTarget]:
    return callable(getattr(value, "run", None)) and isinstance(
        getattr(value, "cancellation_is_final", None), bool
    )


class _ManagedTarget:
    """Prevent a target from joining the lifecycle that is waiting for it."""

    def __init__(self, automation: Automation, target: AutomationTarget) -> None:
        self._automation = automation
        self._target = target

    @property
    def cancellation_is_final(self) -> bool:
        return self._target.cancellation_is_final

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        with self._automation._activity():
            return await self._target.run(request)


class Automation:
    """Manage trusted targets and owner-bound tasks in one explicit lifecycle.

    Use ``async with automation.worker()`` to manage and execute schedules, or
    ``async with automation`` to query and submit immediate work to a remote worker.
    An instance can be entered once. Supplied Stores and their database engines
    remain borrowed; a supplied Scheduler transfers its lifecycle to Automation.
    Host authentication and target authorization must precede owner operations.
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        store: AutomationStore | None = None,
        scheduler: AutomationScheduler | None = None,
        clock: AutomationClock | None = None,
    ) -> None:
        """Configure in-memory defaults without opening resources or starting work."""
        self._clock = clock or SystemClock()
        self._service = AutomationService(
            namespace=namespace, store=store, scheduler=scheduler, clock=self._clock
        )
        self._targets: dict[str, AutomationTarget] = {}
        self._state: Literal[
            "new", "starting", "client", "worker", "closing", "closed"
        ] = "new"
        self._engine: AutomationEngine | None = None
        self._start_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._closing = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._operations = 0

    @overload
    def target(self, name: str) -> Callable[[_TargetT], _TargetT]: ...

    @overload
    def target(self, name: str, target: _TargetT) -> _TargetT: ...

    def target(
        self,
        name: str,
        target: _TargetT | None = None,
    ) -> _TargetT | Callable[[_TargetT], _TargetT]:
        """Register a target or decorate an async callable, returning the original.

        Args:
            name: Canonical target name persisted in task definitions.
            target: A target or callable returning an awaitable; omit to decorate.

        Returns:
            The unchanged target, or a decorator returning the unchanged callable.

        Raises:
            AutomationLifecycleError: Registration is frozen after entry starts.
            ValueError: The name is invalid or already registered.
            TypeError: The target is neither a callable nor an AutomationTarget.
        """
        self._require_new()
        self._service._validate_identifier(name, name="target", maximum=191)

        def register(value: _TargetT) -> _TargetT:
            self._require_new()
            if name in self._targets:
                raise ValueError(f"Target is already registered: {name}")
            if _is_target(value):
                executable = value
            elif callable(value):
                executable = FunctionTarget(value)
            else:
                raise TypeError(
                    "target must implement AutomationTarget or return an awaitable"
                )
            self._targets[name] = executable
            return value

        return register if target is None else register(target)

    def for_owner(
        self, owner_id: str, *, execution_namespace: str | None = None
    ) -> AutomationOwner:
        """Bind an authenticated owner and the Runtime scope for new work.

        Args:
            owner_id: Host-authorized identity for task and execution access.
            execution_namespace: Runtime namespace for newly created tasks and
                taskless executions; None selects this Automation's namespace.
                Existing tasks and retries retain their saved Runtime scope.

        Returns:
            A resource-free owner view borrowing this Automation lifecycle.

        Raises:
            AutomationLifecycleError: The lifecycle cannot accept owner bindings.
            ValueError: An identity or namespace is invalid.
        """
        from .owner import AutomationOwner

        if self._state not in {"new", "client", "worker"}:
            raise AutomationLifecycleError(
                "Bind owners before entry or during an active lifecycle"
            )
        return AutomationOwner._bind(self, owner_id, execution_namespace)

    def worker(
        self,
        *,
        worker_id: str | None = None,
        global_concurrency: int = 16,
        claim_batch_size: int = 16,
        lease_duration: timedelta = timedelta(minutes=1),
        poll_interval: timedelta = timedelta(seconds=1),
        drain_timeout: timedelta = timedelta(seconds=30),
        on_interrupt: OnInterrupt | None = None,
    ) -> AbstractAsyncContextManager[AutomationEngine]:
        """Select managed execution with the Engine's bounded worker settings.

        Args:
            worker_id: Optional diagnostic claim owner.
            global_concurrency: Maximum unfinished executions across workers.
            claim_batch_size: Maximum work items considered per dispatch.
            lease_duration: Ownership interval renewed while executing.
            poll_interval: Maximum interval between durable work checks.
            drain_timeout: Grace period before cancelling remaining owned work.
            on_interrupt: Optional host classifier; cannot approve or resume a graph.

        Returns:
            A single-use context returning the active AutomationEngine.

        Raises:
            AutomationLifecycleError: This instance has already entered a lifecycle.
        """
        self._require_new()

        @asynccontextmanager
        async def lifespan() -> AsyncIterator[AutomationEngine]:
            self._require_new()
            self._state = "starting"
            try:
                classifier: OnInterrupt | None = None
                if on_interrupt is not None:
                    classify_interrupt: OnInterrupt = on_interrupt

                    async def classify(
                        execution: InterruptedExecution,
                    ) -> ExecutionFailure | None:
                        with self._activity():
                            return await classify_interrupt(execution)

                    classifier = classify
                self._engine = AutomationEngine(
                    self._service,
                    targets={
                        name: _ManagedTarget(self, value)
                        for name, value in self._targets.items()
                    },
                    worker_id=worker_id,
                    global_concurrency=global_concurrency,
                    claim_batch_size=claim_batch_size,
                    lease_duration=lease_duration,
                    poll_interval=poll_interval,
                    drain_timeout=drain_timeout,
                    on_interrupt=classifier,
                    clock=self._clock,
                )
                await self._enter()
                yield self._engine
            except BaseException as error:
                await self._exit(error)
                raise
            else:
                await self._exit(None)

        return lifespan()

    async def __aenter__(self) -> Automation:
        """Enter query/submission mode without starting a local worker."""
        self._require_new()
        self._state = "starting"
        try:
            await self._enter()
        except BaseException as error:
            await self._exit(error)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Settle owned resources while retaining a body exception's causal chain."""
        await self._exit(exc)

    async def preview_schedule(
        self, schedule: ScheduleSpec, *, after: datetime | None = None, count: int = 5
    ) -> tuple[datetime, ...]:
        """Preview occurrences, using Store time when an explicit cursor is absent."""
        async with self._operation():
            return await self._service.preview_schedule(
                schedule, after=after, count=count
            )

    async def aclose(self) -> None:
        """Stop accepting operations and settle owned resources exactly once.

        Accepted operations finish before resource closure. Waiters stop observing
        when closing starts. Concurrent close calls share the outcome; cancellation
        propagates only after cleanup settles. Do not close from a running target,
        resource callback, or accepted operation: that would wait for itself.

        Raises:
            AutomationLifecycleError: Shutdown would wait for its own callback.
            BaseException: Cleanup failed or the caller cancelled its wait.
        """
        if any(item.active and item.automation is self for item in _activities.get()):
            raise AutomationLifecycleError(
                "Close Automation after its active callback returns"
            )
        _require_external_close(self._service._task_scheduler)
        if self._close_task is None:
            self._state = "closing"
            self._closing.set()
            self._close_task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-automation-close"
            )
        await join_owned_task(self._close_task)

    def _require_new(self) -> None:
        if self._state != "new":
            raise AutomationLifecycleError(
                "Automation can only be configured and entered once"
            )

    @contextmanager
    def _activity(self) -> Iterator[None]:
        activity = _Activity(self)
        token = _activities.set(
            (*[item for item in _activities.get() if item.active], activity)
        )
        try:
            yield
        finally:
            activity.active = False
            _activities.reset(token)

    @asynccontextmanager
    async def _operation(self, *, schedule_write: bool = False) -> AsyncIterator[None]:
        if self._state not in {"client", "worker"}:
            raise AutomationLifecycleError(
                "Enter an Automation lifecycle before operating"
            )
        if schedule_write and self._state != "worker":
            raise AutomationLifecycleError(
                "Manage schedules inside automation.worker()"
            )
        self._operations += 1
        self._idle.clear()
        try:
            with self._activity():
                yield
        finally:
            self._operations -= 1
            if not self._operations:
                self._idle.set()

    async def _enter(self) -> None:
        self._start_task = asyncio.create_task(
            capture(self._start_once()), name="tinkerfin-automation-start"
        )
        await join_owned_task(self._start_task)
        if self._state not in {"client", "worker"}:
            raise AutomationLifecycleError("Automation closed during startup")

    async def _start_once(self) -> None:
        with self._activity():
            await self._service.__aenter__()
            if self._state != "starting":
                return
            if self._engine is not None:
                await self._engine.start()
            if self._state == "starting":
                self._state = "worker" if self._engine else "client"

    async def _exit(self, primary: BaseException | None) -> None:
        try:
            await self.aclose()
        except BaseException as error:  # noqa: BLE001 - retain both failures without masking cancellation
            raise error if primary is None else select_failure(primary, error)

    async def _close_once(self) -> None:
        failure: BaseException | None = None
        try:
            if self._start_task is not None:
                # Startup's caller owns its failure; cleanup still closes resources
                # acquired before that failure and cannot race further acquisition.
                await self._start_task
            await self._idle.wait()
            with self._activity():
                for close in ([self._engine.close] if self._engine else []) + [
                    self._service.close
                ]:
                    try:
                        await close()
                    except BaseException as error:  # noqa: BLE001 - attempt every close
                        failure = (
                            error if failure is None else select_failure(failure, error)
                        )
        finally:
            self._state = "closed"
        if failure is not None:
            raise failure


__all__ = ["Automation"]
