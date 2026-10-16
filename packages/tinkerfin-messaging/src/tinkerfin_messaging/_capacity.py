"""Bound retained checkpoint evidence and terminal expiry scheduling."""

from __future__ import annotations

import heapq

from .models import RecoveryCheckpoint

__all__ = ["_ExpiryIndex", "checkpoint_bytes"]


def checkpoint_bytes(checkpoint: RecoveryCheckpoint | None) -> int:
    """Count the retained recovery position and its optional message identifier."""

    if checkpoint is None:
        return 0
    return len(checkpoint.position) + len((checkpoint.last_message_id or "").encode())


class _ExpiryIndex:
    """Track terminal threads without retaining unbounded stale heap entries.

    The dictionary owns the current deadline. Lazy heap entries are compacted after
    updates so storage stays below twice the live entries plus 64. Active threads have
    no entry, and repeated finish/reopen cycles cannot grow the index indefinitely.
    """

    def __init__(self) -> None:
        self._deadlines: dict[tuple[str, str], float] = {}
        self._heap: list[tuple[float, tuple[str, str]]] = []

    def set(self, key: tuple[str, str], deadline: float | None) -> None:
        if deadline is None:
            self._deadlines.pop(key, None)
        else:
            self._deadlines[key] = deadline
            heapq.heappush(self._heap, (deadline, key))
        if len(self._heap) > 2 * len(self._deadlines) + 64:
            self._heap = [(value, key) for key, value in self._deadlines.items()]
            heapq.heapify(self._heap)

    def pop_due(self, now: float) -> tuple[str, str] | None:
        while self._heap and self._heap[0][0] <= now:
            deadline, key = heapq.heappop(self._heap)
            if self._deadlines.get(key) == deadline:
                del self._deadlines[key]
                return key
        return None
