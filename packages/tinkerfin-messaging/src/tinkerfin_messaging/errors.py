"""Stable public failures for protocol-neutral messaging operations."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from tinkerfin_contracts import RunIdentity

_ContextValue: TypeAlias = str | int | float | bool | None


class MessagingErrorCode(StrEnum):
    """Stable machine-readable categories for Messaging failures."""

    ERROR = "messaging.error"
    NOT_STARTED = "messaging.not_started"
    SETTLEMENT_TIMEOUT = "messaging.settlement_timeout"
    CLOSED = "messaging.closed"
    INVALID_CURSOR = "messaging.invalid_cursor"
    CODEC_MISMATCH = "messaging.codec_mismatch"
    SOURCE_PROFILE_MISMATCH = "messaging.source_profile_mismatch"
    MESSAGE_ID_CONFLICT = "messaging.message_id_conflict"
    PUBLICATION_REJECTED = "messaging.publication_rejected"
    QUOTA_EXCEEDED = "messaging.quota_exceeded"
    RUN_ALREADY_ACTIVE = "messaging.run_already_active"
    RUN_REQUEST_CONFLICT = "messaging.run_request_conflict"
    RUN_NOT_FOUND = "messaging.run_not_found"
    RUN_PRODUCER_FAILED = "messaging.run_producer_failed"
    CANCELLATION_UNSUPPORTED = "messaging.cancellation_unsupported"
    RECOVERY_UNSUPPORTED = "messaging.recovery_unsupported"
    SSE_RENDERING_UNSUPPORTED = "messaging.sse_rendering_unsupported"
    BACKEND_OWNERSHIP_LOST = "messaging.backend_ownership_lost"
    STREAM_EXPIRED = "messaging.stream_expired"
    STREAM_DELETED = "messaging.stream_deleted"
    STREAM_DELETE_CONFLICT = "messaging.stream_delete_conflict"
    BACKEND_UNAVAILABLE = "messaging.backend_unavailable"
    BACKEND_TIMEOUT = "messaging.backend_timeout"
    BACKEND_PROTOCOL_ERROR = "messaging.backend_protocol_error"
    UNEXPECTED_BACKEND_FAILURE = "messaging.unexpected_backend_failure"


def _identity_context(identity: RunIdentity) -> dict[str, _ContextValue]:
    return {
        "thread_id": identity.thread_id,
        "run_id": identity.run_id,
    }


class MessagingError(Exception):
    """Base failure with separate public and trusted diagnostic context.

    ``message`` and ``context`` are safe to expose at a client boundary.
    ``diagnostic_context`` and ``cause`` are reserved for trusted logs and
    telemetry and must not be serialized into client responses.

    Args:
        message: Safe human-readable failure summary.
        context: Client-safe machine-readable semantic values.
        diagnostic_context: Implementation details for trusted observability.
        cause: Original failure retained for debugging and exception chaining.
    """

    code: MessagingErrorCode = MessagingErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize public context and trusted diagnostic evidence."""

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

    def _enrich_diagnostic_context(
        self,
        context: Mapping[str, _ContextValue],
    ) -> None:
        self.diagnostic_context = MappingProxyType(
            {**self.diagnostic_context, **context}
        )


class MessagingNotStarted(MessagingError):
    """The Messaging facade has not entered its asynchronous lifecycle."""

    code = MessagingErrorCode.NOT_STARTED


class MessagingSettlementTimeout(MessagingError):
    """A caller stopped waiting before Messaging settlement completed."""

    code = MessagingErrorCode.SETTLEMENT_TIMEOUT

    def __init__(self, *, timeout: float) -> None:
        """Initialize a timeout failure with its caller settlement budget."""

        self.timeout = timeout
        super().__init__(
            f"Messaging settlement timed out after {timeout:g} seconds",
            context={"timeout": timeout},
        )


class MessagingClosed(MessagingError):
    """The single-use Messaging facade has already closed."""

    code = MessagingErrorCode.CLOSED


class RunRequestConflict(MessagingError):
    """A retained run is already bound to different command content."""

    code = MessagingErrorCode.RUN_REQUEST_CONFLICT

    def __init__(self, *, identity: RunIdentity) -> None:
        """Identify the conflicting run without exposing either request digest."""
        self.identity = identity
        super().__init__(
            "Run identity is already bound to another request",
            context=_identity_context(identity),
        )


class InvalidCursor(MessagingError):
    """A replay cursor falls outside the retained stream range."""

    code = MessagingErrorCode.INVALID_CURSOR

    def __init__(self, *, after: int, latest: int) -> None:
        """Initialize an invalid cursor failure with the retained range."""

        self.after = after
        self.latest = latest
        super().__init__(
            f"Replay cursor {after} is outside the retained range 0..{latest}",
            context={"after": after, "latest": latest},
        )


class CodecMismatch(MessagingError):
    """One logical channel stream was opened with a different durable codec."""

    code = MessagingErrorCode.CODEC_MISMATCH

    def __init__(self, *, expected: str, actual: str) -> None:
        """Initialize a durable codec mismatch with both codec identifiers."""

        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Stream codec is {expected!r}; it cannot be read as {actual!r}",
            context={"expected": expected, "actual": actual},
        )


class SourceProfileMismatch(MessagingError):
    """A built-in source profile is missing or contradicts its codec types."""

    code = MessagingErrorCode.SOURCE_PROFILE_MISMATCH

    def __init__(self, *, profile: str, reason: str) -> None:
        """Initialize a source profile failure with its safe incompatibility reason."""

        self.profile = profile
        self.reason = reason
        super().__init__(
            f"Source profile {profile!r} is incompatible: {reason}",
            context={"profile": profile},
        )


class PublicationRejected(MessagingError):
    """An existing run or its protocol does not accept an external message.

    Args:
        identity: Exact run targeted by the publisher.
        reason: Safe rejection category identifying the violated publication rule.
    """

    code = MessagingErrorCode.PUBLICATION_REJECTED

    def __init__(self, *, identity: RunIdentity, reason: str) -> None:
        """Retain safe run identity and rejection reason for the caller."""
        super().__init__(
            "The run does not accept this publication",
            context={**_identity_context(identity), "reason": reason},
        )


class MessageIdConflict(MessagingError):
    """A message ID was retried with different committed content."""

    code = MessagingErrorCode.MESSAGE_ID_CONFLICT

    def __init__(self, *, identity: RunIdentity, message_id: str) -> None:
        """Initialize a conflicting idempotency-key failure."""

        self.identity = identity
        self.message_id = message_id
        super().__init__(
            f"Message {message_id!r} already exists in thread "
            f"{identity.thread_id!r} with different content",
            context={**_identity_context(identity), "message_id": message_id},
        )


class MessagingQuotaExceeded(MessagingError):
    """A commit would exceed one configured durable capacity boundary."""

    code = MessagingErrorCode.QUOTA_EXCEEDED

    def __init__(self, *, resource: str, limit: int) -> None:
        """Initialize a capacity failure with the exceeded resource and limit."""

        self.resource = resource
        self.limit = limit
        super().__init__(
            f"Messaging {resource} limit of {limit} was exceeded",
            context={"resource": resource, "limit": limit},
        )


class RunAlreadyActive(MessagingError):
    """A different run already owns one channel and stream slot."""

    code = MessagingErrorCode.RUN_ALREADY_ACTIVE

    def __init__(
        self,
        *,
        active_identity: RunIdentity,
        requested_identity: RunIdentity,
    ) -> None:
        """Initialize an ownership conflict with active and requested runs."""

        self.active_identity = active_identity
        self.requested_identity = requested_identity
        super().__init__(
            f"Run {active_identity.run_id!r} is already active; cannot start "
            f"{requested_identity.run_id!r}",
            context={
                "thread_id": requested_identity.thread_id,
                "active_run_id": active_identity.run_id,
                "requested_run_id": requested_identity.run_id,
            },
        )


class RunNotFound(MessagingError):
    """No run record exists for the requested channel and stream."""

    code = MessagingErrorCode.RUN_NOT_FOUND

    def __init__(self, *, identity: RunIdentity) -> None:
        """Initialize a missing-run failure for one durable identity."""

        self.identity = identity
        super().__init__(
            f"Run {identity.run_id!r} was not found",
            context=_identity_context(identity),
        )


class RunProducerFailed(MessagingError):
    """A producer failed after zero or more messages were committed."""

    code = MessagingErrorCode.RUN_PRODUCER_FAILED

    def __init__(self, *, identity: RunIdentity, cause: BaseException) -> None:
        """Initialize a producer failure while preserving its original cause."""

        self.identity = identity
        super().__init__(
            f"Producer for run {identity.run_id!r} failed",
            context=_identity_context(identity),
            cause=cause,
        )


class CancellationUnsupported(MessagingError):
    """The active producer has no application cancellation callback."""

    code = MessagingErrorCode.CANCELLATION_UNSUPPORTED

    def __init__(self, *, identity: RunIdentity) -> None:
        """Initialize an unsupported cancellation failure for one run."""

        self.identity = identity
        super().__init__(
            f"Run {identity.run_id!r} does not support cancellation",
            context=_identity_context(identity),
        )


class RecoveryUnsupported(MessagingError):
    """A non-recoverable source cannot be rebuilt after owner loss."""

    code = MessagingErrorCode.RECOVERY_UNSUPPORTED


class SseRenderingUnsupported(MessagingError):
    """A channel has no SSE renderer for its decoded payload type."""

    code = MessagingErrorCode.SSE_RENDERING_UNSUPPORTED


class BackendOwnershipLost(MessagingError):
    """A stale producer attempted to mutate a run after losing ownership."""

    code = MessagingErrorCode.BACKEND_OWNERSHIP_LOST


class StreamDeleted(MessagingError):
    """A handle refers to a stream generation that has been deleted."""

    code = MessagingErrorCode.STREAM_DELETED

    def __init__(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        generation: int | None,
    ) -> None:
        """Initialize a stale-generation failure for one deleted stream."""

        self.channel = channel
        self.identity = identity
        self.generation = generation
        generation_text = (
            "the current generation"
            if generation is None
            else f"generation {generation}"
        )
        super().__init__(
            f"Thread {identity.thread_id!r} in channel {channel!r} {generation_text} "
            "has been deleted",
            context={
                **_identity_context(identity),
                "channel": channel,
                "generation": generation,
            },
        )


class StreamExpired(MessagingError):
    """A terminal transport generation is outside its replay window."""

    code = MessagingErrorCode.STREAM_EXPIRED

    def __init__(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        generation: int,
    ) -> None:
        """Initialize an expired-generation failure for Trace rehydration.

        Args:
            channel: Durable codec channel that owned the transport log.
            identity: Requested semantic Run identity.
            generation: Expired thread-stream generation.
        """

        self.channel = channel
        self.identity = identity
        self.generation = generation
        super().__init__(
            f"Thread {identity.thread_id!r} in channel {channel!r} generation "
            f"{generation} is outside the replay retention window",
            context={
                **_identity_context(identity),
                "channel": channel,
                "generation": generation,
            },
        )


class StreamDeleteConflict(MessagingError):
    """A stream cannot be deleted while one producer lease is active."""

    code = MessagingErrorCode.STREAM_DELETE_CONFLICT

    def __init__(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        active_identity: RunIdentity,
    ) -> None:
        """Initialize a deletion conflict with the active producer identity."""

        self.channel = channel
        self.identity = identity
        self.active_identity = active_identity
        super().__init__(
            f"Thread {identity.thread_id!r} in channel {channel!r} has active run "
            f"{active_identity.run_id!r}",
            context={
                **_identity_context(identity),
                "channel": channel,
                "active_run_id": active_identity.run_id,
            },
        )


class MessagingBackendError(MessagingError):
    """Base failure for replaceable Messaging backend implementations."""

    code = MessagingErrorCode.UNEXPECTED_BACKEND_FAILURE


class MessagingBackendUnavailable(MessagingBackendError):
    """The configured backend is unavailable for one operation."""

    code = MessagingErrorCode.BACKEND_UNAVAILABLE


class MessagingBackendTimeout(MessagingBackendError):
    """A backend operation exceeded its bounded wait."""

    code = MessagingErrorCode.BACKEND_TIMEOUT


class MessagingBackendProtocolError(MessagingBackendError):
    """A backend response violates the supported durable protocol."""

    code = MessagingErrorCode.BACKEND_PROTOCOL_ERROR


class UnexpectedMessagingBackendError(MessagingBackendError):
    """A replaceable backend leaked an undeclared ordinary exception."""

    code = MessagingErrorCode.UNEXPECTED_BACKEND_FAILURE


__all__ = [
    "BackendOwnershipLost",
    "CancellationUnsupported",
    "CodecMismatch",
    "InvalidCursor",
    "MessageIdConflict",
    "MessagingBackendError",
    "MessagingBackendProtocolError",
    "MessagingBackendTimeout",
    "MessagingBackendUnavailable",
    "MessagingClosed",
    "MessagingError",
    "MessagingErrorCode",
    "MessagingNotStarted",
    "MessagingQuotaExceeded",
    "MessagingSettlementTimeout",
    "RecoveryUnsupported",
    "RunAlreadyActive",
    "RunNotFound",
    "RunProducerFailed",
    "SourceProfileMismatch",
    "SseRenderingUnsupported",
    "StreamDeleteConflict",
    "StreamDeleted",
    "StreamExpired",
    "UnexpectedMessagingBackendError",
]
