"""Bounded single-process implementation of the Automation Store contract."""

from __future__ import annotations

import asyncio
import secrets
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeVar
from uuid import uuid4

from pydantic import JsonValue

from . import _rules
from ._query_cursor import decode_cursor, encode_cursor, query_scope
from .clock import AutomationClock, SystemClock
from .errors import (
    AutomationStoreError,
    ClaimLostError,
    ExecutionNotFoundError,
    RequestConflictError,
    TaskConflictError,
    TaskNotFoundError,
)
from .models import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionPage,
    ExecutionStatus,
    TaskPage,
    TaskStatus,
    _validate_persisted_text,
)
from .queries import ExecutionFilter, TaskFilter
from .store import (
    MaterializationResult,
    ScheduledExecution,
    StartAuthorization,
    WorkItemClaim,
    WorkKind,
)

_ResultT = TypeVar("_ResultT", bound=AutomationTask | AutomationExecution | str)


def _validate_scope(namespace: object, owner_id: object) -> None:
    """Validate direct Store ownership inputs before lookup or mutation."""

    _validate_persisted_text(namespace, name="namespace", maximum=128)
    _validate_persisted_text(owner_id, name="owner_id", maximum=191)


def _snapshot(value: _ResultT) -> _ResultT:
    # Copy only JSON containers; immutable identity, policy and timestamp values
    # remain shared. Neither an input nor a returned view may mutate saved facts.
    if isinstance(value, AutomationExecution):
        return replace(
            value, input=deepcopy(dict(value.input)), result=deepcopy(value.result)
        )
    if isinstance(value, AutomationTask):
        return replace(value, input=deepcopy(dict(value.input)))
    return value


@dataclass(frozen=True, slots=True)
class MemoryStoreLimits:
    """Bound records retained by one in-memory store instance."""

    max_tasks: int = 10_000
    max_executions: int = 100_000
    max_work_items: int = 100_000
    max_operations: int = 100_000

    def __post_init__(self) -> None:
        """Require positive capacities for every retained record type."""

        for name, value in (
            ("max_tasks", self.max_tasks),
            ("max_executions", self.max_executions),
            ("max_work_items", self.max_work_items),
            ("max_operations", self.max_operations),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")


class _WorkStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"


@dataclass(slots=True)
class _WorkItem:
    work_item_id: str
    namespace: str
    execution_id: str
    kind: WorkKind
    available_at: datetime
    created_at: datetime
    status: _WorkStatus = _WorkStatus.PENDING
    worker_id: str | None = None
    claim_token: str | None = None
    fence: int = 0
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Operation:
    input_digest: str
    result: AutomationTask | AutomationExecution | str


def _matches_tasks(task: AutomationTask, filters: TaskFilter) -> bool:
    return (
        not filters.name_contains
        or filters.name_contains.casefold() in task.name.casefold()
    ) and (not filters.statuses or task.status in filters.statuses)


def _matches_executions(
    execution: AutomationExecution, filters: ExecutionFilter
) -> bool:
    return (
        (
            not filters.name_contains
            or filters.name_contains.casefold()
            in (execution.task_name or "").casefold()
        )
        and (not filters.statuses or execution.status in filters.statuses)
        and (filters.queued_from is None or execution.queued_at >= filters.queued_from)
        and (filters.queued_until is None or execution.queued_at < filters.queued_until)
    )


class MemoryAutomationStore:
    """Persist bounded Automation state in one process.

    All mutations share one short asyncio lock and perform no external I/O while the
    lock is held. The store is suitable for local development and deterministic tests;
    it cannot coordinate multiple processes and loses all facts on process exit.
    """

    def __init__(
        self,
        *,
        clock: AutomationClock | None = None,
        limits: MemoryStoreLimits | None = None,
    ) -> None:
        """Create an open store with explicit record capacity limits."""

        self._clock = clock or SystemClock()
        self._limits = limits or MemoryStoreLimits()
        self._tasks: dict[str, AutomationTask] = {}
        self._executions: dict[str, AutomationExecution] = {}
        self._occurrences: dict[tuple[str, str], str] = {}
        self._work: dict[str, _WorkItem] = {}
        self._work_keys: dict[tuple[str, str, WorkKind], str] = {}
        self._operations: dict[tuple[str, str, str], _Operation] = {}
        self._scope_allocations: dict[tuple[str, str, str], int] = {}
        self._admitted: set[str] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def setup(self) -> None:
        """Confirm that this ready-by-construction Store remains open."""

        self._ensure_open()

    async def current_time(self) -> datetime:
        """Return the in-memory store clock in UTC."""

        self._ensure_open()
        return self._clock.now().astimezone(UTC)

    async def create_task(
        self,
        task: AutomationTask,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Create one task with optional command idempotency."""

        task = _snapshot(task)
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                task.namespace, task.owner_id, request_id, input_digest
            )
            if previous is not None:
                return self._expect_result(previous, AutomationTask)
            if task.task_id in self._tasks:
                raise TaskConflictError(
                    "Task identity already exists",
                    context={"task_id": task.task_id},
                )
            self._ensure_capacity("tasks", len(self._tasks), self._limits.max_tasks)
            self._require_operation_capacity(request_id)
            self._tasks[task.task_id] = task
            self._save_operation(
                task.namespace,
                task.owner_id,
                request_id,
                input_digest,
                task,
            )
            return _snapshot(task)

    async def update_task(
        self,
        task: AutomationTask,
        *,
        expected_revision: int,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Replace one task after an atomic revision check."""

        task = _snapshot(task)
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                task.namespace, task.owner_id, request_id, input_digest
            )
            if previous is not None:
                return self._expect_result(previous, AutomationTask)
            current = self._owned_task(task.namespace, task.owner_id, task.task_id)
            if current.revision != expected_revision:
                raise TaskConflictError(
                    "Task revision changed",
                    context={
                        "task_id": task.task_id,
                        "expected_revision": expected_revision,
                        "actual_revision": current.revision,
                    },
                )
            if task.revision != expected_revision + 1:
                raise TaskConflictError(
                    "Replacement task must advance revision exactly once",
                    context={"task_id": task.task_id},
                )
            self._require_operation_capacity(request_id)
            self._tasks[task.task_id] = task
            self._save_operation(
                task.namespace,
                task.owner_id,
                request_id,
                input_digest,
                task,
            )
            return _snapshot(task)

    async def delete_task(
        self,
        namespace: str,
        owner_id: str,
        task_id: str,
        *,
        expected_revision: int,
        request_id: str | None,
        input_digest: str,
    ) -> None:
        """Delete a task and cancel only executions that have not started."""

        _validate_scope(namespace, owner_id)
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                namespace, owner_id, request_id, input_digest
            )
            if previous is not None:
                self._expect_result(previous, str)
                return
            task = self._owned_task(namespace, owner_id, task_id)
            if task.revision != expected_revision:
                raise TaskConflictError(
                    "Task revision changed",
                    context={
                        "task_id": task_id,
                        "expected_revision": expected_revision,
                        "actual_revision": task.revision,
                    },
                )
            self._require_operation_capacity(request_id)
            del self._tasks[task_id]
            now = self._clock.now()
            for execution in tuple(self._executions.values()):
                if (
                    execution.task_id != task_id
                    or execution.status is not ExecutionStatus.QUEUED
                ):
                    continue
                self._apply_transition(_rules.cancel_execution(execution, now))
            self._save_operation(namespace, owner_id, request_id, input_digest, task_id)

    async def get_task(
        self, namespace: str, owner_id: str, task_id: str
    ) -> AutomationTask:
        """Read one task inside an explicit ownership scope."""

        _validate_scope(namespace, owner_id)
        async with self._lock:
            self._ensure_open()
            return _snapshot(self._owned_task(namespace, owner_id, task_id))

    async def list_tasks(
        self,
        namespace: str,
        owner_id: str,
        *,
        limit: int,
        cursor: str | None,
        filters: TaskFilter | None = None,
    ) -> TaskPage:
        """Read matching tasks using an ownership-bound stable position."""
        _validate_scope(namespace, owner_id)
        self._validate_page_limit(limit)
        selected = filters or TaskFilter()
        scope = query_scope(namespace, owner_id, selected)
        position = decode_cursor(cursor, scope)
        async with self._lock:
            self._ensure_open()
            items = sorted(
                (
                    item
                    for item in self._tasks.values()
                    if item.namespace == namespace
                    and item.owner_id == owner_id
                    and _matches_tasks(item, selected)
                    and (position is None or (item.created_at, item.task_id) > position)
                ),
                key=lambda item: (item.created_at, item.task_id),
                reverse=False,
            )
            return TaskPage(
                items=tuple(_snapshot(item) for item in items[:limit]),
                next_cursor=encode_cursor(
                    scope, items[limit - 1].created_at, items[limit - 1].task_id
                )
                if len(items) > limit
                else None,
            )

    async def summarize_tasks(
        self, namespace: str, owner_id: str, *, filters: TaskFilter | None = None
    ) -> dict[TaskStatus, int]:
        """Count matching tasks across every page in one owner scope."""
        _validate_scope(namespace, owner_id)
        selected = filters or TaskFilter()
        async with self._lock:
            self._ensure_open()
            counts = {status: 0 for status in TaskStatus}
            for item in self._tasks.values():
                if (
                    item.namespace == namespace
                    and item.owner_id == owner_id
                    and _matches_tasks(item, selected)
                ):
                    counts[item.status] += 1
            return counts

    async def summarize_executions(
        self, namespace: str, owner_id: str, *, filters: ExecutionFilter | None = None
    ) -> dict[ExecutionStatus, int]:
        """Count matching executions across every page in one owner scope."""
        _validate_scope(namespace, owner_id)
        selected = filters or ExecutionFilter()
        async with self._lock:
            self._ensure_open()
            counts = {status: 0 for status in ExecutionStatus}
            for item in self._executions.values():
                if (
                    item.namespace == namespace
                    and item.owner_id == owner_id
                    and _matches_executions(item, selected)
                ):
                    counts[item.status] += 1
            return counts

    async def get_scheduled_task(self, namespace: str, task_id: str) -> AutomationTask:
        """Read one task for a trusted worker inside its namespace."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        async with self._lock:
            self._ensure_open()
            task = self._tasks.get(task_id)
            if task is None or task.namespace != namespace:
                raise TaskNotFoundError(
                    "Task was not found", context={"task_id": task_id}
                )
            return _snapshot(task)

    async def list_scheduled_tasks(
        self,
        namespace: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> TaskPage:
        """List enabled tasks across owners for a trusted scheduler worker."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        self._validate_page_limit(limit)
        async with self._lock:
            self._ensure_open()
            items = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.namespace == namespace and task.status is TaskStatus.ENABLED
                ),
                key=lambda task: (task.created_at, task.task_id),
            )
            page = self._page_after(items, cursor, limit, id_name="task_id")
            return TaskPage(
                items=tuple(_snapshot(item) for item in page[:limit]),
                next_cursor=page[limit - 1].task_id if len(page) > limit else None,
            )

    async def materialize_task(
        self,
        task: AutomationTask,
        *,
        expected_next_run_at: datetime,
        executions: tuple[ScheduledExecution, ...],
    ) -> MaterializationResult:
        """Persist due executions and advance one task wakeup atomically."""

        task = _snapshot(task)
        executions = tuple(
            replace(item, execution=_snapshot(item.execution)) for item in executions
        )
        async with self._lock:
            self._ensure_open()
            current = self._owned_task(task.namespace, task.owner_id, task.task_id)
            if not _rules.materialization_is_current(
                current, task, expected_next_run_at
            ):
                return MaterializationResult(task=_snapshot(current), executions=())
            pending = _rules.pending_occurrences(
                executions,
                {
                    key
                    for namespace, key in self._occurrences
                    if namespace == task.namespace
                },
            )
            queued = sum(
                execution.task_id == task.task_id
                and execution.status is ExecutionStatus.QUEUED
                for execution in self._executions.values()
            )
            _rules.require_queue_capacity(
                queued=queued,
                additional=len(pending),
                maximum=task.limits.max_queued_runs,
                scope_id=task.task_id,
            )
            if len(self._executions) + len(pending) > self._limits.max_executions:
                self._ensure_capacity(
                    "executions",
                    self._limits.max_executions,
                    self._limits.max_executions,
                )
            if len(self._work) + len(pending) > self._limits.max_work_items:
                self._ensure_capacity(
                    "work_items",
                    self._limits.max_work_items,
                    self._limits.max_work_items,
                )
            self._tasks[task.task_id] = task
            created: list[AutomationExecution] = []
            for item in pending:
                execution = item.execution
                self._executions[execution.execution_id] = execution
                self._occurrences[(task.namespace, item.occurrence_key)] = (
                    execution.execution_id
                )
                self._create_work(
                    execution,
                    kind=WorkKind.EXECUTE,
                    available_at=execution.queued_at,
                )
                created.append(execution)
            return MaterializationResult(
                task=_snapshot(task),
                executions=tuple(_snapshot(item) for item in created),
            )

    async def enqueue_execution(
        self,
        execution: AutomationExecution,
        *,
        occurrence_key: str,
        request_id: str | None,
        input_digest: str,
        expected_task_revision: int | None = None,
    ) -> AutomationExecution:
        """Create one queued execution and its executable work item atomically."""

        execution = _snapshot(execution)
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                execution.namespace,
                execution.owner_id,
                request_id,
                input_digest,
            )
            if previous is not None:
                return self._expect_result(previous, AutomationExecution)
            occurrence = (execution.namespace, occurrence_key)
            existing_id = self._occurrences.get(occurrence)
            if existing_id is not None:
                return _snapshot(self._executions[existing_id])
            if execution.status is not ExecutionStatus.QUEUED:
                raise ValueError("new execution status must be queued")
            if execution.execution_id in self._executions:
                raise RequestConflictError(
                    "Execution identity already exists",
                    context={"execution_id": execution.execution_id},
                )
            if execution.retry_of is None and execution.task_id is not None:
                task = self._owned_task(
                    execution.namespace, execution.owner_id, execution.task_id
                )
                _rules.require_task_revision(task, expected_task_revision)
            elif expected_task_revision is not None:
                raise ValueError("expected_task_revision requires new task-backed work")
            queued = sum(
                _rules.same_execution_scope(item, execution)
                and item.status is ExecutionStatus.QUEUED
                for item in self._executions.values()
            )
            _rules.require_queue_capacity(
                queued=queued,
                additional=1,
                maximum=execution.limits.max_queued_runs,
                scope_id=execution.task_id or execution.owner_id,
            )
            self._ensure_capacity(
                "executions",
                len(self._executions),
                self._limits.max_executions,
            )
            self._require_operation_capacity(request_id)
            self._ensure_capacity(
                "work_items", len(self._work), self._limits.max_work_items
            )
            self._executions[execution.execution_id] = execution
            self._occurrences[occurrence] = execution.execution_id
            self._create_work(
                execution,
                kind=WorkKind.EXECUTE,
                available_at=execution.queued_at,
            )
            self._save_operation(
                execution.namespace,
                execution.owner_id,
                request_id,
                input_digest,
                execution,
            )
            return _snapshot(execution)

    async def get_execution(
        self, namespace: str, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution inside an explicit ownership scope."""

        _validate_scope(namespace, owner_id)
        async with self._lock:
            self._ensure_open()
            return _snapshot(self._owned_execution(namespace, owner_id, execution_id))

    async def get_scheduled_execution(
        self, namespace: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution for a trusted worker inside its namespace."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        async with self._lock:
            self._ensure_open()
            execution = self._executions.get(execution_id)
            if execution is None or execution.namespace != namespace:
                raise ExecutionNotFoundError(
                    "Execution was not found", context={"execution_id": execution_id}
                )
            return _snapshot(execution)

    async def list_executions(
        self,
        namespace: str,
        owner_id: str,
        *,
        task_id: str | None,
        limit: int,
        cursor: str | None,
        filters: ExecutionFilter | None = None,
    ) -> ExecutionPage:
        """Read matching executions using an ownership-bound stable position."""
        _validate_scope(namespace, owner_id)
        self._validate_page_limit(limit)
        selected = filters or ExecutionFilter()
        scope = query_scope(namespace, owner_id, selected, task_id)
        position = decode_cursor(cursor, scope)
        async with self._lock:
            self._ensure_open()
            items = sorted(
                (
                    item
                    for item in self._executions.values()
                    if item.namespace == namespace
                    and item.owner_id == owner_id
                    and _matches_executions(item, selected)
                    and (task_id is None or item.task_id == task_id)
                    and (
                        position is None
                        or (item.queued_at, item.execution_id) < position
                    )
                ),
                key=lambda item: (item.queued_at, item.execution_id),
                reverse=True,
            )
            return ExecutionPage(
                items=tuple(_snapshot(item) for item in items[:limit]),
                next_cursor=encode_cursor(
                    scope, items[limit - 1].queued_at, items[limit - 1].execution_id
                )
                if len(items) > limit
                else None,
            )

    async def claim_work(
        self,
        namespace: str,
        worker_id: str,
        *,
        limit: int,
        lease_duration: timedelta,
        global_concurrency: int,
    ) -> tuple[WorkItemClaim, ...]:
        """Claim due work and reserve global and task capacity atomically."""

        _validate_persisted_text(namespace, name="namespace", maximum=128)
        _validate_persisted_text(worker_id, name="worker_id", maximum=191)
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be at least 1")
        async with self._lock:
            self._ensure_open()
            now = self._clock.now()
            self._recover_expired_claims(namespace, now)
            candidates = sorted(
                (
                    item
                    for item in self._work.values()
                    if item.namespace == namespace
                    and item.status is _WorkStatus.PENDING
                    and item.available_at <= now
                ),
                key=lambda item: (
                    item.available_at,
                    item.created_at,
                    item.work_item_id,
                ),
            )
            claims: list[WorkItemClaim] = []
            for item in candidates:
                if len(claims) >= limit:
                    break
                execution = self._executions[item.execution_id]
                transition = _rules.claim_candidate(execution, item.kind, now)
                if transition is not None:
                    self._apply_transition(transition, item)
                    continue
                if item.kind is WorkKind.EXECUTE and not self._reserve_scopes(
                    execution, global_concurrency
                ):
                    continue
                claim_token = secrets.token_hex(16)
                item.status = _WorkStatus.CLAIMED
                item.worker_id = worker_id
                item.claim_token = claim_token
                item.fence += 1
                item.lease_until = now + lease_duration
                claims.append(self._claim_from(item))
            return tuple(claims)

    async def renew_claim(
        self, claim: WorkItemClaim, *, lease_duration: timedelta
    ) -> WorkItemClaim:
        """Extend a valid claim without changing its fence."""

        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        async with self._lock:
            item = self._valid_claim(claim)
            item.lease_until = self._clock.now() + lease_duration
            return self._claim_from(item)

    async def authorize_start(
        self,
        claim: WorkItemClaim,
        *,
        execution_timeout: timedelta,
    ) -> StartAuthorization:
        """Persist and return the only target start authorization."""

        if execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive")
        async with self._lock:
            item = self._valid_claim(claim)
            execution = self._executions[item.execution_id]
            authorization = _rules.authorize_start(
                execution,
                kind=item.kind,
                admitted=execution.execution_id in self._admitted,
                now=self._clock.now(),
                execution_timeout=execution_timeout,
                start_token=secrets.token_hex(24),
            )
            self._executions[execution.execution_id] = authorization.execution
            return replace(authorization, execution=_snapshot(authorization.execution))

    async def mark_interrupted(
        self,
        claim: WorkItemClaim,
        *,
        interrupt_ids: tuple[str, ...],
    ) -> AutomationExecution:
        """Record an unfinished graph and schedule only its deadline check."""

        if not interrupt_ids:
            raise ValueError("interrupt_ids must not be empty")
        async with self._lock:
            item = self._valid_claim(claim)
            execution = self._executions[item.execution_id]
            transition = _rules.interrupt_execution(
                execution, interrupt_ids=interrupt_ids, now=self._clock.now()
            )
            return _snapshot(self._apply_transition(transition, item))

    async def finish_execution(
        self,
        claim: WorkItemClaim,
        *,
        status: ExecutionStatus,
        result: JsonValue | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> AutomationExecution:
        """Settle claimed work and retain capacity only for uncertain execution."""

        if not status.is_terminal and status is not ExecutionStatus.NEEDS_ATTENTION:
            raise ValueError("finish status must be terminal or needs_attention")
        result = deepcopy(result)
        async with self._lock:
            item = self._valid_claim(claim)
            execution = self._executions[item.execution_id]
            transition = _rules.finish_execution(
                execution,
                status=status,
                result=result,
                failure_code=failure_code,
                failure_message=failure_message,
                now=self._clock.now(),
            )
            return _snapshot(self._apply_transition(transition, item))

    async def cancel_execution(
        self,
        namespace: str,
        owner_id: str,
        execution_id: str,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationExecution:
        """Cancel queued/interrupted work or persist running cancellation intent."""

        _validate_scope(namespace, owner_id)
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                namespace, owner_id, request_id, input_digest
            )
            if previous is not None:
                return self._expect_result(previous, AutomationExecution)
            execution = self._owned_execution(namespace, owner_id, execution_id)
            transition = _rules.cancel_execution(execution, self._clock.now())
            self._require_operation_capacity(request_id)
            updated = self._apply_transition(transition)
            self._save_operation(namespace, owner_id, request_id, input_digest, updated)
            return _snapshot(updated)

    async def resolve_execution(
        self,
        namespace: str,
        owner_id: str,
        execution_id: str,
        *,
        resolution: AttentionResolution,
        request_id: str,
        input_digest: str,
        reason: str,
    ) -> AutomationExecution:
        """Apply one audited terminal resolution to uncertain external work."""

        _validate_scope(namespace, owner_id)
        _validate_persisted_text(reason, name="reason")
        async with self._lock:
            self._ensure_open()
            previous = self._operation_result(
                namespace, owner_id, request_id, input_digest
            )
            if previous is not None:
                return self._expect_result(previous, AutomationExecution)
            execution = self._owned_execution(namespace, owner_id, execution_id)
            transition = _rules.resolve_execution(
                execution, resolution=resolution, reason=reason, now=self._clock.now()
            )
            self._require_operation_capacity(request_id)
            updated = self._apply_transition(transition)
            self._save_operation(namespace, owner_id, request_id, input_digest, updated)
            return _snapshot(updated)

    async def close(self) -> None:
        """Close the in-memory store; repeated close calls are harmless."""

        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise AutomationStoreError("Memory Automation Store is closed")

    def _owned_task(
        self, namespace: str, owner_id: str, task_id: str
    ) -> AutomationTask:
        task = self._tasks.get(task_id)
        if task is None or task.namespace != namespace or task.owner_id != owner_id:
            raise TaskNotFoundError(
                "Task was not found",
                context={"task_id": task_id},
            )
        return task

    def _owned_execution(
        self, namespace: str, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        execution = self._executions.get(execution_id)
        if (
            execution is None
            or execution.namespace != namespace
            or execution.owner_id != owner_id
        ):
            raise ExecutionNotFoundError(
                "Execution was not found",
                context={"execution_id": execution_id},
            )
        return execution

    def _operation_result(
        self,
        namespace: str,
        owner_id: str,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask | AutomationExecution | str | None:
        if request_id is None:
            return None
        operation = self._operations.get((namespace, owner_id, request_id))
        if operation is None:
            return None
        if operation.input_digest != input_digest:
            raise RequestConflictError(
                "Request ID was reused with different input",
                context={"request_id": request_id},
            )
        return operation.result

    def _require_operation_capacity(self, request_id: str | None) -> None:
        # A command's recorded result and its effects must become visible together.
        if request_id is not None:
            self._ensure_capacity(
                "operations", len(self._operations), self._limits.max_operations
            )

    def _save_operation(
        self,
        namespace: str,
        owner_id: str,
        request_id: str | None,
        input_digest: str,
        result: AutomationTask | AutomationExecution | str,
    ) -> None:
        if request_id is None:
            return
        self._ensure_capacity(
            "operations", len(self._operations), self._limits.max_operations
        )
        self._operations[(namespace, owner_id, request_id)] = _Operation(
            input_digest=input_digest,
            result=_snapshot(result),
        )

    @staticmethod
    def _expect_result(
        result: AutomationTask | AutomationExecution | str, expected: type[_ResultT]
    ) -> _ResultT:
        if not isinstance(result, expected):
            raise AutomationStoreError("Stored operation result has an invalid type")
        return _snapshot(result)

    def _create_work(
        self,
        execution: AutomationExecution,
        *,
        kind: WorkKind,
        available_at: datetime,
    ) -> None:
        key = (execution.namespace, execution.execution_id, kind)
        existing_id = self._work_keys.get(key)
        if existing_id is not None:
            existing = self._work[existing_id]
            existing.available_at = available_at
            existing.status = _WorkStatus.PENDING
            existing.worker_id = None
            existing.claim_token = None
            existing.lease_until = None
            return
        self._ensure_capacity(
            "work_items", len(self._work), self._limits.max_work_items
        )
        work_item_id = str(uuid4())
        item = _WorkItem(
            work_item_id=work_item_id,
            namespace=execution.namespace,
            execution_id=execution.execution_id,
            kind=kind,
            available_at=available_at,
            created_at=self._clock.now(),
        )
        self._work[work_item_id] = item
        self._work_keys[key] = work_item_id

    def _apply_transition(
        self, transition: _rules.ExecutionTransition, item: _WorkItem | None = None
    ) -> AutomationExecution:
        execution = transition.execution
        if transition.deadline_work_at is not None:
            # Check capacity before changing the execution or completing its claim.
            key = (
                execution.namespace,
                execution.execution_id,
                WorkKind.EXPIRE_INTERRUPT,
            )
            if key not in self._work_keys:
                self._ensure_capacity(
                    "work_items", len(self._work), self._limits.max_work_items
                )
        self._executions[execution.execution_id] = execution
        if transition.complete_work == "all":
            self._complete_execution_work(execution.execution_id)
        elif transition.complete_work == "current":
            assert item is not None
            item.status = _WorkStatus.COMPLETED
            item.worker_id = None
            item.claim_token = None
            item.lease_until = None
        if transition.release_capacity:
            self._release_scopes(execution)
        if transition.deadline_work_at is not None:
            self._create_work(
                execution,
                kind=WorkKind.EXPIRE_INTERRUPT,
                available_at=transition.deadline_work_at,
            )
        return execution

    def _recover_expired_claims(self, namespace: str, now: datetime) -> None:
        for item in self._work.values():
            if (
                item.namespace == namespace
                and item.status is _WorkStatus.CLAIMED
                and item.lease_until is not None
                and item.lease_until <= now
            ):
                execution = self._executions[item.execution_id]
                transition = _rules.recover_expired_claim(execution, item.kind, now)
                self._apply_transition(transition, item)
                if transition.complete_work == "none":
                    item.status = _WorkStatus.PENDING
                item.worker_id = None
                item.claim_token = None
                item.lease_until = None

    def _reserve_scopes(
        self, execution: AutomationExecution, global_concurrency: int
    ) -> bool:
        if execution.execution_id in self._admitted:
            return True
        global_key = (execution.namespace, "global", "global")
        scope_kind, scope_key = _rules.execution_scope(execution)
        execution_key = (execution.namespace, scope_kind, scope_key)
        if self._scope_allocations.get(global_key, 0) >= global_concurrency:
            return False
        if (
            self._scope_allocations.get(execution_key, 0)
            >= execution.limits.max_concurrent_runs
        ):
            return False
        self._scope_allocations[global_key] = (
            self._scope_allocations.get(global_key, 0) + 1
        )
        self._scope_allocations[execution_key] = (
            self._scope_allocations.get(execution_key, 0) + 1
        )
        self._admitted.add(execution.execution_id)
        return True

    def _release_scopes(self, execution: AutomationExecution) -> None:
        if execution.execution_id not in self._admitted:
            return
        scope_kind, scope_key = _rules.execution_scope(execution)
        for key in (
            (execution.namespace, "global", "global"),
            (execution.namespace, scope_kind, scope_key),
        ):
            allocated = self._scope_allocations.get(key, 0)
            if allocated <= 1:
                self._scope_allocations.pop(key, None)
            else:
                self._scope_allocations[key] = allocated - 1
        self._admitted.remove(execution.execution_id)

    def _valid_claim(self, claim: WorkItemClaim) -> _WorkItem:
        self._ensure_open()
        item = self._work.get(claim.work_item_id)
        now = self._clock.now()
        if (
            item is None
            or item.namespace != claim.namespace
            or item.execution_id != claim.execution_id
            or item.status is not _WorkStatus.CLAIMED
            or item.claim_token != claim.claim_token
            or item.fence != claim.fence
            or item.lease_until is None
            or item.lease_until <= now
        ):
            raise ClaimLostError(
                "Work item claim is no longer valid",
                context={"work_item_id": claim.work_item_id},
            )
        return item

    @staticmethod
    def _claim_from(item: _WorkItem) -> WorkItemClaim:
        assert item.claim_token is not None
        assert item.lease_until is not None
        return WorkItemClaim(
            work_item_id=item.work_item_id,
            namespace=item.namespace,
            execution_id=item.execution_id,
            kind=item.kind,
            claim_token=item.claim_token,
            fence=item.fence,
            lease_until=item.lease_until,
        )

    def _complete_execution_work(self, execution_id: str) -> None:
        for item in self._work.values():
            if item.execution_id == execution_id:
                item.status = _WorkStatus.COMPLETED
                item.worker_id = None
                item.claim_token = None
                item.lease_until = None

    @staticmethod
    def _validate_page_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _page_after(
        items: list[_ResultT],
        cursor: str | None,
        limit: int,
        *,
        id_name: str,
    ) -> list[_ResultT]:
        if cursor is None:
            return items[: limit + 1]
        for index, item in enumerate(items):
            if getattr(item, id_name) == cursor:
                return items[index + 1 : index + limit + 2]
        return []

    @staticmethod
    def _ensure_capacity(kind: str, current: int, maximum: int) -> None:
        if current >= maximum:
            raise AutomationStoreError(
                "Memory Automation Store capacity is exhausted",
                context={"record_kind": kind, "maximum": maximum},
            )


__all__ = ["MemoryAutomationStore", "MemoryStoreLimits"]
