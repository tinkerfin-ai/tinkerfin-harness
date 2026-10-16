"""Clock boundaries used by scheduling and lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol


class AutomationClock(Protocol):
    """Provide wall-clock UTC and monotonic waiting without global time state."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""

        ...

    async def wait_until(self, when: datetime) -> None:
        """Wait until one absolute timezone-aware UTC instant."""

        ...


class SystemClock:
    """Use the process wall and monotonic clocks for production scheduling."""

    def now(self) -> datetime:
        """Return the current UTC wall time."""

        return datetime.now(UTC)

    async def wait_until(self, when: datetime) -> None:
        """Wait until an absolute instant using asyncio's monotonic delay."""

        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("when must be timezone-aware")
        delay = when.astimezone(UTC) - self.now()
        await asyncio.sleep(max(0.0, delay.total_seconds()))


class ManualClock:
    """Advance time explicitly so tests never depend on elapsed wall time."""

    def __init__(self, initial: datetime) -> None:
        """Create a controllable clock at one aware instant."""

        if initial.tzinfo is None or initial.utcoffset() is None:
            raise ValueError("initial must be timezone-aware")
        self._current = initial.astimezone(UTC)
        self._advanced = asyncio.Condition()

    def now(self) -> datetime:
        """Return the current controlled UTC time."""

        return self._current

    async def wait_until(self, when: datetime) -> None:
        """Wait until callers advance the clock to an absolute instant."""

        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("when must be timezone-aware")
        deadline = when.astimezone(UTC)
        async with self._advanced:
            await self._advanced.wait_for(lambda: self._current >= deadline)

    async def advance(self, delta: timedelta) -> datetime:
        """Move the clock forward and wake all due waiters."""

        if delta < timedelta(0):
            raise ValueError("delta must not be negative")
        async with self._advanced:
            self._current += delta
            self._advanced.notify_all()
            return self._current


__all__ = ["AutomationClock", "ManualClock", "SystemClock"]
