"""Failures that preserve remote authorization and uncertain side effects."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

_ContextValue = str | int | float | bool | None


class RemoteErrorCode(StrEnum):
    """Classify execution failures without exposing host credentials."""

    CLOSED = "remote.closed"
    CONFLICT = "remote.conflict"
    PROTOCOL = "remote.protocol"
    UNAVAILABLE = "remote.unavailable"
    UNCERTAIN = "remote.uncertain"
    CANCELLED = "remote.cancelled"
    CAPACITY = "remote.capacity"
    STORAGE = "remote.storage"
    REJECTED = "remote.rejected"
    TIMEOUT = "remote.timeout"
    CLEANUP = "remote.cleanup"


class RemoteError(RuntimeError):
    """Report a confirmed failure or an explicitly uncertain execution outcome.

    Args:
        code: Stable remote failure classification.
        message: Client-safe explanation.
        context: Client-safe scalar details, copied into a read-only mapping.
        diagnostic_context: Private scalar details for trusted diagnostics only.
        cause: Original failure, retained privately and through exception chaining.

    Only message, code and context may be exposed to clients. Diagnostic details
    and causes must not be serialized or logged without separate safety review.
    """

    def __init__(
        self,
        code: RemoteErrorCode,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Copy contexts and retain the original failure outside client-safe fields."""
        self.code = code
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

    @property
    def workspace_failure(self) -> Literal["busy", "stopped", "unavailable", "timeout"]:
        """Classify this failure when it occurs during managed workspace preparation."""
        if self.code == RemoteErrorCode.TIMEOUT:
            return "timeout"
        if self.code in {RemoteErrorCode.CLOSED, RemoteErrorCode.CANCELLED}:
            return "stopped"
        if self.code in {RemoteErrorCode.CAPACITY, RemoteErrorCode.CONFLICT}:
            return "busy"
        return "unavailable"


class WorkspaceRejected(RemoteError):
    """Reject a host authorization before preparing or executing local resources."""

    def __init__(
        self,
        message: str = "Workspace authorization was rejected",
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Retain the host's safe reason and private evidence using RemoteError's boundary."""
        super().__init__(
            RemoteErrorCode.REJECTED,
            message,
            context=context,
            diagnostic_context=diagnostic_context,
            cause=cause,
        )


__all__ = ["RemoteError", "RemoteErrorCode", "WorkspaceRejected"]
