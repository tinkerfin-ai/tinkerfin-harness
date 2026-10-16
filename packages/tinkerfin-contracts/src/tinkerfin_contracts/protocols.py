"""Structural extension boundaries for TinkerFin Runtime observers."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, runtime_checkable

from .observations import ObservationBoundary, RunSourceContext, RuntimeObservation


@runtime_checkable
class RunObservationSession(Protocol):
    """Own observation work and failure propagation for one Runtime request."""

    async def observe(self, observation: RuntimeObservation) -> None:
        """Accept one observation in Runtime order and treat its values as read-only."""

        ...

    async def force(self, boundary: ObservationBoundary) -> None:
        """Settle every accepted observation before a hard Runtime boundary."""

        ...

    def failure_waiter(self) -> Awaitable[BaseException]:
        """Return an awaitable that resolves with the first asynchronous failure."""

        ...

    async def aclose(self) -> None:
        """Close session-owned work idempotently without closing borrowed resources."""

        ...


@runtime_checkable
class RuntimeObserver(Protocol):
    """Create one managed observation session for each admitted Runtime request."""

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        """Open one request-scoped session without taking ownership of the Runtime.

        This context records the actual request before delayed Graph/checkpoint
        preparation. The session receives determined input and resume lineage in
        RunInputObservation before any Native observation or terminal.
        """

        ...


__all__ = ["RunObservationSession", "RuntimeObserver"]
