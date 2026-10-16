"""Immutable retention policy for terminal transport generations."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MessagingRetentionPolicy:
    """Configure how long a terminal thread stream remains replayable.

    Active producers never expire. A new Run admitted before the deadline clears the
    timer and continues the same generation. Disabled retention keeps terminal streams
    until explicit deletion. Capacity exhaustion rejects new storage; it never evicts
    active or unexpired history. Enabled retention is reclaimed on subsequent backend
    activity, including writes to other threads. Identity and tombstone records remain
    counted against total record capacity after generation cleanup.

    Attributes:
        terminal_ttl_seconds: Positive terminal replay window, or ``None`` to disable
            automatic expiry.
    """

    terminal_ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        """Normalize the configured duration and reject unsafe retention windows."""

        value = self.terminal_ttl_seconds
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("terminal_ttl_seconds must be numeric or None")
        resolved = float(value)
        if not math.isfinite(resolved) or resolved <= 0:
            raise ValueError("terminal_ttl_seconds must be finite and positive")
        object.__setattr__(self, "terminal_ttl_seconds", resolved)

    @classmethod
    def disabled(cls) -> MessagingRetentionPolicy:
        """Keep terminal transport generations until explicit deletion."""

        return cls()

    @classmethod
    def expire_after(cls, seconds: float) -> MessagingRetentionPolicy:
        """Expire terminal transport generations after a positive duration.

        Args:
            seconds: Replay window measured from the latest terminal settlement.

        Returns:
            Immutable enabled policy.

        Raises:
            TypeError: ``seconds`` is not numeric.
            ValueError: ``seconds`` is non-finite or not positive.
        """

        return cls(terminal_ttl_seconds=seconds)

    @property
    def enabled(self) -> bool:
        """Return whether terminal streams have an automatic replay deadline."""

        return self.terminal_ttl_seconds is not None


__all__ = ["MessagingRetentionPolicy"]
