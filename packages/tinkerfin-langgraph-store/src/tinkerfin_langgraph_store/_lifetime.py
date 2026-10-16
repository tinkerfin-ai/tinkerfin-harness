"""Track accepted operations without owning the caller's database or task."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

from .errors import LangGraphStoreError, StoreClosedError


class StoreLifetime:
    """Close admission immediately, then join every accepted operation."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _check_loop(self) -> None:
        current = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = current
        elif self._loop is not current:
            raise LangGraphStoreError(
                "A Store must be used and closed on one event loop"
            )

    @contextmanager
    def operation(self) -> Generator[None]:
        """Account for an operation, including time waiting for setup or a connection."""

        self._check_loop()
        if self._closed:
            raise StoreClosedError("The Store is closed")
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if not self._active:
                self._idle.set()

    async def close(self) -> None:
        """Wait for accepted work before delivering repeated close-waiter cancellation."""

        self._check_loop()
        self._closed = True
        cancellation: asyncio.CancelledError | None = None
        # Event.wait cancellation only removes this waiter. Accepted operations are
        # caller-owned and keep their outcome; there is no background shutdown task.
        while self._active:
            try:
                await self._idle.wait()
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        if cancellation is not None:
            raise cancellation
