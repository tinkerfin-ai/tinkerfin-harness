"""Task-oriented public service for Automation definitions and execution history."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TypeAlias
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from tinkerfin_contracts.identity import validate_namespace

from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .clock import AutomationClock, SystemClock
from .errors import (
    AutomationError,
    AutomationLifecycleError,
    InvalidScheduleError,
    QueueFullError,
    RetryNotAllowedError,
)
from .identity import canonical_digest, execution_identity, occurrence_key
from .memory import MemoryAutomationStore
from .models import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionOrigin,
    ExecutionPage,
    ExecutionStatus,
    JsonObject,
    TaskPage,
    TaskStatus,
)
from .policies import ExecutionLimits, MisfirePolicy
from .queries import ExecutionFilter, TaskFilter
from .scheduler import AutomationScheduler, MemoryScheduler, _require_external_close
from .schedules import (
    ScheduleSpec,
    materialize_schedule,
    next_run_after,
    preview_schedule,
)
from .store import AutomationStore, ScheduledExecution

_CancelRunning: TypeAlias = Callable[[str], Awaitable[None]]
_WakeWorker: TypeAlias = Callable[[], None]
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class AutomationService:
    """Manage scheduled tasks and immediate executions through one business API.

    The default constructor owns an in-memory Store and Scheduler. A supplied Store
    remains caller-owned; a supplied Scheduler transfers its lifecycle to this service
    because the service must synchronize task changes and stop its wakeups on close.
    Store setup completes automatically before each public operation can touch state;
    engine startup also completes it before Scheduler startup. Host authorization must
    run before passing ``owner_id`` to this boundary.
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        store: AutomationStore | None = None,
        scheduler: AutomationScheduler | None = None,
        clock: AutomationClock | None = None,
    ) -> None:
        """Create a service with in-memory defaults and an explicit namespace."""

        self._validate_identifier(namespace, name="namespace", maximum=128)
        selected_clock = clock or SystemClock()
        self._namespace = namespace
        self._store = store or MemoryAutomationStore(clock=selected_clock)
        self._owns_store = store is None
        self._scheduler = scheduler or MemoryScheduler(clock=selected_clock)
        self._cancel_running: _CancelRunning | None = None
        self._wake_worker: _WakeWorker | None = None
        self._closed = False
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None

    @property
    def namespace(self) -> str:
        """Return the scheduling Store partition managed by this service."""

        return self._namespace

    async def create_task(
        self,
        *,
        owner_id: str,
        name: str,
        schedule: ScheduleSpec,
        target: str,
        input: Mapping[str, JsonValue] | None = None,
        misfire_policy: MisfirePolicy | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
        execution_namespace: str | None = None,
    ) -> AutomationTask:
        """Create an enabled task and schedule its first future wakeup.

        Args:
            owner_id: Trusted host identity that owns the task.
            name: User-visible task name.
            schedule: Validated one-time, interval, or Cron schedule.
            target: Name of a target registered by the trusted host.
            input: JSON input to persist for each execution; omit credentials and secrets.
            misfire_policy: Optional missed-wakeup behavior.
            limits: Optional queue and execution limits.
            request_id: Optional command idempotency key.
            execution_namespace: Host-selected Runtime scope for every occurrence;
                None selects this service's scheduling namespace.

        Returns:
            The persisted task definition.

        Raises:
            InvalidScheduleError: The schedule has no supported future run.
            RequestConflictError: A request ID was reused with different input.
        """

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_name(name)
        self._validate_identifier(target, name="target", maximum=191)
        self._validate_request_id(request_id)
        normalized_input = self._json_input(input)
        selected_namespace = validate_namespace(
            self._namespace if execution_namespace is None else execution_namespace
        )
        now = await self._store.current_time()
        next_run_at = next_run_after(schedule, now)
        if next_run_at is None:
            raise InvalidScheduleError("Task schedule has no future run")
        task = AutomationTask(
            task_id=str(uuid4()),
            namespace=self._namespace,
            owner_id=owner_id,
            execution_namespace=selected_namespace,
            name=name,
            target=target,
            input=normalized_input,
            schedule=schedule,
            status=TaskStatus.ENABLED,
            revision=1,
            next_run_at=next_run_at,
            misfire_policy=misfire_policy or MisfirePolicy(),
            limits=limits or ExecutionLimits(),
            created_at=now,
            updated_at=now,
        )
        selected_misfire = task.misfire_policy
        selected_limits = task.limits
        digest = canonical_digest(
            {
                "operation": "create_task",
                "execution_namespace": selected_namespace,
                "owner_id": owner_id,
                "name": name,
                "target": target,
                "input": normalized_input,
                "schedule": schedule,
                "misfire_policy": self._misfire_json(selected_misfire),
                "limits": self._limits_json(selected_limits),
            }
        )
        created = await self._store.create_task(
            task, request_id=request_id, input_digest=digest
        )
        await self._sync_task(created)
        return created

    async def get_task(self, *, owner_id: str, task_id: str) -> AutomationTask:
        """Return one task after applying the explicit ownership scope."""

        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.get_task(self._namespace, owner_id, task_id)

    async def list_tasks(
        self,
        *,
        owner_id: str,
        limit: int = 50,
        cursor: str | None = None,
        filters: TaskFilter | None = None,
    ) -> TaskPage:
        """List matching tasks, newest first, within the trusted owner scope.

        Args:
            owner_id: Identity supplied by host authentication.
            limit: Maximum page size, from 1 to 100.
            cursor: Previous next_cursor, unchanged and for the same query.
            filters: Literal name and status selection; None selects all tasks.

        Returns:
            A live page and an optional opaque continuation cursor. Deleting
            the preceding row does not invalidate the cursor.

        Raises:
            ValueError: The limit or cursor does not match the query contract.
        """

        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.list_tasks(
            self._namespace, owner_id, limit=limit, cursor=cursor, filters=filters
        )

    async def summarize_tasks(
        self, *, owner_id: str, filters: TaskFilter | None = None
    ) -> dict[TaskStatus, int]:
        """Count all matching tasks by status, independently of pagination.

        Args:
            owner_id: Identity supplied by host authentication.
            filters: The same selection accepted by list_tasks.

        Returns:
            Counts for every TaskStatus, including zero counts.
        """
        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.summarize_tasks(
            self._namespace, owner_id, filters=filters
        )

    async def summarize_executions(
        self, *, owner_id: str, filters: ExecutionFilter | None = None
    ) -> dict[ExecutionStatus, int]:
        """Count all matching executions by status, independently of pagination.

        Args:
            owner_id: Identity supplied by host authentication.
            filters: The same selection accepted by list_executions.

        Returns:
            Counts for every ExecutionStatus, including zero counts.
        """
        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.summarize_executions(
            self._namespace, owner_id, filters=filters
        )

    async def update_task(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_revision: int,
        name: str | None = None,
        schedule: ScheduleSpec | None = None,
        target: str | None = None,
        input: Mapping[str, JsonValue] | None = None,
        misfire_policy: MisfirePolicy | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
    ) -> AutomationTask:
        """Update selected task fields after an optimistic revision check."""

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_request_id(request_id)
        current = await self.get_task(owner_id=owner_id, task_id=task_id)
        next_name = current.name if name is None else name
        next_target = current.target if target is None else target
        next_input = current.input if input is None else self._json_input(input)
        next_schedule = current.schedule if schedule is None else schedule
        self._validate_name(next_name)
        self._validate_identifier(next_target, name="target", maximum=191)
        now = await self._store.current_time()
        if current.status is TaskStatus.ENABLED and schedule is not None:
            next_run_at = next_run_after(next_schedule, now)
            if next_run_at is None:
                raise InvalidScheduleError("Task schedule has no future run")
        else:
            next_run_at = current.next_run_at
        updated = replace(
            current,
            name=next_name,
            target=next_target,
            input=next_input,
            schedule=next_schedule,
            revision=expected_revision + 1,
            next_run_at=next_run_at,
            misfire_policy=misfire_policy or current.misfire_policy,
            limits=limits or current.limits,
            updated_at=now,
        )
        digest = canonical_digest(
            {
                "operation": "update_task",
                "task_id": task_id,
                "expected_revision": expected_revision,
                "name": next_name,
                "target": next_target,
                "input": next_input,
                "schedule": next_schedule,
                "misfire_policy": self._misfire_json(updated.misfire_policy),
                "limits": self._limits_json(updated.limits),
            }
        )
        stored = await self._store.update_task(
            updated,
            expected_revision=expected_revision,
            request_id=request_id,
            input_digest=digest,
        )
        await self._sync_task(stored)
        return stored

    async def pause_task(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_revision: int,
        request_id: str | None = None,
    ) -> AutomationTask:
        """Pause future schedule wakeups without cancelling existing executions."""

        await self._setup_store()
        self._validate_request_id(request_id)
        current = await self.get_task(owner_id=owner_id, task_id=task_id)
        now = await self._store.current_time()
        updated = replace(
            current,
            status=TaskStatus.PAUSED,
            revision=expected_revision + 1,
            updated_at=now,
        )
        digest = canonical_digest(
            {
                "operation": "pause_task",
                "task_id": task_id,
                "expected_revision": expected_revision,
            }
        )
        stored = await self._store.update_task(
            updated,
            expected_revision=expected_revision,
            request_id=request_id,
            input_digest=digest,
        )
        await self._sync_task(stored)
        return stored

    async def enable_task(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_revision: int,
        request_id: str | None = None,
    ) -> AutomationTask:
        """Enable future runs from the current store time without paused catch-up."""

        await self._setup_store()
        self._validate_request_id(request_id)
        current = await self.get_task(owner_id=owner_id, task_id=task_id)
        now = await self._store.current_time()
        next_run_at = next_run_after(current.schedule, now)
        if next_run_at is None:
            raise InvalidScheduleError("Task schedule has no future run")
        updated = replace(
            current,
            status=TaskStatus.ENABLED,
            revision=expected_revision + 1,
            next_run_at=next_run_at,
            updated_at=now,
        )
        digest = canonical_digest(
            {
                "operation": "enable_task",
                "task_id": task_id,
                "expected_revision": expected_revision,
            }
        )
        stored = await self._store.update_task(
            updated,
            expected_revision=expected_revision,
            request_id=request_id,
            input_digest=digest,
        )
        await self._sync_task(stored)
        return stored

    async def delete_task(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_revision: int,
        request_id: str | None = None,
    ) -> None:
        """Delete a task definition while retaining its execution history."""

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_request_id(request_id)
        digest = canonical_digest(
            {
                "operation": "delete_task",
                "task_id": task_id,
                "expected_revision": expected_revision,
            }
        )
        await self._store.delete_task(
            self._namespace,
            owner_id,
            task_id,
            expected_revision=expected_revision,
            request_id=request_id,
            input_digest=digest,
        )
        await self._scheduler.remove_task(task_id)

    async def preview_schedule(
        self,
        schedule: ScheduleSpec,
        *,
        after: datetime | None = None,
        count: int = 5,
    ) -> tuple[datetime, ...]:
        """Preview future UTC run times without saving a task."""

        await self._setup_store()
        cursor = await self._store.current_time() if after is None else after
        return preview_schedule(schedule, after=cursor, count=count)

    async def execute_once(
        self,
        *,
        owner_id: str,
        target: str,
        input: Mapping[str, JsonValue] | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
        execution_namespace: str | None = None,
    ) -> AutomationExecution:
        """Queue one immediate execution without creating a task definition.

        Taskless executions share the selected concurrency and queue limits within
        their owner scope. Global Engine concurrency always applies.

        Args:
            owner_id: Trusted host identity that owns the execution.
            target: Name of a target registered by the trusted host.
            input: JSON input to persist for the target; omit credentials and secrets.
            limits: Optional owner-scoped queue and execution limits.
            request_id: Optional command idempotency key.
            execution_namespace: Host-selected Runtime scope for this attempt;
                None selects this service's scheduling namespace.

        Returns:
            The persisted queued execution without a task identity.

        Raises:
            ValueError: Identity, target, input, or request ID is invalid.
            QueueFullError: The owner's taskless execution queue is full.
            RequestConflictError: A request ID was reused with different input.
        """

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_identifier(target, name="target", maximum=191)
        self._validate_request_id(request_id)
        normalized_input = self._json_input(input)
        selected_limits = limits or ExecutionLimits()
        selected_namespace = validate_namespace(
            self._namespace if execution_namespace is None else execution_namespace
        )
        now = await self._store.current_time()
        request_part = request_id or str(uuid4())
        occurrence = occurrence_key(
            [self._namespace, owner_id, "one_time", request_part]
        )
        execution_id, identity = execution_identity(
            namespace=self._namespace,
            execution_namespace=selected_namespace,
            owner_id=owner_id,
            task_id=None,
            occurrence=occurrence,
            attempt=1,
        )
        execution = AutomationExecution(
            execution_id=execution_id,
            task_id=None,
            namespace=self._namespace,
            owner_id=owner_id,
            identity=identity,
            target=target,
            input=normalized_input,
            limits=selected_limits,
            origin=ExecutionOrigin.ONE_TIME,
            status=ExecutionStatus.QUEUED,
            attempt=1,
            retry_of=None,
            scheduled_for=None,
            queued_at=now,
            queue_deadline=now + selected_limits.queue_timeout,
            execution_started_at=None,
            execution_deadline=None,
            finished_at=None,
            failure_code=None,
            failure_message=None,
            result=None,
            interrupt_ids=(),
            start_authorized_at=None,
            start_token=None,
            created_at=now,
            updated_at=now,
        )
        digest = canonical_digest(
            {
                "operation": "execute_once",
                "execution_namespace": selected_namespace,
                "target": target,
                "input": normalized_input,
                "limits": self._limits_json(selected_limits),
            }
        )
        queued = await self._store.enqueue_execution(
            execution,
            occurrence_key=occurrence,
            request_id=request_id,
            input_digest=digest,
        )
        self._notify_work_available()
        return queued

    async def run_task_now(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> AutomationExecution:
        """Queue an additional execution without changing the task schedule.

        Args:
            owner_id: Trusted host identity that owns the task.
            task_id: Existing task to execute immediately.
            expected_revision: Optional task revision that must still be current
                when new work is enqueued. An existing idempotent result is returned
                even if the task has since changed.
            request_id: Optional command idempotency key.

        Returns:
            The persisted queued execution linked to the existing task.

        Raises:
            ValueError: Identity or request ID is invalid.
            TaskNotFoundError: The task does not exist in the ownership scope.
            TaskConflictError: New work was requested for a task revision that changed.
            QueueFullError: The task execution queue is full.
            RequestConflictError: A request ID was reused with different input.
        """

        await self._setup_store()
        self._validate_request_id(request_id)
        task = await self.get_task(owner_id=owner_id, task_id=task_id)
        now = await self._store.current_time()
        request_part = request_id or str(uuid4())
        occurrence = occurrence_key(
            [self._namespace, owner_id, task_id, "task_now", request_part]
        )
        execution = self._new_execution(
            task,
            occurrence=occurrence,
            origin=ExecutionOrigin.MANUAL,
            attempt=1,
            retry_of=None,
            scheduled_for=None,
            now=now,
        )
        digest = canonical_digest({"operation": "run_task_now", "task_id": task_id})
        queued = await self._store.enqueue_execution(
            execution,
            occurrence_key=occurrence,
            request_id=request_id,
            input_digest=digest,
            expected_task_revision=expected_revision,
        )
        self._notify_work_available()
        return queued

    async def get_execution(
        self, *, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        """Return one execution after applying the ownership scope."""

        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.get_execution(self._namespace, owner_id, execution_id)

    async def list_executions(
        self,
        *,
        owner_id: str,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        filters: ExecutionFilter | None = None,
    ) -> ExecutionPage:
        """List matching execution snapshots, newest first.

        Args:
            owner_id: Identity supplied by host authentication.
            task_id: Restrict results to this saved task, including deleted tasks.
            limit: Maximum page size, from 1 to 100.
            cursor: Previous next_cursor for this owner, task and filter selection.
            filters: Captured name, status and aware queue-time bounds.

        Returns:
            A live page and an optional opaque continuation cursor.

        Raises:
            ValueError: The limit or cursor does not match the query contract.
        """

        await self._setup_store()
        self._validate_owner(owner_id)
        return await self._store.list_executions(
            self._namespace,
            owner_id,
            task_id=task_id,
            limit=limit,
            cursor=cursor,
            filters=filters,
        )

    async def cancel_execution(
        self,
        *,
        owner_id: str,
        execution_id: str,
        request_id: str | None = None,
    ) -> AutomationExecution:
        """Cancel queued work or request cancellation of a running execution."""

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_request_id(request_id)
        digest = canonical_digest(
            {"operation": "cancel_execution", "execution_id": execution_id}
        )
        execution = await self._store.cancel_execution(
            self._namespace,
            owner_id,
            execution_id,
            request_id=request_id,
            input_digest=digest,
        )
        if (
            execution.status is ExecutionStatus.CANCEL_REQUESTED
            and self._cancel_running is not None
        ):
            await self._cancel_running(execution.execution_id)
        return execution

    async def retry_execution(
        self,
        *,
        owner_id: str,
        execution_id: str,
        request_id: str | None = None,
    ) -> AutomationExecution:
        """Queue one explicit business retry with a new Runtime identity."""

        await self._setup_store()
        self._validate_request_id(request_id)
        original = await self.get_execution(
            owner_id=owner_id, execution_id=execution_id
        )
        if original.status not in {
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
        }:
            raise RetryNotAllowedError(
                "Execution is not in a retryable terminal state",
                context={"execution_id": execution_id},
            )
        now = await self._store.current_time()
        request_part = request_id or str(uuid4())
        occurrence = occurrence_key(
            [self._namespace, owner_id, execution_id, "retry", request_part]
        )
        retry_id, identity = execution_identity(
            namespace=self._namespace,
            execution_namespace=original.identity.namespace,
            owner_id=owner_id,
            task_id=original.task_id,
            occurrence=occurrence,
            attempt=original.attempt + 1,
        )
        retry = AutomationExecution(
            execution_id=retry_id,
            task_name=original.task_name,
            task_id=original.task_id,
            namespace=self._namespace,
            owner_id=owner_id,
            identity=identity,
            target=original.target,
            input=original.input,
            limits=original.limits,
            origin=ExecutionOrigin.RETRY,
            status=ExecutionStatus.QUEUED,
            attempt=original.attempt + 1,
            retry_of=original.execution_id,
            scheduled_for=None,
            queued_at=now,
            queue_deadline=now + original.limits.queue_timeout,
            execution_started_at=None,
            execution_deadline=None,
            finished_at=None,
            failure_code=None,
            failure_message=None,
            result=None,
            interrupt_ids=(),
            start_authorized_at=None,
            start_token=None,
            created_at=now,
            updated_at=now,
        )
        digest = canonical_digest(
            {
                "operation": "retry_execution",
                "execution_id": execution_id,
                "request": request_part,
            }
        )
        queued = await self._store.enqueue_execution(
            retry,
            occurrence_key=occurrence,
            request_id=request_id,
            input_digest=digest,
        )
        self._notify_work_available()
        return queued

    async def resolve_execution(
        self,
        *,
        owner_id: str,
        execution_id: str,
        resolution: AttentionResolution,
        reason: str,
        request_id: str,
    ) -> AutomationExecution:
        """Apply an audited host-confirmed result to uncertain external work."""

        await self._setup_store()
        self._validate_owner(owner_id)
        self._validate_request_id(request_id)
        digest = canonical_digest(
            {
                "operation": "resolve_execution",
                "execution_id": execution_id,
                "resolution": resolution.value,
                "reason": reason,
            }
        )
        resolved = await self._store.resolve_execution(
            self._namespace,
            owner_id,
            execution_id,
            resolution=resolution,
            request_id=request_id,
            input_digest=digest,
            reason=reason,
        )
        self._notify_work_available()
        return resolved

    async def close(self) -> None:
        """Settle scheduling and the default Store, retaining all shutdown failures.

        Concurrent callers join the same shutdown. Cancelling a waiter does not
        abandon owned resources; cancellation propagates after they settle. A
        failed shutdown remains observable on later close calls. A supplied Store
        remains caller-owned.

        Raises:
            AutomationLifecycleError: Shutdown is requested from a scheduler
                callback or from a resource currently being closed by this Service.
        """

        _require_external_close(self._scheduler)
        if asyncio.current_task() is self._close_task:
            raise AutomationLifecycleError("Service shutdown cannot wait for itself")
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-automation-service-close"
            )
        await join_owned_task(self._close_task)

    async def _close_once(self) -> None:
        failure: BaseException | None = None
        resources = [self._scheduler.close]
        if self._owns_store:
            resources.append(self._store.close)
        for close in resources:
            try:
                await close()
            except BaseException as error:  # noqa: BLE001 - every owned resource still needs settlement
                failure = error if failure is None else select_failure(failure, error)
        if failure is not None:
            if isinstance(failure, Exception) and not isinstance(
                failure, AutomationError
            ):
                raise AutomationLifecycleError(
                    "Automation Service shutdown failed", cause=failure
                ) from failure
            raise failure

    async def __aenter__(self) -> AutomationService:
        """Prepare storage and return this service for lifespan management."""

        await self._setup_store()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close owned resources when leaving the application lifespan."""

        await self.close()

    async def _materialize_due_task(self, task_id: str) -> None:
        task = await self._store.get_scheduled_task(self._namespace, task_id)
        if task.status is not TaskStatus.ENABLED or task.next_run_at is None:
            await self._scheduler.remove_task(task_id)
            return
        now = await self._store.current_time()
        materialized = materialize_schedule(
            task.schedule,
            next_run_at=task.next_run_at,
            now=now,
            policy=task.misfire_policy,
        )
        updated_task = replace(task, next_run_at=materialized.next_run_at)
        executions = tuple(
            ScheduledExecution(
                execution=self._new_execution(
                    task,
                    occurrence=occurrence_key(
                        [
                            self._namespace,
                            task.owner_id,
                            task.task_id,
                            str(task.revision),
                            scheduled_for.isoformat(),
                        ]
                    ),
                    origin=ExecutionOrigin.SCHEDULED,
                    attempt=1,
                    retry_of=None,
                    scheduled_for=scheduled_for,
                    now=now,
                ),
                occurrence_key=occurrence_key(
                    [
                        self._namespace,
                        task.owner_id,
                        task.task_id,
                        str(task.revision),
                        scheduled_for.isoformat(),
                    ]
                ),
            )
            for scheduled_for in materialized.due_at
        )
        try:
            result = await self._store.materialize_task(
                updated_task,
                expected_next_run_at=task.next_run_at,
                executions=executions,
            )
        except QueueFullError:
            await self._scheduler.schedule_task(task_id, now + timedelta(minutes=1))
            return
        if result.executions:
            self._notify_work_available()
        await self._sync_task(result.task)

    async def _sync_all_tasks(self) -> None:
        cursor: str | None = None
        while True:
            page = await self._store.list_scheduled_tasks(
                self._namespace, limit=100, cursor=cursor
            )
            for task in page.items:
                await self._sync_task(task)
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def _setup_store(self) -> None:
        """Prepare storage before any service or engine operation can observe it."""

        self._ensure_open()
        await self._store.setup()
        self._ensure_open()

    def _bind_engine(
        self, cancel_running: _CancelRunning, wake_worker: _WakeWorker
    ) -> None:
        if self._cancel_running is not None:
            raise RuntimeError("Automation Service is already bound to an engine")
        self._cancel_running = cancel_running
        self._wake_worker = wake_worker

    def _unbind_engine(self, cancel_running: _CancelRunning) -> None:
        if self._cancel_running == cancel_running:
            self._cancel_running = None
            self._wake_worker = None

    def _notify_work_available(self) -> None:
        if self._wake_worker is not None:
            self._wake_worker()

    async def _sync_task(self, task: AutomationTask) -> None:
        if task.status is TaskStatus.ENABLED and task.next_run_at is not None:
            await self._scheduler.schedule_task(task.task_id, task.next_run_at)
        else:
            await self._scheduler.remove_task(task.task_id)

    @property
    def _execution_store(self) -> AutomationStore:
        return self._store

    @property
    def _task_scheduler(self) -> AutomationScheduler:
        return self._scheduler

    @staticmethod
    def _new_execution(
        task: AutomationTask,
        *,
        occurrence: str,
        origin: ExecutionOrigin,
        attempt: int,
        retry_of: str | None,
        scheduled_for: datetime | None,
        now: datetime,
    ) -> AutomationExecution:
        execution_id, identity = execution_identity(
            namespace=task.namespace,
            execution_namespace=task.execution_namespace,
            owner_id=task.owner_id,
            task_id=task.task_id,
            occurrence=occurrence,
            attempt=attempt,
        )
        return AutomationExecution(
            execution_id=execution_id,
            task_id=task.task_id,
            task_name=task.name,
            namespace=task.namespace,
            owner_id=task.owner_id,
            identity=identity,
            target=task.target,
            input=task.input,
            limits=task.limits,
            origin=origin,
            status=ExecutionStatus.QUEUED,
            attempt=attempt,
            retry_of=retry_of,
            scheduled_for=scheduled_for,
            queued_at=now,
            queue_deadline=now + task.limits.queue_timeout,
            execution_started_at=None,
            execution_deadline=None,
            finished_at=None,
            failure_code=None,
            failure_message=None,
            result=None,
            interrupt_ids=(),
            start_authorized_at=None,
            start_token=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _json_input(value: Mapping[str, JsonValue] | None) -> JsonObject:
        validated = _JSON_OBJECT.validate_python({} if value is None else dict(value))
        return json.loads(
            json.dumps(
                validated,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        )

    @staticmethod
    def _misfire_json(policy: MisfirePolicy) -> dict[str, JsonValue]:
        return {
            "mode": policy.mode.value,
            "grace_seconds": policy.grace.total_seconds(),
            "catch_up_window_seconds": policy.catch_up_window.total_seconds(),
            "max_catch_up": policy.max_catch_up,
        }

    @staticmethod
    def _limits_json(limits: ExecutionLimits) -> dict[str, JsonValue]:
        return {
            "max_concurrent_runs": limits.max_concurrent_runs,
            "max_queued_runs": limits.max_queued_runs,
            "execution_timeout_seconds": limits.execution_timeout.total_seconds(),
            "queue_timeout_seconds": limits.queue_timeout.total_seconds(),
        }

    @staticmethod
    def _validate_identifier(value: object, *, name: str, maximum: int) -> None:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if "\x00" in value:
            raise ValueError(f"{name} must not contain NUL bytes")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"{name} must be valid UTF-8 text") from error
        if not value or value != value.strip() or len(value) > maximum:
            raise ValueError(f"{name} must be a non-empty canonical value")

    @classmethod
    def _validate_owner(cls, owner_id: object) -> None:
        cls._validate_identifier(owner_id, name="owner_id", maximum=191)

    @staticmethod
    def _validate_name(name: object) -> None:
        AutomationService._validate_identifier(name, name="name", maximum=255)

    @staticmethod
    def _validate_request_id(request_id: object | None) -> None:
        if request_id is not None:
            AutomationService._validate_identifier(
                request_id, name="request_id", maximum=128
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise AutomationLifecycleError("Automation Service is closed")


__all__ = ["AutomationService"]
