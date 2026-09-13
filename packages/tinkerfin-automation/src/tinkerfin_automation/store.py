"""Shared persistence and execution coordination for Automation stores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import JsonValue

from .models import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionPage,
    ExecutionStatus,
    TaskPage,
    TaskStatus,
)
from .queries import ExecutionFilter, TaskFilter


class WorkKind(StrEnum):
    """Durable action represented by one claimable work item."""

    EXECUTE = "execute"
    EXPIRE_INTERRUPT = "expire_interrupt"


@dataclass(frozen=True, slots=True)
class WorkItemClaim:
    """Fenced ownership of one short-lived Automation work item."""

    work_item_id: str
    namespace: str
    execution_id: str
    kind: WorkKind
    claim_token: str
    fence: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class StartAuthorization:
    """The only permission to invoke a target for one execution."""

    execution: AutomationExecution
    start_token: str


@dataclass(frozen=True, slots=True)
class ScheduledExecution:
    """One due execution and its deterministic schedule occurrence key."""

    execution: AutomationExecution
    occurrence_key: str


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    """Atomic task wakeup advancement and newly persisted executions."""

    task: AutomationTask
    executions: tuple[AutomationExecution, ...]


class AutomationStore(Protocol):
    """Persist task facts and provide fenced execution coordination.

    Page cursors belong to the same ownership scope and filter selection as the
    requested page. A missing, deleted, or excluded anchor returns an empty page.
    """

    async def setup(self) -> None:
        """Prepare the Store for use or reject incompatible persisted state.

        Repeated and concurrent calls must be safe. Implementations that own durable
        schema objects must create them only when none of those objects exist and must
        reject any existing shape outside the single current contract.

        Raises:
            AutomationStoreError: Preparation fails or persisted state is incompatible.
        """

        ...

    async def current_time(self) -> datetime:
        """Return the store authority's timezone-aware UTC time."""

        ...

    async def create_task(
        self,
        task: AutomationTask,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Create one task with optional command idempotency."""

        ...

    async def update_task(
        self,
        task: AutomationTask,
        *,
        expected_revision: int,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationTask:
        """Replace one task after an atomic revision check."""

        ...

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
        """Delete a task definition while retaining its execution history."""

        ...

    async def get_task(
        self, namespace: str, owner_id: str, task_id: str
    ) -> AutomationTask:
        """Read one task inside an explicit ownership scope."""

        ...

    async def list_tasks(
        self,
        namespace: str,
        owner_id: str,
        *,
        limit: int,
        cursor: str | None,
        filters: TaskFilter | None = None,
    ) -> TaskPage:
        """List tasks with a stable keyset cursor."""

        ...

    async def get_scheduled_task(self, namespace: str, task_id: str) -> AutomationTask:
        """Read one task for a trusted worker inside its configured namespace."""

        ...

    async def summarize_tasks(
        self, namespace: str, owner_id: str, *, filters: TaskFilter | None = None
    ) -> dict[TaskStatus, int]:
        """Count all matching tasks in the authorized owner scope."""
        ...

    async def summarize_executions(
        self, namespace: str, owner_id: str, *, filters: ExecutionFilter | None = None
    ) -> dict[ExecutionStatus, int]:
        """Count all matching executions in the authorized owner scope."""
        ...

    async def list_scheduled_tasks(
        self,
        namespace: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> TaskPage:
        """List tasks across owners for a trusted scheduler worker."""

        ...

    async def materialize_task(
        self,
        task: AutomationTask,
        *,
        expected_next_run_at: datetime,
        executions: tuple[ScheduledExecution, ...],
    ) -> MaterializationResult:
        """Persist due executions and advance the task wakeup atomically."""

        ...

    async def enqueue_execution(
        self,
        execution: AutomationExecution,
        *,
        occurrence_key: str,
        request_id: str | None,
        input_digest: str,
        expected_task_revision: int | None = None,
    ) -> AutomationExecution:
        """Atomically create an execution and enforce an optional task revision.

        A supplied revision applies only to new task-backed work. Verify it against
        the task inside the same atomic boundary as queue admission. Return an
        existing command or occurrence result before applying this precondition.

        Args:
            execution: Complete queued execution snapshot selected by the caller.
            occurrence_key: Stable identity of the scheduled or explicit occurrence.
            request_id: Optional command key within the namespace and owner scope.
            input_digest: Canonical command digest used to reject conflicting reuse.
            expected_task_revision: Optional current revision required for new
                task-backed work; not applicable to taskless work or business retries.

        Returns:
            The newly committed execution or the existing idempotent result.

        Raises:
            TaskConflictError: The task revision changed before queue admission.
            RequestConflictError: The command conflicts with an existing result.
            QueueFullError: Queue capacity is exhausted.
        """

        ...

    async def get_execution(
        self, namespace: str, owner_id: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution inside an explicit ownership scope."""

        ...

    async def get_scheduled_execution(
        self, namespace: str, execution_id: str
    ) -> AutomationExecution:
        """Read one execution for a trusted worker inside its namespace."""

        ...

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
        """List execution history with a stable keyset cursor."""

        ...

    async def claim_work(
        self,
        namespace: str,
        worker_id: str,
        *,
        limit: int,
        lease_duration: timedelta,
        global_concurrency: int,
    ) -> tuple[WorkItemClaim, ...]:
        """Claim due work and atomically reserve required concurrency scopes."""

        ...

    async def renew_claim(
        self, claim: WorkItemClaim, *, lease_duration: timedelta
    ) -> WorkItemClaim:
        """Extend a valid claim without changing its fence."""

        ...

    async def authorize_start(
        self,
        claim: WorkItemClaim,
        *,
        execution_timeout: timedelta,
    ) -> StartAuthorization:
        """Persist and return the one-time permission to invoke a target."""

        ...

    async def mark_interrupted(
        self,
        claim: WorkItemClaim,
        *,
        interrupt_ids: tuple[str, ...],
    ) -> AutomationExecution:
        """Record an unfinished graph without approving or resuming it."""

        ...

    async def finish_execution(
        self,
        claim: WorkItemClaim,
        *,
        status: ExecutionStatus,
        result: JsonValue | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> AutomationExecution:
        """Settle a claimed execution and release capacity only when safe."""

        ...

    async def cancel_execution(
        self,
        namespace: str,
        owner_id: str,
        execution_id: str,
        *,
        request_id: str | None,
        input_digest: str,
    ) -> AutomationExecution:
        """Persist cancellation intent or cancel queued work atomically."""

        ...

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

        ...

    async def close(self) -> None:
        """Close owned resources; borrowed resources remain caller-owned."""

        ...


__all__ = [
    "AutomationStore",
    "MaterializationResult",
    "ScheduledExecution",
    "StartAuthorization",
    "WorkItemClaim",
    "WorkKind",
]
