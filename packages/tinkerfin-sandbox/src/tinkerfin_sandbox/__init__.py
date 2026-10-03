"""Public OpenSandbox configuration, adapters, lifecycle, and State contracts.

Importing this module does not create a remote Sandbox or open a database. Optional
SQL integrations are loaded only when ``SQLAlchemyOpenSandboxState`` is requested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lifecycle.sqlalchemy import (
        SQLAlchemyOpenSandboxState as SQLAlchemyOpenSandboxState,
    )
    from .lifecycle.sqlalchemy import (
        SQLAlchemyOpenSandboxStateSchema as SQLAlchemyOpenSandboxStateSchema,
    )
    from .lifecycle.sqlalchemy import (
        get_sqlalchemy_opensandbox_state_schema as get_sqlalchemy_opensandbox_state_schema,
    )

from .backends import OpenSandboxBackend as OpenSandboxBackend
from .backends import OpenSandboxHandle as OpenSandboxHandle
from .backends import RootedOpenSandboxBackend as RootedOpenSandboxBackend
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
from .errors import OpenSandboxObserverReentryError as OpenSandboxObserverReentryError
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
from .errors import (
    OpenSandboxStateOwnershipError as OpenSandboxStateOwnershipError,
)
from .errors import (
    OpenSandboxStateProtocolError as OpenSandboxStateProtocolError,
)
from .errors import OpenSandboxStateTimeoutError as OpenSandboxStateTimeoutError
from .errors import (
    OpenSandboxStateUnavailableError as OpenSandboxStateUnavailableError,
)
from .errors import (
    OpenSandboxWarmPoolUnavailableError as OpenSandboxWarmPoolUnavailableError,
)
from .errors import (
    UnexpectedOpenSandboxBackendError as UnexpectedOpenSandboxBackendError,
)
from .errors import UnexpectedOpenSandboxStateError as UnexpectedOpenSandboxStateError
from .lifecycle import InMemoryOpenSandboxState as InMemoryOpenSandboxState
from .lifecycle import OpenSandboxBinding as OpenSandboxBinding
from .lifecycle import OpenSandboxCleanupClaim as OpenSandboxCleanupClaim
from .lifecycle import OpenSandboxClient as OpenSandboxClient
from .lifecycle import OpenSandboxInitializer as OpenSandboxInitializer
from .lifecycle import OpenSandboxLifecycleEvent as OpenSandboxLifecycleEvent
from .lifecycle import OpenSandboxLifecycleEventType as OpenSandboxLifecycleEventType
from .lifecycle import OpenSandboxLifecycleObserver as OpenSandboxLifecycleObserver
from .lifecycle import OpenSandboxLifecycleReason as OpenSandboxLifecycleReason
from .lifecycle import OpenSandboxManager as OpenSandboxManager
from .lifecycle import OpenSandboxNotificationOptions as OpenSandboxNotificationOptions
from .lifecycle import OpenSandboxOwnerClaim as OpenSandboxOwnerClaim
from .lifecycle import OpenSandboxReadyWarmClaim as OpenSandboxReadyWarmClaim
from .lifecycle import OpenSandboxRecoveryPolicy as OpenSandboxRecoveryPolicy
from .lifecycle import OpenSandboxState as OpenSandboxState
from .lifecycle import OpenSandboxWarmClaim as OpenSandboxWarmClaim
from .lifecycle import SandboxWorkspace as SandboxWorkspace
from .lifecycle import WorkspaceChange as WorkspaceChange
from .lifecycle.availability import OpenSandboxAvailability as OpenSandboxAvailability
from .lifecycle.availability import (
    OpenSandboxAvailabilityPhase as OpenSandboxAvailabilityPhase,
)
from .lifecycle.availability import OpenSandboxHolderUpdate as OpenSandboxHolderUpdate
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
from .models import (
    OpenSandboxUnavailableReason as OpenSandboxUnavailableReason,
)


def __getattr__(name: str) -> object:
    """Load optional database integrations only when explicitly requested."""
    if name in {
        "SQLAlchemyOpenSandboxState",
        "SQLAlchemyOpenSandboxStateSchema",
        "get_sqlalchemy_opensandbox_state_schema",
    }:
        try:
            from .lifecycle import sqlalchemy as sqlalchemy_lifecycle
        except ModuleNotFoundError as error:
            if error.name not in {"sqlalchemy", "tinkerfin_sqlalchemy"} and not (
                error.name and error.name.startswith("sqlalchemy.")
            ):
                raise
            raise ImportError(
                'SQLAlchemyOpenSandboxState requires "tinkerfin-sandbox[sqlalchemy]"'
            ) from error
        value = getattr(sqlalchemy_lifecycle, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
