"""Public value objects for scheduled tasks and their executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity

from ._json import require_finite_json
from .policies import ExecutionLimits, MisfirePolicy
from .schedules import Schedule

JsonObject: TypeAlias = Mapping[str, JsonValue]


def _validate_persisted_text(
    value: object, *, name: str, maximum: int | None = None
) -> None:
    """Reject text values that differ across the supported SQL backends."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical value")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid UTF-8 text") from error
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters")


class TaskStatus(StrEnum):
    """Whether a task may create new scheduled executions."""

    ENABLED = "enabled"
    PAUSED = "paused"


class ExecutionStatus(StrEnum):
    """Current framework-owned state of one execution attempt."""

    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"

    @property
    def is_terminal(self) -> bool:
        """Return whether no automatic framework transition may execute the run."""

        return self in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
        }


class ExecutionOrigin(StrEnum):
    """Reason an execution was created."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    ONE_TIME = "one_time"
    RETRY = "retry"


class AttentionResolution(StrEnum):
    """Audited outcomes allowed for an execution with uncertain external state."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AutomationTask:
    """A persisted task definition visible to its owning application subject."""

    task_id: str
    namespace: str
    owner_id: str
    name: str
    target: str
    input: JsonObject
    schedule: Schedule
    status: TaskStatus
    revision: int
    next_run_at: datetime | None
    misfire_policy: MisfirePolicy
    limits: ExecutionLimits
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Require finite JSON and freeze the top-level input mapping."""

        _validate_persisted_text(self.task_id, name="task_id", maximum=36)
        _validate_persisted_text(self.namespace, name="namespace", maximum=128)
        _validate_persisted_text(self.owner_id, name="owner_id", maximum=191)
        _validate_persisted_text(self.name, name="name", maximum=255)
        _validate_persisted_text(self.target, name="target", maximum=191)
        require_finite_json(self.input)
        object.__setattr__(self, "input", MappingProxyType(dict(self.input)))


@dataclass(frozen=True, slots=True)
class AutomationExecution:
    """A persisted attempt to run a task snapshot or one-time input."""

    execution_id: str
    task_id: str | None
    namespace: str
    owner_id: str
    identity: RunIdentity
    target: str
    input: JsonObject
    limits: ExecutionLimits
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
    start_token: str | None = field(repr=False)
    created_at: datetime
    updated_at: datetime

    task_name: str | None = None

    def __post_init__(self) -> None:
        """Require finite JSON and freeze the top-level input mapping."""

        if self.task_name is not None:
            _validate_persisted_text(self.task_name, name="task_name", maximum=255)
        _validate_persisted_text(self.execution_id, name="execution_id", maximum=36)
        if self.task_id is not None:
            _validate_persisted_text(self.task_id, name="task_id", maximum=36)
        _validate_persisted_text(self.namespace, name="namespace", maximum=128)
        _validate_persisted_text(self.owner_id, name="owner_id", maximum=191)
        _validate_persisted_text(self.target, name="target", maximum=191)
        _validate_persisted_text(
            self.identity.namespace, name="identity.namespace", maximum=128
        )
        _validate_persisted_text(self.identity.thread_id, name="identity.thread_id")
        _validate_persisted_text(self.identity.run_id, name="identity.run_id")
        if self.retry_of is not None:
            _validate_persisted_text(self.retry_of, name="retry_of", maximum=36)
        if self.identity.namespace != self.namespace:
            raise ValueError("identity.namespace must match namespace")
        if self.failure_code is not None:
            _validate_persisted_text(self.failure_code, name="failure_code")
        if self.failure_message is not None:
            _validate_persisted_text(self.failure_message, name="failure_message")
        for index, interrupt_id in enumerate(self.interrupt_ids):
            _validate_persisted_text(interrupt_id, name=f"interrupt_ids[{index}]")
        require_finite_json(self.input)
        require_finite_json(self.result)
        object.__setattr__(self, "input", MappingProxyType(dict(self.input)))


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    """A safe explicit failure returned by a target or interrupt callback."""

    code: str
    message: str

    def __post_init__(self) -> None:
        """Require stable non-empty failure information."""

        _validate_persisted_text(self.code, name="code")
        _validate_persisted_text(self.message, name="message")


@dataclass(frozen=True, slots=True)
class InterruptedExecution:
    """An unfinished graph execution presented to the host for classification.

    Returning ``None`` from the host callback keeps the execution interrupted under
    its original deadline. The callback cannot approve, reject, or resume the graph.
    """

    task_id: str | None
    execution_id: str
    identity: RunIdentity
    interrupt_ids: tuple[str, ...]
    interrupted_at: datetime
    execution_deadline: datetime


@dataclass(frozen=True, slots=True)
class TaskPage:
    """A stable page of tasks and an optional keyset cursor."""

    items: tuple[AutomationTask, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPage:
    """A stable page of task executions and an optional keyset cursor."""

    items: tuple[AutomationExecution, ...]
    next_cursor: str | None = None


__all__ = [
    "AttentionResolution",
    "AutomationExecution",
    "AutomationTask",
    "ExecutionFailure",
    "ExecutionLimits",
    "ExecutionOrigin",
    "ExecutionPage",
    "ExecutionStatus",
    "InterruptedExecution",
    "JsonObject",
    "TaskPage",
    "TaskStatus",
]
