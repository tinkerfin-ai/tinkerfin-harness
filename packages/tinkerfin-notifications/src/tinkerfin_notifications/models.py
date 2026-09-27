"""Resource identity, bounded notification data, and explicit resynchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


def _identifier(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError("notification identifiers must be strings")
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError("notification identifier is empty, padded, or too long")
    value.encode("utf-8")
    return value


@dataclass(frozen=True, slots=True)
class NotificationScope:
    """Identify a host-owned namespace and an optional owner within it.

    A scope does not grant authorization. When used as a subscription filter, an
    omitted owner selects the whole namespace; hosts must authorize that access.
    An owner-specific filter receives only that owner's events, including no
    ownerless namespace events. Other namespaces never match.
    No namespace string is interpreted as a user, workspace, or platform identity.
    """

    namespace: str
    owner_id: str | None = None

    def __post_init__(self) -> None:
        """Reject noncanonical identities before any transport operation."""
        _identifier(self.namespace, maximum=128)
        if self.owner_id is not None:
            _identifier(self.owner_id, maximum=1024)


class Notification(BaseModel):
    """Invalidate one resource without carrying its authoritative state.

    Publish only after the source's authoritative commit. Details contain bounded
    routing facts such as a generation or related resource ID, never credentials,
    message content, tool results, or signed URLs. The source owns their schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    scope: NotificationScope
    topic: str = Field(min_length=1, max_length=128)
    key: str = Field(
        min_length=1,
        max_length=2048,
        description="Source-owned stable resource key used to coalesce changes",
    )
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("topic", "key")
    @classmethod
    def canonical_identifiers(cls, value: str) -> str:
        """Preserve exact resource identity rather than silently normalizing it."""
        return _identifier(value, maximum=2048)


@dataclass(frozen=True, slots=True)
class ResyncRequired:
    """Require authoritative reads after notification continuity is lost."""

    reason: Literal["overflow", "disconnected", "reconnected"]


@dataclass(frozen=True, slots=True)
class NotificationLimits:
    """Bound subscription state and message size independently of event history."""

    max_subscriptions: int = 1024
    max_pending_per_subscription: int = 128
    max_notification_bytes: int = 4096
    max_pending_publications: int = 128

    def __post_init__(self) -> None:
        """Reject disabled or unbounded capacities."""
        for value in (
            self.max_subscriptions,
            self.max_pending_per_subscription,
            self.max_notification_bytes,
            self.max_pending_publications,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("notification capacities must be positive integers")
