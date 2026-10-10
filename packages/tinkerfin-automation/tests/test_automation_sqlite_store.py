from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from tinkerfin_automation import (
    AutomationExecution,
    AutomationTask,
    ExecutionLimits,
    ExecutionOrigin,
    ExecutionStatus,
    MisfirePolicy,
    OnceSchedule,
    TaskStatus,
)
from tinkerfin_contracts import RunIdentity


def _task(now: datetime) -> AutomationTask:
    return AutomationTask(
        task_id="00000000-0000-0000-0000-000000000001",
        namespace="app",
        owner_id="owner-1",
        execution_namespace="app",
        name="Daily summary",
        target="summary",
        input={"project_id": "project-1"},
        schedule=OnceSchedule(at=now + timedelta(hours=1)),
        status=TaskStatus.ENABLED,
        revision=1,
        next_run_at=now + timedelta(hours=1),
        misfire_policy=MisfirePolicy(),
        limits=ExecutionLimits(max_queued_runs=2),
        created_at=now,
        updated_at=now,
    )


def _execution(
    task: AutomationTask, now: datetime, *, execution_id: str
) -> AutomationExecution:
    return AutomationExecution(
        execution_id=execution_id,
        task_id=task.task_id,
        namespace=task.namespace,
        owner_id=task.owner_id,
        identity=RunIdentity(
            namespace=task.namespace,
            thread_id=f"thread-{execution_id}",
            run_id=f"run-{execution_id}",
        ),
        target=task.target,
        input=task.input,
        limits=task.limits,
        origin=ExecutionOrigin.MANUAL,
        status=ExecutionStatus.QUEUED,
        attempt=1,
        retry_of=None,
        scheduled_for=None,
        queued_at=now,
        queue_deadline=now + timedelta(hours=1),
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


def _taskless_execution(
    now: datetime,
    *,
    execution_id: str,
    owner_id: str = "owner-1",
    limits: ExecutionLimits | None = None,
) -> AutomationExecution:
    task = replace(_task(now), owner_id=owner_id)
    return replace(
        _execution(task, now, execution_id=execution_id),
        task_id=None,
        origin=ExecutionOrigin.ONE_TIME,
        limits=limits or task.limits,
    )
