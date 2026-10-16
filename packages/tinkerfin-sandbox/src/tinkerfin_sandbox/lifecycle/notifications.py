"""Optional observations of confirmed Sandbox lifecycle changes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


class OpenSandboxLifecycleEventType(StrEnum):
    """Sandbox and warm-capacity changes visible to lifecycle observers."""

    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    REPLACED = "replaced"
    RECOVERY_FAILED = "recovery_failed"
    DESTROYED = "destroyed"
    PAUSED = "paused"
    RESUMED = "resumed"
    WORKSPACE_RESET = "workspace_reset"
    WARM_CAPACITY_DEGRADED = "warm_capacity_degraded"
    WARM_CAPACITY_RESTORED = "warm_capacity_restored"


class OpenSandboxLifecycleReason(StrEnum):
    """Stable causes that never include provider-controlled exception text."""

    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    UNHEALTHY = "unhealthy"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    PROVIDER_REJECTED = "provider_rejected"
    PROTOCOL_ERROR = "protocol_error"
    INITIALIZATION_FAILED = "initialization_failed"
    STATE_FAILURE = "state_failure"
    UNEXPECTED_FAILURE = "unexpected_failure"
    CONNECTION_RESTORED = "connection_restored"
    BINDING_CHANGED = "binding_changed"
    EXPLICIT_RECREATE = "explicit_recreate"
    EXPLICIT_RESET = "explicit_reset"
    EXPLICIT_DESTROY = "explicit_destroy"
    EXPLICIT_PAUSE = "explicit_pause"
    EXPLICIT_RESUME = "explicit_resume"
    WARM_CAPACITY_UNAVAILABLE = "warm_capacity_unavailable"
    WARM_CAPACITY_AVAILABLE = "warm_capacity_available"


@dataclass(frozen=True, slots=True)
class OpenSandboxLifecycleEvent:
    """Describe one confirmed transition without workspace or credential contents.

    Events are process-local observations, not durable delivery receipts. Hosts
    choose which client-safe fields to forward. ``diagnostic_context`` is a copied,
    immutable mapping reserved for trusted observers; it contains remote IDs and
    must not be serialized into browser or other untrusted responses. Owner keys
    are supplied by the host and must be suitable for its notification audience.

    Args:
        event_id: Unique identity of this observation, shared by all observers.
        type: Confirmed lifecycle change.
        owner_key: Host-resolved owner identity, or ``None`` for warm capacity.
        namespace: Logical resource scope, or None for standalone or warm capacity.
        occurred_at: Time of observation as an aware UTC datetime.
        reason: Stable cause without provider exception text.
        workspace_may_have_changed: Whether this event flags possible workspace
            changes from connection initialization, uncertain recovery effects,
            confirmed remote absence, replacement, reset, or destruction. ``False``
            means this observation supplies no such evidence; it never certifies
            file integrity or absence of independent writes. Retaining an instance
            does not roll back initializer side effects.
        diagnostic_context: Remote identifiers for trusted diagnostics only.
    """

    event_id: str
    type: OpenSandboxLifecycleEventType
    owner_key: str | None
    occurred_at: datetime
    reason: OpenSandboxLifecycleReason
    workspace_may_have_changed: bool
    diagnostic_context: Mapping[str, str] = field(
        default_factory=dict[str, str], repr=False
    )
    namespace: str | None = None

    def __post_init__(self) -> None:
        """Detach diagnostic values from their caller-owned mapping."""
        object.__setattr__(
            self, "diagnostic_context", MappingProxyType(dict(self.diagnostic_context))
        )

    @property
    def recovered(self) -> bool:
        """Whether this event confirms restored Sandbox or warm availability."""
        return self.type in {
            OpenSandboxLifecycleEventType.RECOVERED,
            OpenSandboxLifecycleEventType.RESUMED,
            OpenSandboxLifecycleEventType.REPLACED,
            OpenSandboxLifecycleEventType.WARM_CAPACITY_RESTORED,
        }

    @property
    def replaced(self) -> bool:
        """Whether the owner now uses a different remote Sandbox."""
        return self.type is OpenSandboxLifecycleEventType.REPLACED


class OpenSandboxLifecycleObserver(Protocol):
    """Observe lifecycle facts without participating in resource decisions.

    The manager borrows observers and never closes them. Each observer receives
    events in publication order through its own bounded queue. Callbacks must use
    cooperative asynchronous I/O and propagate cancellation; synchronous blocking
    work or cancellation suppression cannot be forcibly stopped by the manager.
    Callbacks and tasks they spawn must not call resource operations or close on
    the same manager. They may enqueue work for an independently owned host worker.
    """

    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        """Observe a confirmed transition within the configured delivery timeout.

        Args:
            event: Immutable event whose diagnostic fields are trusted-only.
        """
        ...


@dataclass(frozen=True, slots=True)
class OpenSandboxNotificationOptions:
    """Limit pending notifications and cooperative observer execution.

    A full observer queue drops the new event. Failures, timeouts, and observer
    cancellation do not change Sandbox operation results. Closing drains accepted
    events in order; with cooperative callbacks the remaining delivery wait is at
    most ``(max_pending_events + 1) * timeout`` per observer, concurrently.

    Args:
        max_pending_events: Per-observer pending capacity, excluding one active call.
        timeout: Maximum seconds for one cooperative observer call.

    Raises:
        TypeError: A limit has the wrong type.
        ValueError: A limit is not positive and finite.
    """

    max_pending_events: int = 128
    timeout: float = 1.0

    def __post_init__(self) -> None:
        """Reject unbounded or unusable notification limits."""
        if isinstance(self.max_pending_events, bool) or not isinstance(
            self.max_pending_events, int
        ):
            raise TypeError("max_pending_events must be an integer")
        if self.max_pending_events < 1:
            raise ValueError("max_pending_events must be positive")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int | float):
            raise TypeError("timeout must be a number")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be positive and finite")
