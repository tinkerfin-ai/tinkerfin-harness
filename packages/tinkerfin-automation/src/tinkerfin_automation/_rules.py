"""Decide Automation state changes from one Store's atomic facts and time.

These rules perform no I/O and own no locks, clocks, tokens, or capacity records.
The Store applies each returned change inside the same boundary that read its
facts. In particular, uncertain external work keeps its reserved capacity until
an explicit resolution; a recovered claim never authorizes a second start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from pydantic import JsonValue

from .errors import (
    ClaimLostError,
    QueueFullError,
    ResolutionNotAllowedError,
    StartAlreadyAuthorizedError,
    TaskConflictError,
)
from .models import (
    AttentionResolution,
    AutomationExecution,
    AutomationTask,
    ExecutionStatus,
    TaskStatus,
)
from .store import ScheduledExecution, StartAuthorization, WorkKind


@dataclass(frozen=True, slots=True)
class ExecutionTransition:
    """An execution snapshot and work/capacity changes committed with it."""

    execution: AutomationExecution
    complete_work: Literal["none", "current", "all"] = "none"
    release_capacity: bool = False
    deadline_work_at: datetime | None = None


def require_task_revision(task: AutomationTask, expected_revision: int | None) -> None:
    """Protect a task snapshot selected by the caller before changing durable work."""

    if expected_revision is None:
        return
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise TypeError("expected_revision must be an integer")
    if expected_revision < 1:
        raise ValueError("expected_revision must be positive")
    if task.revision != expected_revision:
        raise TaskConflictError(
            "Task revision changed",
            context={
                "task_id": task.task_id,
                "expected_revision": expected_revision,
                "actual_revision": task.revision,
            },
        )


def execution_scope(execution: AutomationExecution) -> tuple[str, str]:
    """Return the task or taskless-owner scope within an execution's namespace."""

    return (
        ("owner", execution.owner_id)
        if execution.task_id is None
        else ("task", execution.task_id)
    )


def same_execution_scope(
    candidate: AutomationExecution, execution: AutomationExecution
) -> bool:
    """Compare namespace and the task or taskless-owner capacity scope."""

    return candidate.namespace == execution.namespace and execution_scope(
        candidate
    ) == execution_scope(execution)


def materialization_is_current(
    current: AutomationTask, proposed: AutomationTask, expected_next_run_at: datetime
) -> bool:
    """Whether a scheduler still owns the task occurrence it observed."""

    return (
        current.next_run_at == expected_next_run_at
        and current.revision == proposed.revision
        and current.status is TaskStatus.ENABLED
    )


def pending_occurrences(
    executions: tuple[ScheduledExecution, ...], existing: set[str]
) -> tuple[ScheduledExecution, ...]:
    """Keep the first snapshot for each not-yet-persisted occurrence, in order."""

    seen = set(existing)
    pending: list[ScheduledExecution] = []
    for item in executions:
        if item.occurrence_key not in seen:
            pending.append(item)
            seen.add(item.occurrence_key)
    return tuple(pending)


def require_queue_capacity(
    *, queued: int, additional: int, maximum: int, scope_id: str
) -> None:
    """Reject new occurrences that would exceed their shared queue bound."""

    if queued + additional > maximum:
        raise QueueFullError("Execution queue is full", context={"scope_id": scope_id})


def claim_candidate(
    execution: AutomationExecution, kind: WorkKind, now: datetime
) -> ExecutionTransition | None:
    """Return a non-claimable work change, or None when admission may proceed."""

    if kind is WorkKind.EXECUTE:
        if execution.status is not ExecutionStatus.QUEUED:
            return ExecutionTransition(execution, complete_work="current")
        if execution.queue_deadline <= now:
            return expire_queued(execution, now)
    elif execution.status is not ExecutionStatus.INTERRUPTED:
        return ExecutionTransition(execution, complete_work="current")
    return None


def authorize_start(
    execution: AutomationExecution,
    *,
    kind: WorkKind,
    admitted: bool,
    now: datetime,
    execution_timeout: timedelta,
    start_token: str,
) -> StartAuthorization:
    """Grant the only target start after Store ownership and admission checks."""

    if kind is not WorkKind.EXECUTE:
        raise ClaimLostError("Only executable work can authorize a target")
    if execution.start_authorized_at is not None:
        raise StartAlreadyAuthorizedError(
            "Execution start was already authorized",
            context={"execution_id": execution.execution_id},
        )
    if execution.status is not ExecutionStatus.QUEUED or not admitted:
        raise ClaimLostError("Execution is no longer eligible to start")
    started = replace(
        execution,
        status=ExecutionStatus.RUNNING,
        execution_started_at=now,
        execution_deadline=now + execution_timeout,
        start_authorized_at=now,
        start_token=start_token,
        updated_at=now,
    )
    return StartAuthorization(execution=started, start_token=start_token)


def interrupt_execution(
    execution: AutomationExecution, *, interrupt_ids: tuple[str, ...], now: datetime
) -> ExecutionTransition:
    """Retain admission and schedule only the interrupted run's original deadline."""

    if not interrupt_ids:
        raise ValueError("interrupt_ids must not be empty")
    if execution.execution_deadline is None:
        raise ClaimLostError("Execution has no active deadline")
    return ExecutionTransition(
        replace(
            execution,
            status=ExecutionStatus.INTERRUPTED,
            interrupt_ids=tuple(dict.fromkeys(interrupt_ids)),
            updated_at=now,
        ),
        complete_work="current",
        deadline_work_at=execution.execution_deadline,
    )


def finish_execution(
    execution: AutomationExecution,
    *,
    status: ExecutionStatus,
    now: datetime,
    result: JsonValue | None = None,
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> ExecutionTransition:
    """Complete work while retaining admission for uncertain external outcomes."""

    if not status.is_terminal and status is not ExecutionStatus.NEEDS_ATTENTION:
        raise ValueError("finish status must be terminal or needs_attention")
    return ExecutionTransition(
        replace(
            execution,
            status=status,
            result=result,
            failure_code=failure_code,
            failure_message=failure_message,
            finished_at=now if status.is_terminal else None,
            updated_at=now,
        ),
        complete_work="all",
        release_capacity=status.is_terminal,
    )


def cancel_execution(
    execution: AutomationExecution, now: datetime
) -> ExecutionTransition:
    """Cancel unstarted work or record intent without claiming a running target stopped."""

    if execution.status is ExecutionStatus.NEEDS_ATTENTION:
        raise ResolutionNotAllowedError(
            "Uncertain execution requires explicit resolution",
            context={"execution_id": execution.execution_id},
        )
    if execution.status in {ExecutionStatus.QUEUED, ExecutionStatus.INTERRUPTED}:
        return ExecutionTransition(
            replace(
                execution,
                status=ExecutionStatus.CANCELLED,
                finished_at=now,
                updated_at=now,
            ),
            complete_work="all",
            release_capacity=True,
        )
    if execution.status is ExecutionStatus.RUNNING:
        return ExecutionTransition(
            replace(execution, status=ExecutionStatus.CANCEL_REQUESTED, updated_at=now)
        )
    return ExecutionTransition(execution)


def expire_interrupted(
    execution: AutomationExecution, now: datetime
) -> ExecutionTransition:
    """Settle an interrupted execution after its pending deadline work becomes due."""
    if (
        execution.status is not ExecutionStatus.INTERRUPTED
        or execution.execution_deadline is None
        or execution.execution_deadline > now
    ):
        raise ClaimLostError("Interrupted execution is not due for expiry")
    return finish_execution(
        execution,
        status=ExecutionStatus.TIMED_OUT,
        now=now,
        failure_code="automation.execution_timeout",
        failure_message="Interrupted execution exceeded its deadline",
    )


def resolve_execution(
    execution: AutomationExecution,
    *,
    resolution: AttentionResolution,
    reason: str,
    now: datetime,
) -> ExecutionTransition:
    """Record an explicit outcome before releasing uncertain execution capacity."""

    if not reason or reason != reason.strip():
        raise ValueError("reason must be a non-empty canonical value")
    if execution.status is not ExecutionStatus.NEEDS_ATTENTION:
        raise ResolutionNotAllowedError(
            "Only uncertain execution can be resolved",
            context={"execution_id": execution.execution_id},
        )
    status = ExecutionStatus(resolution.value)
    return ExecutionTransition(
        replace(
            execution,
            status=status,
            failure_code="automation.explicit_resolution"
            if status is ExecutionStatus.FAILED
            else execution.failure_code,
            failure_message=reason if status is ExecutionStatus.FAILED else None,
            finished_at=now,
            updated_at=now,
        ),
        release_capacity=True,
    )


def expire_queued(execution: AutomationExecution, now: datetime) -> ExecutionTransition:
    """Release even a previously admitted attempt when its queue deadline expires."""

    return ExecutionTransition(
        replace(
            execution,
            status=ExecutionStatus.TIMED_OUT,
            finished_at=now,
            failure_code="automation.queue_timeout",
            failure_message="Execution exceeded its queue deadline",
            updated_at=now,
        ),
        complete_work="current",
        release_capacity=True,
    )


def recover_expired_claim(
    execution: AutomationExecution, kind: WorkKind, now: datetime
) -> ExecutionTransition:
    """Stop automatic restart after authorization; otherwise allow fenced reclaim."""

    if kind is WorkKind.EXECUTE and execution.start_authorized_at is not None:
        return ExecutionTransition(
            replace(
                execution,
                status=ExecutionStatus.NEEDS_ATTENTION,
                failure_code="automation.claim_lost_after_start",
                failure_message="Execution ownership expired after start",
                updated_at=now,
            ),
            complete_work="current",
        )
    return ExecutionTransition(execution)
