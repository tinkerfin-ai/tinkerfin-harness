"""Register trusted work and own its task-management lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Literal, TypeGuard, TypeVar, overload

from tinkerfin_contracts.identity import validate_namespace

from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .clock import AutomationClock, SystemClock
from .engine import AutomationEngine, OnInterrupt
from .errors import AutomationLifecycleError, TargetNamespaceError, TargetNotFoundError
from .models import ExecutionFailure, InterruptedExecution
from .runtime_target import TinkerFinTarget
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

    def __init__(
        self, automation: Automation, name: str, target: AutomationTarget
    ) -> None:
        self._automation = automation
        self._name = name
        self._target = target

    @property
    def cancellation_is_final(self) -> bool:
        return self._target.cancellation_is_final

    async def run(self, request: ExecutionRequest) -> ExecutionOutcome:
        with self._automation._activity():
            expected = self._automation._execution_namespace(
                self._name, request.execution.owner_id
            )
            if request.execution.identity.namespace != expected:
                raise TargetNamespaceError(
                    "Target does not match the saved execution namespace",
                    context={"target": self._name},
                )
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
        self._target_namespaces: dict[str, str | Callable[[str], str]] = {}
        self._remote_targets: set[str] = set()
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
    def target(
        self,
        name: str,
        *,
        execution_namespace: str | Callable[[str], str] | None = None,
    ) -> Callable[[_TargetT], _TargetT]: ...

    @overload
    def target(
        self,
        name: str,
        target: _TargetT,
        *,
        execution_namespace: str | Callable[[str], str] | None = None,
    ) -> _TargetT: ...

    def target(
        self,
        name: str,
        target: _TargetT | None = None,
        *,
        execution_namespace: str | Callable[[str], str] | None = None,
    ) -> _TargetT | Callable[[_TargetT], _TargetT]:
        """Register a target or decorate an async callable, returning the original.

        Args:
            name: Canonical target name persisted in task definitions.
            target: A target or callable returning an awaitable; omit to decorate.
            execution_namespace: Fixed space or a deterministic, side-effect-free
                owner-to-space mapping that performs no I/O. Ordinary targets
                default to the scheduling namespace. TinkerFinTarget already owns
                this choice and rejects an additional declaration.

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
            registered = value
            self._require_new()
            if name in self._target_namespaces:
                raise ValueError(f"Target is already registered: {name}")
            if _is_target(value):
                executable = value
            elif callable(value):
                executable = FunctionTarget(value)
            else:
                raise TypeError(
                    "target must implement AutomationTarget or return an awaitable"
                )
            if isinstance(value, TinkerFinTarget):
                if execution_namespace is not None:
                    raise ValueError("TinkerFinTarget already selects its namespace")
                policy = value.execution_namespace
            else:
                policy = (
                    self._service.namespace
                    if execution_namespace is None
                    else execution_namespace
                )
            self._target_namespaces[name] = self._namespace_policy(policy)
            self._targets[name] = executable
            return registered

        return register if target is None else register(target)

    def remote_target(
        self, name: str, *, execution_namespace: str | Callable[[str], str]
    ) -> None:
        """Declare a remote target's execution space without loading its Runtime.

        Use this only with the client lifecycle. The host must keep its declaration
        consistent with the remote worker; this method does not discover or route
        workers. Queries and operations on saved executions need no declaration.

        Args:
            name: Target name accepted by the remote worker.
            execution_namespace: Fixed space or deterministic owner-to-space mapping,
                without I/O or side effects.

        Raises:
            AutomationLifecycleError: Configuration is already frozen.
            ValueError: The name or space is invalid or the target already exists.
            TypeError: The space is neither a string nor a synchronous callable.
        """
        self._require_new()
        self._service._validate_identifier(name, name="target", maximum=191)
        if name in self._target_namespaces:
            raise ValueError(f"Target is already registered: {name}")
        self._target_namespaces[name] = self._namespace_policy(execution_namespace)
        self._remote_targets.add(name)

    @staticmethod
    def _namespace_policy(
        policy: str | Callable[[str], str],
    ) -> str | Callable[[str], str]:
        if isinstance(policy, str):
            return validate_namespace(policy)
        if (
            not callable(policy)
            or inspect.iscoroutinefunction(policy)
            or inspect.iscoroutinefunction(getattr(policy, "__call__", None))
        ):
            raise TypeError(
                "execution_namespace must be text or a synchronous callable"
            )
        return policy

    def _execution_namespace(self, target: str, owner_id: str) -> str:
        policy = self._target_namespaces.get(target)
        if policy is None:
            raise TargetNotFoundError(
                "Target has not been declared", context={"target": target}
            )
        try:
            return validate_namespace(
                policy if isinstance(policy, str) else policy(owner_id)
            )
        except Exception as error:
            raise TargetNamespaceError(
                "Target could not select a valid execution namespace",
                context={"target": target},
                cause=error,
            ) from error

    def for_owner(self, owner_id: str) -> AutomationOwner:
        """Bind an authenticated owner to task and execution operations.

        Args:
            owner_id: Host-authorized identity for task and execution access.

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
        return AutomationOwner._bind(self, owner_id)

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
            poll_interval: Maximum interval between durable work checks when the
                Store has no Notifications service.
            drain_timeout: Grace period before cancelling remaining owned work.
            on_interrupt: Optional host classifier; cannot approve or resume a graph.

        Returns:
            A single-use context returning the active AutomationEngine.

        Raises:
            AutomationLifecycleError: This instance has already entered a lifecycle.
        """
        self._require_worker_configuration()

        @asynccontextmanager
        async def lifespan() -> AsyncGenerator[AutomationEngine, None]:
            # The returned context may be entered after further configuration.
            # Validate the complete target set immediately before freezing it.
            self._require_worker_configuration()
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
                        name: _ManagedTarget(self, name, value)
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

    def _require_worker_configuration(self) -> None:
        self._require_new()
        if self._remote_targets:
            raise AutomationLifecycleError(
                "Remote target declarations require the client lifecycle"
            )

    @contextmanager
    def _activity(self) -> Generator[None, None, None]:
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
    async def _operation(
        self, *, schedule_write: bool = False
    ) -> AsyncGenerator[None, None]:
        if self._state not in {"client", "worker"}:
            raise AutomationLifecycleError(
                "Enter an Automation lifecycle before operating"
            )
        if (
            schedule_write
            and self._state != "worker"
            and self._service._execution_store.notifications is None
        ):
            raise AutomationLifecycleError(
                "Schedule management requires a worker or a Store with Notifications"
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
