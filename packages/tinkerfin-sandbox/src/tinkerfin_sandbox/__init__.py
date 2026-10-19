"""Isolated native and OpenSandbox workspaces with explicit resource ownership.

Providers load only when selected; importing this package starts no resources.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._native import LocalAccess as LocalAccess
    from ._native import LocalError as LocalError
    from ._native import LocalErrorCode as LocalErrorCode
    from ._native import LocalLimits as LocalLimits
    from ._native import LocalProcess as LocalProcess
    from ._native import LocalSession as LocalSession
    from ._native import LocalWorkspace as LocalWorkspace
    from .backends import OpenSandboxBackend as OpenSandboxBackend
    from .backends import OpenSandboxHandle as OpenSandboxHandle
    from .backends import RootedOpenSandboxBackend as RootedOpenSandboxBackend
    from .directory_contents import (
        WorkspaceDirectoryContents as WorkspaceDirectoryContents,
    )
    from .errors import OpenSandboxBackendError as OpenSandboxBackendError
    from .errors import (
        OpenSandboxBackendProtocolError as OpenSandboxBackendProtocolError,
    )
    from .errors import OpenSandboxBackendTimeoutError as OpenSandboxBackendTimeoutError
    from .errors import (
        OpenSandboxBackendUnavailableError as OpenSandboxBackendUnavailableError,
    )
    from .errors import OpenSandboxBusyError as OpenSandboxBusyError
    from .errors import OpenSandboxDestroyError as OpenSandboxDestroyError
    from .errors import OpenSandboxError as OpenSandboxError
    from .errors import OpenSandboxErrorCode as OpenSandboxErrorCode
    from .errors import OpenSandboxFileChangedError as OpenSandboxFileChangedError
    from .errors import OpenSandboxFileTooLargeError as OpenSandboxFileTooLargeError
    from .errors import OpenSandboxHandleClosedError as OpenSandboxHandleClosedError
    from .errors import (
        OpenSandboxHandleOwnershipError as OpenSandboxHandleOwnershipError,
    )
    from .errors import OpenSandboxInitializationError as OpenSandboxInitializationError
    from .errors import (
        OpenSandboxLifecycleUncertainError as OpenSandboxLifecycleUncertainError,
    )
    from .errors import OpenSandboxManagerClosedError as OpenSandboxManagerClosedError
    from .errors import OpenSandboxNotTextError as OpenSandboxNotTextError
    from .errors import (
        OpenSandboxObserverReentryError as OpenSandboxObserverReentryError,
    )
    from .errors import OpenSandboxPausedError as OpenSandboxPausedError
    from .errors import OpenSandboxPurposeError as OpenSandboxPurposeError
    from .errors import OpenSandboxResetError as OpenSandboxResetError
    from .errors import (
        OpenSandboxSettlementTimeoutError as OpenSandboxSettlementTimeoutError,
    )
    from .errors import (
        OpenSandboxStateCommitUncertainError as OpenSandboxStateCommitUncertainError,
    )
    from .errors import (
        OpenSandboxStateConfigurationError as OpenSandboxStateConfigurationError,
    )
    from .errors import OpenSandboxStateError as OpenSandboxStateError
    from .errors import OpenSandboxStateOwnershipError as OpenSandboxStateOwnershipError
    from .errors import OpenSandboxStateProtocolError as OpenSandboxStateProtocolError
    from .errors import OpenSandboxStateTimeoutError as OpenSandboxStateTimeoutError
    from .errors import (
        OpenSandboxStateUnavailableError as OpenSandboxStateUnavailableError,
    )
    from .errors import (
        OpenSandboxWarmPoolUnavailableError as OpenSandboxWarmPoolUnavailableError,
    )
    from .errors import (
        OpenSandboxWorkspaceNotInitializedError as OpenSandboxWorkspaceNotInitializedError,
    )
    from .errors import SandboxError as SandboxError
    from .errors import (
        UnexpectedOpenSandboxBackendError as UnexpectedOpenSandboxBackendError,
    )
    from .errors import (
        UnexpectedOpenSandboxStateError as UnexpectedOpenSandboxStateError,
    )
    from .lifecycle import InMemoryOpenSandboxState as InMemoryOpenSandboxState
    from .lifecycle import OpenSandboxBinding as OpenSandboxBinding
    from .lifecycle import OpenSandboxCleanupClaim as OpenSandboxCleanupClaim
    from .lifecycle import OpenSandboxClient as OpenSandboxClient
    from .lifecycle import OpenSandboxInitializer as OpenSandboxInitializer
    from .lifecycle import OpenSandboxLifecycleEvent as OpenSandboxLifecycleEvent
    from .lifecycle import (
        OpenSandboxLifecycleEventType as OpenSandboxLifecycleEventType,
    )
    from .lifecycle import OpenSandboxLifecycleObserver as OpenSandboxLifecycleObserver
    from .lifecycle import OpenSandboxLifecycleReason as OpenSandboxLifecycleReason
    from .lifecycle import OpenSandboxManager as OpenSandboxManager
    from .lifecycle import (
        OpenSandboxNotificationOptions as OpenSandboxNotificationOptions,
    )
    from .lifecycle import OpenSandboxOwnerClaim as OpenSandboxOwnerClaim
    from .lifecycle import OpenSandboxReadyWarmClaim as OpenSandboxReadyWarmClaim
    from .lifecycle import OpenSandboxRecoveryPolicy as OpenSandboxRecoveryPolicy
    from .lifecycle import OpenSandboxState as OpenSandboxState
    from .lifecycle import OpenSandboxWarmClaim as OpenSandboxWarmClaim
    from .lifecycle import SandboxWorkspace as SandboxWorkspace
    from .lifecycle import WorkspaceChange as WorkspaceChange
    from .lifecycle.availability import (
        OpenSandboxAvailability as OpenSandboxAvailability,
    )
    from .lifecycle.availability import (
        OpenSandboxAvailabilityPhase as OpenSandboxAvailabilityPhase,
    )
    from .lifecycle.availability import (
        OpenSandboxHolderUpdate as OpenSandboxHolderUpdate,
    )
    from .lifecycle.sqlalchemy import (
        SQLAlchemyOpenSandboxState as SQLAlchemyOpenSandboxState,
    )
    from .lifecycle.sqlalchemy import (
        SQLAlchemyOpenSandboxStateSchema as SQLAlchemyOpenSandboxStateSchema,
    )
    from .lifecycle.sqlalchemy import (
        get_sqlalchemy_opensandbox_state_schema as get_sqlalchemy_opensandbox_state_schema,
    )
    from .middleware import (
        build_rooted_filesystem_middleware as build_rooted_filesystem_middleware,
    )
    from .models import OpenSandboxConfig as OpenSandboxConfig
    from .models import OpenSandboxDetails as OpenSandboxDetails
    from .models import OpenSandboxDiagnosticContent as OpenSandboxDiagnosticContent
    from .models import OpenSandboxPlatformInfo as OpenSandboxPlatformInfo
    from .models import OpenSandboxPurpose as OpenSandboxPurpose
    from .models import OpenSandboxRuntimeInfo as OpenSandboxRuntimeInfo
    from .models import OpenSandboxStatusInfo as OpenSandboxStatusInfo
    from .models import OpenSandboxUnavailableReason as OpenSandboxUnavailableReason
    from .workspace_files import WorkspaceDirectoryPage as WorkspaceDirectoryPage
    from .workspace_files import WorkspaceFileInfo as WorkspaceFileInfo
    from .workspace_files import WorkspaceText as WorkspaceText

_EXPORTS = {
    "InMemoryOpenSandboxState": ".lifecycle",
    "LocalAccess": "._native",
    "LocalError": "._native",
    "LocalErrorCode": "._native",
    "LocalLimits": "._native",
    "LocalProcess": "._native",
    "LocalSession": "._native",
    "LocalWorkspace": "._native",
    "OpenSandboxAvailability": ".lifecycle.availability",
    "OpenSandboxAvailabilityPhase": ".lifecycle.availability",
    "OpenSandboxBackend": ".backends",
    "OpenSandboxBackendError": ".errors",
    "OpenSandboxBackendProtocolError": ".errors",
    "OpenSandboxBackendTimeoutError": ".errors",
    "OpenSandboxBackendUnavailableError": ".errors",
    "OpenSandboxBinding": ".lifecycle",
    "OpenSandboxBusyError": ".errors",
    "OpenSandboxCleanupClaim": ".lifecycle",
    "OpenSandboxClient": ".lifecycle",
    "OpenSandboxConfig": ".models",
    "OpenSandboxDestroyError": ".errors",
    "OpenSandboxDetails": ".models",
    "OpenSandboxDiagnosticContent": ".models",
    "OpenSandboxError": ".errors",
    "OpenSandboxErrorCode": ".errors",
    "OpenSandboxFileChangedError": ".errors",
    "OpenSandboxFileTooLargeError": ".errors",
    "OpenSandboxHandle": ".backends",
    "OpenSandboxHandleClosedError": ".errors",
    "OpenSandboxHandleOwnershipError": ".errors",
    "OpenSandboxHolderUpdate": ".lifecycle.availability",
    "OpenSandboxInitializationError": ".errors",
    "OpenSandboxInitializer": ".lifecycle",
    "OpenSandboxLifecycleEvent": ".lifecycle",
    "OpenSandboxLifecycleEventType": ".lifecycle",
    "OpenSandboxLifecycleObserver": ".lifecycle",
    "OpenSandboxLifecycleReason": ".lifecycle",
    "OpenSandboxLifecycleUncertainError": ".errors",
    "OpenSandboxManager": ".lifecycle",
    "OpenSandboxManagerClosedError": ".errors",
    "OpenSandboxNotTextError": ".errors",
    "OpenSandboxNotificationOptions": ".lifecycle",
    "OpenSandboxObserverReentryError": ".errors",
    "OpenSandboxOwnerClaim": ".lifecycle",
    "OpenSandboxPausedError": ".errors",
    "OpenSandboxPlatformInfo": ".models",
    "OpenSandboxPurpose": ".models",
    "OpenSandboxPurposeError": ".errors",
    "OpenSandboxReadyWarmClaim": ".lifecycle",
    "OpenSandboxRecoveryPolicy": ".lifecycle",
    "OpenSandboxResetError": ".errors",
    "OpenSandboxRuntimeInfo": ".models",
    "OpenSandboxSettlementTimeoutError": ".errors",
    "OpenSandboxState": ".lifecycle",
    "OpenSandboxStateCommitUncertainError": ".errors",
    "OpenSandboxStateConfigurationError": ".errors",
    "OpenSandboxStateError": ".errors",
    "OpenSandboxStateOwnershipError": ".errors",
    "OpenSandboxStateProtocolError": ".errors",
    "OpenSandboxStateTimeoutError": ".errors",
    "OpenSandboxStateUnavailableError": ".errors",
    "OpenSandboxStatusInfo": ".models",
    "OpenSandboxUnavailableReason": ".models",
    "OpenSandboxWarmClaim": ".lifecycle",
    "OpenSandboxWarmPoolUnavailableError": ".errors",
    "OpenSandboxWorkspaceNotInitializedError": ".errors",
    "RootedOpenSandboxBackend": ".backends",
    "SQLAlchemyOpenSandboxState": ".lifecycle.sqlalchemy",
    "SQLAlchemyOpenSandboxStateSchema": ".lifecycle.sqlalchemy",
    "SandboxError": ".errors",
    "SandboxWorkspace": ".lifecycle",
    "UnexpectedOpenSandboxBackendError": ".errors",
    "UnexpectedOpenSandboxStateError": ".errors",
    "WorkspaceChange": ".lifecycle",
    "WorkspaceDirectoryContents": ".directory_contents",
    "WorkspaceDirectoryPage": ".workspace_files",
    "WorkspaceFileInfo": ".workspace_files",
    "WorkspaceText": ".workspace_files",
    "build_rooted_filesystem_middleware": ".middleware",
    "get_sqlalchemy_opensandbox_state_schema": ".lifecycle.sqlalchemy",
}


def __getattr__(name: str) -> object:
    """Load the selected provider without importing unrelated integrations."""
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        value = getattr(import_module(module, __name__), name)
    except ModuleNotFoundError as error:
        missing = (error.name or "").split(".")[0]
        if missing not in {
            "opensandbox",
            "httpx",
            "tinkerfin_notifications",
            "sqlalchemy",
            "tinkerfin_sqlalchemy",
        }:
            raise
        extra = "sqlalchemy" if module == ".lifecycle.sqlalchemy" else "opensandbox"
        raise ImportError(f"{name} requires tinkerfin-sandbox[{extra}]") from error
    globals()[name] = value
    return value


__all__ = [
    "InMemoryOpenSandboxState",
    "LocalAccess",
    "LocalError",
    "LocalErrorCode",
    "LocalLimits",
    "LocalProcess",
    "LocalSession",
    "LocalWorkspace",
    "OpenSandboxAvailability",
    "OpenSandboxAvailabilityPhase",
    "OpenSandboxBackend",
    "OpenSandboxBackendError",
    "OpenSandboxBackendProtocolError",
    "OpenSandboxBackendTimeoutError",
    "OpenSandboxBackendUnavailableError",
    "OpenSandboxBinding",
    "OpenSandboxBusyError",
    "OpenSandboxCleanupClaim",
    "OpenSandboxClient",
    "OpenSandboxConfig",
    "OpenSandboxDestroyError",
    "OpenSandboxDetails",
    "OpenSandboxDiagnosticContent",
    "OpenSandboxError",
    "OpenSandboxErrorCode",
    "OpenSandboxFileChangedError",
    "OpenSandboxFileTooLargeError",
    "OpenSandboxHandle",
    "OpenSandboxHandleClosedError",
    "OpenSandboxHandleOwnershipError",
    "OpenSandboxHolderUpdate",
    "OpenSandboxInitializationError",
    "OpenSandboxInitializer",
    "OpenSandboxLifecycleEvent",
    "OpenSandboxLifecycleEventType",
    "OpenSandboxLifecycleObserver",
    "OpenSandboxLifecycleReason",
    "OpenSandboxLifecycleUncertainError",
    "OpenSandboxManager",
    "OpenSandboxManagerClosedError",
    "OpenSandboxNotTextError",
    "OpenSandboxNotificationOptions",
    "OpenSandboxObserverReentryError",
    "OpenSandboxOwnerClaim",
    "OpenSandboxPausedError",
    "OpenSandboxPlatformInfo",
    "OpenSandboxPurpose",
    "OpenSandboxPurposeError",
    "OpenSandboxReadyWarmClaim",
    "OpenSandboxRecoveryPolicy",
    "OpenSandboxResetError",
    "OpenSandboxRuntimeInfo",
    "OpenSandboxSettlementTimeoutError",
    "OpenSandboxState",
    "OpenSandboxStateCommitUncertainError",
    "OpenSandboxStateConfigurationError",
    "OpenSandboxStateError",
    "OpenSandboxStateOwnershipError",
    "OpenSandboxStateProtocolError",
    "OpenSandboxStateTimeoutError",
    "OpenSandboxStateUnavailableError",
    "OpenSandboxStatusInfo",
    "OpenSandboxUnavailableReason",
    "OpenSandboxWarmClaim",
    "OpenSandboxWarmPoolUnavailableError",
    "OpenSandboxWorkspaceNotInitializedError",
    "RootedOpenSandboxBackend",
    "SQLAlchemyOpenSandboxState",
    "SQLAlchemyOpenSandboxStateSchema",
    "SandboxError",
    "SandboxWorkspace",
    "UnexpectedOpenSandboxBackendError",
    "UnexpectedOpenSandboxStateError",
    "WorkspaceChange",
    "WorkspaceDirectoryContents",
    "WorkspaceDirectoryPage",
    "WorkspaceFileInfo",
    "WorkspaceText",
    "build_rooted_filesystem_middleware",
    "get_sqlalchemy_opensandbox_state_schema",
]
