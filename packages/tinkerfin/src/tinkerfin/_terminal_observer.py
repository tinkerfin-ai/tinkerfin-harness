"""Await a lightweight terminal callback within the normal observation lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from tinkerfin_contracts import (
    ObservationBoundary,
    RunSourceContext,
    RunTerminalObservation,
    RuntimeObservation,
)

TerminalObserver = Callable[[RunTerminalObservation], Awaitable[None]]


class _TerminalSession:
    """Deliver the selected outcome once, without background work or retries."""

    def __init__(self, on_terminal: TerminalObserver) -> None:
        """Borrow the callback for one run without starting background work."""

        self._on_terminal = on_terminal
        self._delivered = False
        self._failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )

    async def observe(self, observation: RuntimeObservation) -> None:
        """Await the selected terminal once, preserving callback failure."""

        if not isinstance(observation, RunTerminalObservation) or self._delivered:
            return
        self._delivered = True
        pending = self._on_terminal(observation)
        if not inspect.isawaitable(pending):
            raise TypeError("on_terminal must return an awaitable")
        await pending

    async def force(self, boundary: ObservationBoundary) -> None:
        """Complete immediately because each delivery is already awaited."""

        # observe() already waits for the callback, so no buffered work remains.
        return None

    def failure_waiter(self) -> Awaitable[BaseException]:
        """Remain pending while the session has no asynchronous failure source."""

        return self._failure

    async def aclose(self) -> None:
        """Release the session waiter without taking ownership of the callback."""

        self._failure.cancel()


class TerminalCallbackObserver:
    """Create an independent callback session for each admitted run."""

    def __init__(self, on_terminal: TerminalObserver) -> None:
        """Borrow the application callback for each admitted run."""

        self._on_terminal = on_terminal

    async def open_run(self, context: RunSourceContext) -> _TerminalSession:
        """Create the session that awaits this run's selected terminal."""

        return _TerminalSession(self._on_terminal)
