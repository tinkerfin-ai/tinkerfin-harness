"""Stable public failures for Automation operations."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class AutomationErrorCode(StrEnum):
    """Stable machine-readable categories for Automation failures."""

    ERROR = "automation.error"
    INVALID_SCHEDULE = "automation.invalid_schedule"
    TASK_NOT_FOUND = "automation.task_not_found"
    EXECUTION_NOT_FOUND = "automation.execution_not_found"
    TASK_CONFLICT = "automation.task_conflict"
    REQUEST_CONFLICT = "automation.request_conflict"
    QUEUE_FULL = "automation.queue_full"
    EXECUTION_BUSY = "automation.execution_busy"
    START_ALREADY_AUTHORIZED = "automation.start_already_authorized"
    CLAIM_LOST = "automation.claim_lost"
    RETRY_NOT_ALLOWED = "automation.retry_not_allowed"
    RESOLUTION_NOT_ALLOWED = "automation.resolution_not_allowed"
    TARGET_NOT_FOUND = "automation.target_not_found"
    TARGET_FAILED = "automation.target_failed"
    INTERRUPT_CALLBACK_FAILED = "automation.interrupt_callback_failed"
    STORE_UNAVAILABLE = "automation.store_unavailable"
    STORE_TIMEOUT = "automation.store_timeout"
    STORE_PROTOCOL_ERROR = "automation.store_protocol_error"
    SCHEDULER_UNAVAILABLE = "automation.scheduler_unavailable"
    LIFECYCLE = "automation.lifecycle"
    WAIT_TIMEOUT = "automation.wait_timeout"


class AutomationError(Exception):
    """Base failure with separate public and trusted diagnostic context."""

    code: AutomationErrorCode = AutomationErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Snapshot safe context and retain trusted diagnostic evidence."""

        self.message = message
        self.context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(context or {})
        )
        self.diagnostic_context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(diagnostic_context or {})
        )
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class InvalidScheduleError(AutomationError, ValueError):
    """A schedule cannot produce the requested sequence of UTC fire times."""

    code = AutomationErrorCode.INVALID_SCHEDULE


class TaskNotFoundError(AutomationError, LookupError):
    """The requested task does not exist in the caller's ownership scope."""

    code = AutomationErrorCode.TASK_NOT_FOUND


class ExecutionNotFoundError(AutomationError, LookupError):
    """The requested execution does not exist in the caller's ownership scope."""

    code = AutomationErrorCode.EXECUTION_NOT_FOUND


class TaskConflictError(AutomationError, RuntimeError):
    """A task changed after the caller read its revision."""

    code = AutomationErrorCode.TASK_CONFLICT


class RequestConflictError(AutomationError, RuntimeError):
    """An idempotency key was reused for a different operation."""

    code = AutomationErrorCode.REQUEST_CONFLICT


class QueueFullError(AutomationError, RuntimeError):
    """A task cannot accept another queued execution under its current limit."""

    code = AutomationErrorCode.QUEUE_FULL


class ExecutionBusyError(AutomationError, RuntimeError):
    """Execution is deferred because a required concurrency slot is unavailable."""

    code = AutomationErrorCode.EXECUTION_BUSY


class StartAlreadyAuthorizedError(AutomationError, RuntimeError):
    """A persisted execution start authorization cannot be issued again."""

    code = AutomationErrorCode.START_ALREADY_AUTHORIZED


class ClaimLostError(AutomationError, RuntimeError):
    """A worker no longer owns the fenced work item it attempted to settle."""

    code = AutomationErrorCode.CLAIM_LOST


class RetryNotAllowedError(AutomationError, RuntimeError):
    """An execution cannot be retried without violating its safety contract."""

    code = AutomationErrorCode.RETRY_NOT_ALLOWED


class ResolutionNotAllowedError(AutomationError, RuntimeError):
    """An execution is not eligible for explicit uncertainty resolution."""

    code = AutomationErrorCode.RESOLUTION_NOT_ALLOWED


class TargetNotFoundError(AutomationError, LookupError):
    """The task references no executable target registered by the trusted host."""

    code = AutomationErrorCode.TARGET_NOT_FOUND


class TargetExecutionError(AutomationError, RuntimeError):
    """A registered target failed through the public execution boundary."""

    code = AutomationErrorCode.TARGET_FAILED


class InterruptCallbackError(TargetExecutionError):
    """The host interrupt callback failed while classifying an unfinished run."""

    code = AutomationErrorCode.INTERRUPT_CALLBACK_FAILED


class AutomationStoreError(AutomationError, RuntimeError):
    """A replaceable Automation Store operation failed."""

    code = AutomationErrorCode.STORE_UNAVAILABLE


class AutomationStoreTimeout(AutomationStoreError, TimeoutError):
    """An Automation Store operation exceeded its bounded wait."""

    code = AutomationErrorCode.STORE_TIMEOUT


class AutomationStoreProtocolError(AutomationStoreError):
    """An Automation Store returned data outside its public contract."""

    code = AutomationErrorCode.STORE_PROTOCOL_ERROR


class AutomationSchedulerError(AutomationError, RuntimeError):
    """A replaceable scheduler failed to maintain task wakeups."""

    code = AutomationErrorCode.SCHEDULER_UNAVAILABLE


class AutomationLifecycleError(AutomationError, RuntimeError):
    """The Automation service or engine is used outside its lifecycle."""

    code = AutomationErrorCode.LIFECYCLE


class AutomationWaitTimeout(AutomationError, TimeoutError):
    """The observer's wait expired without cancelling the underlying execution."""

    code = AutomationErrorCode.WAIT_TIMEOUT


__all__ = [
    "AutomationError",
    "AutomationErrorCode",
    "AutomationLifecycleError",
    "AutomationSchedulerError",
    "AutomationStoreError",
    "AutomationStoreProtocolError",
    "AutomationStoreTimeout",
    "AutomationWaitTimeout",
    "ClaimLostError",
    "ExecutionBusyError",
    "ExecutionNotFoundError",
    "InterruptCallbackError",
    "InvalidScheduleError",
    "QueueFullError",
    "RequestConflictError",
    "ResolutionNotAllowedError",
    "RetryNotAllowedError",
    "StartAlreadyAuthorizedError",
    "TargetExecutionError",
    "TargetNotFoundError",
    "TaskConflictError",
    "TaskNotFoundError",
]
