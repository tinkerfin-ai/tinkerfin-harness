"""Canonical persistence codec for Automation task and execution snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

from tinkerfin_contracts import RunIdentity

from ._json import require_finite_json
from .errors import AutomationStoreProtocolError
from .models import (
    AutomationExecution,
    AutomationTask,
    ExecutionOrigin,
    ExecutionStatus,
    TaskStatus,
)
from .policies import ExecutionLimits, MisfireMode, MisfirePolicy
from .schedules import ScheduleSpec


class _RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LimitsRecord(_RecordModel):
    max_concurrent_runs: int
    max_queued_runs: int
    execution_timeout_seconds: float
    queue_timeout_seconds: float


class _MisfireRecord(_RecordModel):
    mode: MisfireMode
    grace_seconds: float
    catch_up_window_seconds: float
    max_catch_up: int


class _TaskRecord(_RecordModel):
    task_id: str
    namespace: str
    owner_id: str
    execution_namespace: str
    name: str
    target: str
    input: dict[str, JsonValue]
    schedule: ScheduleSpec
    status: TaskStatus
    revision: int
    next_run_at: datetime | None
    misfire_policy: _MisfireRecord
    limits: _LimitsRecord
    created_at: datetime
    updated_at: datetime

    @field_validator("schedule", mode="before")
    @classmethod
    def canonical_schedule(
        cls, value: JsonValue | ScheduleSpec
    ) -> JsonValue | ScheduleSpec:
        if isinstance(value, dict) and not {"active_from", "active_until"}.issubset(
            value
        ):
            raise ValueError("Stored schedules must include their active period")
        return value


class _ExecutionRecord(_RecordModel):
    execution_id: str
    task_id: str | None
    task_name: str | None
    namespace: str
    owner_id: str
    identity: RunIdentity
    target: str
    input: dict[str, JsonValue]
    limits: _LimitsRecord
    origin: ExecutionOrigin
    status: ExecutionStatus
    attempt: int
    retry_of: str | None
    scheduled_for: datetime | None
    queued_at: datetime
    queue_deadline: datetime
    execution_started_at: datetime | None
    execution_deadline: datetime | None
    finished_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    result: JsonValue | None
    interrupt_ids: tuple[str, ...]
    start_authorized_at: datetime | None
    start_token: str | None
    created_at: datetime
    updated_at: datetime


def _invalid_record(
    record_kind: str, error: ValueError | TypeError
) -> AutomationStoreProtocolError:
    return AutomationStoreProtocolError(
        f"Stored Automation {record_kind} is invalid",
        diagnostic_context={
            "record_kind": record_kind,
            "error_type": f"{type(error).__module__}.{type(error).__qualname__}",
        },
        cause=error,
    )


def _limits_record(limits: ExecutionLimits) -> _LimitsRecord:
    return _LimitsRecord(
        max_concurrent_runs=limits.max_concurrent_runs,
        max_queued_runs=limits.max_queued_runs,
        execution_timeout_seconds=limits.execution_timeout.total_seconds(),
        queue_timeout_seconds=limits.queue_timeout.total_seconds(),
    )


def _limits(value: _LimitsRecord) -> ExecutionLimits:
    return ExecutionLimits(
        max_concurrent_runs=value.max_concurrent_runs,
        max_queued_runs=value.max_queued_runs,
        execution_timeout=timedelta(seconds=value.execution_timeout_seconds),
        queue_timeout=timedelta(seconds=value.queue_timeout_seconds),
    )


def _misfire_record(policy: MisfirePolicy) -> _MisfireRecord:
    return _MisfireRecord(
        mode=policy.mode,
        grace_seconds=policy.grace.total_seconds(),
        catch_up_window_seconds=policy.catch_up_window.total_seconds(),
        max_catch_up=policy.max_catch_up,
    )


def _misfire(value: _MisfireRecord) -> MisfirePolicy:
    return MisfirePolicy(
        mode=value.mode,
        grace=timedelta(seconds=value.grace_seconds),
        catch_up_window=timedelta(seconds=value.catch_up_window_seconds),
        max_catch_up=value.max_catch_up,
    )


def encode_task(task: AutomationTask) -> str:
    """Encode a task snapshot without losing JSON numbers or timestamp precision."""

    require_finite_json(task.input)
    return _TaskRecord(
        task_id=task.task_id,
        namespace=task.namespace,
        owner_id=task.owner_id,
        execution_namespace=task.execution_namespace,
        name=task.name,
        target=task.target,
        input=dict(task.input),
        schedule=task.schedule,
        status=task.status,
        revision=task.revision,
        next_run_at=task.next_run_at,
        misfire_policy=_misfire_record(task.misfire_policy),
        limits=_limits_record(task.limits),
        created_at=task.created_at,
        updated_at=task.updated_at,
    ).model_dump_json(by_alias=True)


def decode_task(payload: str) -> AutomationTask:
    """Validate and decode one stored task snapshot."""

    try:
        record = _TaskRecord.model_validate_json(payload)
        return AutomationTask(
            task_id=record.task_id,
            namespace=record.namespace,
            owner_id=record.owner_id,
            execution_namespace=record.execution_namespace,
            name=record.name,
            target=record.target,
            input=record.input,
            schedule=record.schedule,
            status=record.status,
            revision=record.revision,
            next_run_at=record.next_run_at,
            misfire_policy=_misfire(record.misfire_policy),
            limits=_limits(record.limits),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
    except (ValueError, TypeError) as error:
        raise _invalid_record("task payload", error) from error


def encode_execution(execution: AutomationExecution) -> str:
    """Encode an execution snapshot for persistence or command-result replay."""

    require_finite_json(execution.input)
    require_finite_json(execution.result)
    return _ExecutionRecord(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        task_name=execution.task_name,
        namespace=execution.namespace,
        owner_id=execution.owner_id,
        identity=execution.identity,
        target=execution.target,
        input=dict(execution.input),
        limits=_limits_record(execution.limits),
        origin=execution.origin,
        status=execution.status,
        attempt=execution.attempt,
        retry_of=execution.retry_of,
        scheduled_for=execution.scheduled_for,
        queued_at=execution.queued_at,
        queue_deadline=execution.queue_deadline,
        execution_started_at=execution.execution_started_at,
        execution_deadline=execution.execution_deadline,
        finished_at=execution.finished_at,
        failure_code=execution.failure_code,
        failure_message=execution.failure_message,
        result=execution.result,
        interrupt_ids=execution.interrupt_ids,
        start_authorized_at=execution.start_authorized_at,
        start_token=execution.start_token,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    ).model_dump_json(by_alias=True)


def decode_execution(payload: str) -> AutomationExecution:
    """Validate and decode one stored execution snapshot."""

    try:
        record = _ExecutionRecord.model_validate_json(payload)
        return AutomationExecution(
            execution_id=record.execution_id,
            task_name=record.task_name,
            task_id=record.task_id,
            namespace=record.namespace,
            owner_id=record.owner_id,
            identity=record.identity,
            target=record.target,
            input=record.input,
            limits=_limits(record.limits),
            origin=record.origin,
            status=record.status,
            attempt=record.attempt,
            retry_of=record.retry_of,
            scheduled_for=record.scheduled_for,
            queued_at=record.queued_at,
            queue_deadline=record.queue_deadline,
            execution_started_at=record.execution_started_at,
            execution_deadline=record.execution_deadline,
            finished_at=record.finished_at,
            failure_code=record.failure_code,
            failure_message=record.failure_message,
            result=record.result,
            interrupt_ids=record.interrupt_ids,
            start_authorized_at=record.start_authorized_at,
            start_token=record.start_token,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
    except (ValueError, TypeError) as error:
        raise _invalid_record("execution payload", error) from error


def encode_operation_result(
    result: AutomationTask | AutomationExecution | str,
) -> tuple[str, str]:
    """Encode an idempotent command result with an explicit result kind."""

    if isinstance(result, AutomationTask):
        return "task", encode_task(result)
    if isinstance(result, AutomationExecution):
        return "execution", encode_execution(result)
    if isinstance(result, str):
        return "deleted", json.dumps(result, ensure_ascii=False)
    raise TypeError("operation result must be a task, execution, or deleted task ID")


def decode_operation_result(
    kind: str, payload: str
) -> AutomationTask | AutomationExecution | str:
    """Decode one stored idempotent command result."""

    try:
        if kind == "task":
            return decode_task(payload)
        if kind == "execution":
            return decode_execution(payload)
        if kind == "deleted":
            value = json.loads(payload)
            if isinstance(value, str):
                return value
        raise ValueError("stored operation result kind or payload is invalid")
    except AutomationStoreProtocolError:
        raise
    except (ValueError, TypeError) as error:
        raise _invalid_record("operation result", error) from error


__all__ = [
    "decode_execution",
    "decode_operation_result",
    "decode_task",
    "encode_execution",
    "encode_operation_result",
    "encode_task",
]
