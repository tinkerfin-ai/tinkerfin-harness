"""Stable public failures for OpenSandbox capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class OpenSandboxErrorCode(StrEnum):
    """Stable machine-readable categories for OpenSandbox failures."""

    ERROR = "sandbox.error"
    STATE_ERROR = "sandbox.state_error"
    STATE_OWNERSHIP_LOST = "sandbox.state_ownership_lost"
    STATE_CONFIGURATION = "sandbox.state_configuration"
    STATE_UNAVAILABLE = "sandbox.state_unavailable"
    STATE_TIMEOUT = "sandbox.state_timeout"
    STATE_PROTOCOL_ERROR = "sandbox.state_protocol_error"
    STATE_COMMIT_UNCERTAIN = "sandbox.state_commit_uncertain"
    STATE_UNEXPECTED_FAILURE = "sandbox.state_unexpected_failure"
    PURPOSE_MISMATCH = "sandbox.purpose_mismatch"
    BACKEND_ERROR = "sandbox.backend_error"
    FILE_TOO_LARGE = "sandbox.file_too_large"
    FILE_CHANGED = "sandbox.file_changed"
    NOT_TEXT = "sandbox.not_text"
    WORKSPACE_NOT_INITIALIZED = "sandbox.workspace_not_initialized"
    BACKEND_UNAVAILABLE = "sandbox.backend_unavailable"
    BACKEND_TIMEOUT = "sandbox.backend_timeout"
    PAUSED = "sandbox.paused"
    BUSY = "sandbox.busy"
    LIFECYCLE_UNCERTAIN = "sandbox.lifecycle_uncertain"
    BACKEND_PROTOCOL_ERROR = "sandbox.backend_protocol_error"
    BACKEND_UNEXPECTED_FAILURE = "sandbox.backend_unexpected_failure"
    INITIALIZATION_FAILED = "sandbox.initialization_failed"
    DESTROY_FAILED = "sandbox.destroy_failed"
    RESET_FAILED = "sandbox.reset_failed"
    HANDLE_OWNERSHIP = "sandbox.handle_ownership"
    HANDLE_CLOSED = "sandbox.handle_closed"
    MANAGER_CLOSED = "sandbox.manager_closed"
    OBSERVER_REENTRY = "sandbox.observer_reentry"
    WARM_POOL_UNAVAILABLE = "sandbox.warm_pool_unavailable"
    SETTLEMENT_TIMEOUT = "sandbox.settlement_timeout"


class OpenSandboxError(Exception):
    """Base failure for framework-owned OpenSandbox semantics.

    ``message`` and ``context`` are safe to expose at a client boundary.
    ``diagnostic_context`` and ``cause`` are reserved for trusted logs and
    telemetry and must not be serialized into client responses.

    Args:
        message: Safe human-readable failure summary.
        context: Client-safe machine-readable semantic values.
        diagnostic_context: Implementation details for trusted observability.
        cause: Original failure retained for debugging and exception chaining.
    """

    code: OpenSandboxErrorCode = OpenSandboxErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize client-safe context and trusted diagnostic evidence."""

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


class OpenSandboxPurposeError(OpenSandboxError):
    """The requested capability does not match a Sandbox's committed purpose.

    A binding keeps its purpose through recovery and replacement. Only explicit
    unbinding permits a later allocation to select a different purpose. Unknown
    or conflicting remote purpose metadata never authorizes access.
    """

    code = OpenSandboxErrorCode.PURPOSE_MISMATCH


class OpenSandboxStateError(OpenSandboxError, RuntimeError):
    """OpenSandbox allocation state could not complete an atomic operation."""

    code = OpenSandboxErrorCode.STATE_ERROR


class OpenSandboxStateOwnershipError(OpenSandboxStateError):
    """An expired or superseded State claim attempted to mutate ownership."""

    code = OpenSandboxErrorCode.STATE_OWNERSHIP_LOST


class OpenSandboxStateConfigurationError(OpenSandboxStateError):
    """Active workers disagree about one shared State configuration."""

    code = OpenSandboxErrorCode.STATE_CONFIGURATION


class OpenSandboxStateUnavailableError(OpenSandboxStateError):
    """The configured State implementation is unavailable."""

    code = OpenSandboxErrorCode.STATE_UNAVAILABLE


class OpenSandboxStateTimeoutError(OpenSandboxStateError):
    """A State operation exceeded its bounded wait."""

    code = OpenSandboxErrorCode.STATE_TIMEOUT


class OpenSandboxStateProtocolError(OpenSandboxStateError):
    """Persisted State violates the supported storage contract."""

    code = OpenSandboxErrorCode.STATE_PROTOCOL_ERROR


class OpenSandboxStateCommitUncertainError(OpenSandboxStateError):
    """A State commit response was lost, so its outcome is unknown."""

    code = OpenSandboxErrorCode.STATE_COMMIT_UNCERTAIN


class UnexpectedOpenSandboxStateError(OpenSandboxStateError):
    """A replaceable State implementation leaked an undeclared failure."""

    code = OpenSandboxErrorCode.STATE_UNEXPECTED_FAILURE


class OpenSandboxBackendError(OpenSandboxError, RuntimeError):
    """A remote Sandbox backend operation failed."""

    code = OpenSandboxErrorCode.BACKEND_ERROR


class OpenSandboxBackendUnavailableError(OpenSandboxBackendError):
    """The remote Sandbox provider is unavailable."""

    code = OpenSandboxErrorCode.BACKEND_UNAVAILABLE


class OpenSandboxBackendTimeoutError(OpenSandboxBackendError):
    """A remote Sandbox operation exceeded its bounded wait."""

    code = OpenSandboxErrorCode.BACKEND_TIMEOUT


class OpenSandboxPausedError(OpenSandboxBackendError):
    """The Sandbox requires an explicit successful resume before ordinary use."""

    code = OpenSandboxErrorCode.PAUSED


class OpenSandboxBusyError(OpenSandboxBackendError):
    """A lifecycle transition has temporarily stopped accepting new operations."""

    code = OpenSandboxErrorCode.BUSY


class OpenSandboxLifecycleUncertainError(OpenSandboxBackendError):
    """A dispatched lifecycle request has no confirmed remote outcome.

    The existing instance and binding remain authoritative. Conflicting lifecycle
    requests must wait for evidence of the issued request's outcome.
    """

    code = OpenSandboxErrorCode.LIFECYCLE_UNCERTAIN


class OpenSandboxFileTooLargeError(OpenSandboxBackendError):
    """A binary file exceeds the caller's byte limit; no partial bytes are returned."""

    code = OpenSandboxErrorCode.FILE_TOO_LARGE


class OpenSandboxFileChangedError(OpenSandboxBackendError):
    """A file, directory page, or project changed during a read; reread its state."""

    code = OpenSandboxErrorCode.FILE_CHANGED


class OpenSandboxNotTextError(OpenSandboxBackendError):
    """The selected entry cannot be safely read as a regular UTF-8 text file."""

    code = OpenSandboxErrorCode.NOT_TEXT


class OpenSandboxWorkspaceNotInitializedError(OpenSandboxBackendError):
    """No existing project file root is available; the read created no resources."""

    code = OpenSandboxErrorCode.WORKSPACE_NOT_INITIALIZED


class OpenSandboxBackendProtocolError(OpenSandboxBackendError):
    """A remote Sandbox response violates the supported protocol."""

    code = OpenSandboxErrorCode.BACKEND_PROTOCOL_ERROR


class UnexpectedOpenSandboxBackendError(OpenSandboxBackendError):
    """A replaceable Sandbox backend leaked an undeclared failure."""

    code = OpenSandboxErrorCode.BACKEND_UNEXPECTED_FAILURE


class OpenSandboxInitializationError(OpenSandboxBackendError):
    """Workspace preparation or a caller initializer failed; recovery cannot retry it."""

    code = OpenSandboxErrorCode.INITIALIZATION_FAILED


class OpenSandboxDestroyError(OpenSandboxError, RuntimeError):
    """Explicit destruction was not confirmed, so the target remains retryable."""

    code = OpenSandboxErrorCode.DESTROY_FAILED


class OpenSandboxResetError(OpenSandboxError, RuntimeError):
    """Workspace reset failed safely without changing remote identity or binding."""

    code = OpenSandboxErrorCode.RESET_FAILED


class OpenSandboxHandleOwnershipError(OpenSandboxError, RuntimeError):
    """A caller tried to close a stable handle outside its lifecycle manager."""

    code = OpenSandboxErrorCode.HANDLE_OWNERSHIP


class OpenSandboxHandleClosedError(OpenSandboxError, RuntimeError):
    """A caller attempted to use a closed stable handle."""

    code = OpenSandboxErrorCode.HANDLE_CLOSED


class OpenSandboxManagerClosedError(OpenSandboxError, RuntimeError):
    """A lifecycle operation was requested after manager shutdown began."""

    code = OpenSandboxErrorCode.MANAGER_CLOSED


class OpenSandboxObserverReentryError(OpenSandboxError, RuntimeError):
    """A lifecycle observer tried to operate or close the manager delivering it."""

    code = OpenSandboxErrorCode.OBSERVER_REENTRY


class OpenSandboxWarmPoolUnavailableError(OpenSandboxError, RuntimeError):
    """The configured ready Sandbox capacity cannot currently be guaranteed."""

    code = OpenSandboxErrorCode.WARM_POOL_UNAVAILABLE


class OpenSandboxSettlementTimeoutError(OpenSandboxError, TimeoutError):
    """A caller stopped waiting before manager settlement completed."""

    code = OpenSandboxErrorCode.SETTLEMENT_TIMEOUT

    def __init__(self, *, timeout: float) -> None:
        """Initialize a timeout failure with its caller settlement budget."""

        self.timeout = timeout
        super().__init__(
            f"OpenSandbox settlement timed out after {timeout:g} seconds",
            context={"timeout": timeout},
        )


__all__ = [
    "OpenSandboxBackendError",
    "OpenSandboxBackendProtocolError",
    "OpenSandboxBackendTimeoutError",
    "OpenSandboxBackendUnavailableError",
    "OpenSandboxDestroyError",
    "OpenSandboxError",
    "OpenSandboxErrorCode",
    "OpenSandboxFileChangedError",
    "OpenSandboxHandleClosedError",
    "OpenSandboxHandleOwnershipError",
    "OpenSandboxInitializationError",
    "OpenSandboxManagerClosedError",
    "OpenSandboxNotTextError",
    "OpenSandboxObserverReentryError",
    "OpenSandboxResetError",
    "OpenSandboxSettlementTimeoutError",
    "OpenSandboxStateCommitUncertainError",
    "OpenSandboxStateConfigurationError",
    "OpenSandboxStateError",
    "OpenSandboxStateOwnershipError",
    "OpenSandboxStateProtocolError",
    "OpenSandboxStateTimeoutError",
    "OpenSandboxStateUnavailableError",
    "OpenSandboxWarmPoolUnavailableError",
    "OpenSandboxWorkspaceNotInitializedError",
    "UnexpectedOpenSandboxBackendError",
    "UnexpectedOpenSandboxStateError",
]
