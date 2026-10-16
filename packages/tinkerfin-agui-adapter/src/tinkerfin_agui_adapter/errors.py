"""Stable public failures owned by Deep Agents to AG-UI conversion."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class AgUiAdapterErrorCode(StrEnum):
    """Stable machine-readable categories for adapter failures."""

    ERROR = "agui.error"
    STREAM_CONTRACT_INVALID = "agui.stream_contract_invalid"
    CONVERSION_FAILED = "agui.conversion_failed"
    SERIALIZATION_FAILED = "agui.serialization_failed"
    LIFECYCLE_ERROR = "agui.lifecycle_error"
    HITL_CORRELATION_FAILED = "agui.hitl.correlation_failed"
    HITL_NO_MATCH = "agui.hitl.no_match"
    TOOL_REVIEW_CONTRACT_INVALID = "agui.tool_review.contract_invalid"
    RESUME_INTERRUPT_UNSUPPORTED = "agui.resume.interrupt_unsupported"
    RESUME_DUPLICATE_PENDING_INTERRUPT_ID = "agui.resume.duplicate_pending_interrupt_id"
    RESUME_DECISION_NOT_ALLOWED = "agui.resume.decision_not_allowed"
    RESUME_PAYLOAD_REQUIRED = "agui.resume.payload_required"
    RESUME_PAYLOAD_INVALID = "agui.resume.payload_invalid"
    RESUME_NO_PENDING_INTERRUPT = "agui.resume.no_pending_interrupt"
    RESUME_DUPLICATE_INTERRUPT_ID = "agui.resume.duplicate_resume_interrupt_id"
    RESUME_UNKNOWN_INTERRUPT_ID = "agui.resume.unknown_interrupt_id"
    RESUME_INCOMPLETE = "agui.resume.incomplete"
    RESUME_CHECKPOINT_MESSAGES_REQUIRED = "agui.resume.checkpoint_messages_required"


class AgUiAdapterError(Exception):
    """Base failure for framework-owned AG-UI adapter semantics.

    ``message`` and ``context`` are safe to expose at a client boundary.
    ``diagnostic_context`` and ``cause`` are reserved for trusted logs and
    telemetry and must not be serialized into client responses.

    Args:
        message: Safe human-readable failure summary.
        context: Client-safe machine-readable semantic values.
        diagnostic_context: Implementation details for trusted observability.
        cause: Original failure retained for debugging and exception chaining.
    """

    code: AgUiAdapterErrorCode = AgUiAdapterErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize safe public context and trusted diagnostic evidence."""

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


class AgUiStreamContractError(AgUiAdapterError, ValueError):
    """A live StreamPart violates the supported v2 envelope contract."""

    code = AgUiAdapterErrorCode.STREAM_CONTRACT_INVALID


class AgUiConversionError(AgUiAdapterError, ValueError):
    """A valid native value cannot be converted without losing semantics."""

    code = AgUiAdapterErrorCode.CONVERSION_FAILED


class AgUiSerializationError(AgUiAdapterError, ValueError):
    """A converted public event cannot be serialized safely."""

    code = AgUiAdapterErrorCode.SERIALIZATION_FAILED


class AgUiLifecycleError(AgUiAdapterError, RuntimeError):
    """The adapter was used outside its valid conversion lifecycle."""

    code = AgUiAdapterErrorCode.LIFECYCLE_ERROR


class HitlCorrelationError(AgUiConversionError):
    """A HITL action cannot be uniquely correlated to checkpoint Tool calls."""

    code = AgUiAdapterErrorCode.HITL_CORRELATION_FAILED


class HitlNoMatchError(HitlCorrelationError):
    """No complete Tool-call assignment exists for native HITL actions."""

    code = AgUiAdapterErrorCode.HITL_NO_MATCH


__all__ = [
    "AgUiAdapterError",
    "AgUiAdapterErrorCode",
    "AgUiConversionError",
    "AgUiLifecycleError",
    "AgUiSerializationError",
    "AgUiStreamContractError",
    "HitlCorrelationError",
    "HitlNoMatchError",
]
