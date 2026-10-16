"""Caller-selected limits for recovering an existing Sandbox connection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class OpenSandboxRecoveryPolicy:
    """Retry an existing instance before reporting failure or opting into recreation.

    Defaults preserve the existing instance and binding. Recreation allocates a new
    instance and retires the old one; it does not copy workspace files. The policy
    applies to connection and health checks, never to commands, writes, or resets.
    Authentication, permission, protocol, initialization, and State failures are
    reported immediately and cannot authorize recreation.

    Args:
        max_attempts: Total connection or health attempts, including the first.
        initial_delay: Seconds before the second attempt; doubled after each retry.
        max_delay: Maximum delay between attempts in seconds.
        timeout: Work budget in seconds for same-instance connection, health checks,
            and retry waits. Necessary cancellation and resource settlement finish
            before return and can extend elapsed time. Native connection and
            initialization use the earlier client or recovery deadline. Explicit
            recreation uses the client's separate creation deadline.
        on_failure: ``"raise"`` preserves the existing instance. ``"recreate"``
            allows ``get()`` to replace it after eligible recovery failures.
            ``reconnect()`` and ``reset()`` always preserve remote identity.

    Raises:
        TypeError: An attempt count or duration has the wrong type.
        ValueError: A limit is outside its finite supported range.
    """

    max_attempts: int = 2
    initial_delay: float = 15.0
    max_delay: float = 15.0
    timeout: float = 30.0
    on_failure: Literal["raise", "recreate"] = "raise"

    def __post_init__(self) -> None:
        """Reject invalid limits before a manager opens any resources."""
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise TypeError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        for name, value in (
            ("initial_delay", self.initial_delay),
            ("max_delay", self.max_delay),
            ("timeout", self.timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.timeout == 0:
            raise ValueError("timeout must be positive")
        if self.initial_delay > self.max_delay:
            raise ValueError("initial_delay must not exceed max_delay")
        if self.on_failure not in {"raise", "recreate"}:
            raise ValueError("on_failure must be 'raise' or 'recreate'")
