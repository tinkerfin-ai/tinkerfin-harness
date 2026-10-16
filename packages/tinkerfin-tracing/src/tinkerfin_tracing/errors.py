"""Stable public failures for semantic tracing operations."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class TracingErrorCode(StrEnum):
    """Stable machine-readable categories for tracing failures."""

    ERROR = "tracing.error"
    INVALID_CURSOR = "tracing.invalid_cursor"
    INVALID_REFERENCE = "tracing.invalid_reference"
    THREAD_NOT_FOUND = "tracing.thread_not_found"
    RUN_NOT_FOUND = "tracing.run_not_found"
    AMBIGUOUS_HEAD = "tracing.ambiguous_head"
    RUN_CONFLICT = "tracing.run_conflict"
    CORRUPTION = "tracing.corruption"
    STORE_UNAVAILABLE = "tracing.store_unavailable"
    STORE_TIMEOUT = "tracing.store_timeout"
    STORE_PROTOCOL_ERROR = "tracing.store_protocol_error"
    QUOTA_EXCEEDED = "tracing.quota_exceeded"
    CAPTURE_REJECTED = "tracing.capture_rejected"
    OBSERVER_FAILED = "tracing.observer_failed"
    PROJECTION_FAILED = "tracing.projection_failed"
    PROJECTION_CHECKPOINT_CONFLICT = "tracing.projection_checkpoint_conflict"
    FOLLOW_LIFECYCLE = "tracing.follow_lifecycle"


class TracingError(Exception):
    """Base failure with separate public and trusted diagnostic context."""

    code: TracingErrorCode = TracingErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Snapshot public context and retain non-serialized diagnostic evidence."""

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


class InvalidTraceCursor(TracingError, ValueError):
    """An opaque cursor is malformed or belongs to another Trace handle."""

    code = TracingErrorCode.INVALID_CURSOR


class TraceThreadNotFound(TracingError, LookupError):
    """The selected Trace thread or generation does not exist."""

    code = TracingErrorCode.THREAD_NOT_FOUND


class InvalidTraceReference(TracingError, ValueError):
    """A retained-detail reference is malformed or belongs to another conversation."""

    code = TracingErrorCode.INVALID_REFERENCE


class TraceRunNotFound(TracingError, LookupError):
    """The requested Run has not entered the selected Trace generation."""

    code = TracingErrorCode.RUN_NOT_FOUND


class AmbiguousTraceHead(TracingError, LookupError):
    """A branched Trace requires an explicit head Run selection."""

    code = TracingErrorCode.AMBIGUOUS_HEAD


class TraceRunConflict(TracingError, RuntimeError):
    """A Run identity is active or already committed in the current generation."""

    code = TracingErrorCode.RUN_CONFLICT


class TraceCorruption(TracingError, RuntimeError):
    """Stored facts violate deterministic Trace invariants."""

    code = TracingErrorCode.CORRUPTION


class TraceStoreError(TracingError, RuntimeError):
    """Base failure for replaceable Trace Store operations."""

    code = TracingErrorCode.STORE_UNAVAILABLE


class TraceStoreTimeout(TraceStoreError, TimeoutError):
    """A Trace Store operation exceeded its bounded wait."""

    code = TracingErrorCode.STORE_TIMEOUT


class TraceStoreProtocolError(TraceStoreError):
    """A Trace Store returned a value outside its public contract."""

    code = TracingErrorCode.STORE_PROTOCOL_ERROR


class TraceQuotaExceeded(TracingError, RuntimeError):
    """A bounded event, thread, or Tracer capacity has been exhausted."""

    code = TracingErrorCode.QUOTA_EXCEEDED


class TraceCaptureRejected(TracingError, ValueError):
    """Safe capture cannot represent essential fact metadata."""

    code = TracingErrorCode.CAPTURE_REJECTED


class TraceObserverFailed(TracingError, RuntimeError):
    """The Tracer's request-scoped Observer session failed."""

    code = TracingErrorCode.OBSERVER_FAILED


class TraceProjectionFailed(TracingError, RuntimeError):
    """A requested optional Projection failed without changing the Ledger."""

    code = TracingErrorCode.PROJECTION_FAILED


class TraceProjectionCheckpointConflict(TraceStoreError):
    """A Projection checkpoint compare-and-swap observed a newer state."""

    code = TracingErrorCode.PROJECTION_CHECKPOINT_CONFLICT


class TraceFollowLifecycleError(TracingError, RuntimeError):
    """A live Trace follower is closed or already serving another operation."""

    code = TracingErrorCode.FOLLOW_LIFECYCLE


__all__ = [
    "AmbiguousTraceHead",
    "InvalidTraceCursor",
    "TraceCaptureRejected",
    "TraceCorruption",
    "TraceFollowLifecycleError",
    "TraceObserverFailed",
    "TraceProjectionCheckpointConflict",
    "TraceProjectionFailed",
    "TraceQuotaExceeded",
    "TraceRunConflict",
    "TraceRunNotFound",
    "TraceStoreError",
    "TraceStoreProtocolError",
    "TraceStoreTimeout",
    "TraceThreadNotFound",
    "TracingError",
    "TracingErrorCode",
]
