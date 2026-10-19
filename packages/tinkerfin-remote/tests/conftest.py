"""Drive polling deadlines without elapsed wall time or timer thresholds."""

import asyncio
from collections.abc import Callable
from contextvars import Context

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def controlled_polling_clock(monkeypatch: pytest.MonkeyPatch):
    """Advance the polling signal at scheduling points; keep deadlines explicit.

    These tests cover operation and ownership ordering, not elapsed duration.
    No real timer may decide an outcome. Short periodic timers are delivered
    through the loop's ready queue; longer deadlines remain unexpired until
    their operation leaves its scope or a test explicitly cancels its owner.
    """
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: 0.0)
    timers: set[asyncio.TimerHandle] = set()

    def call_at(
        when: float,
        callback: Callable[..., object],
        *args: object,
        context: Context | None = None,
    ) -> asyncio.TimerHandle:
        handle = asyncio.TimerHandle(when, callback, args, loop, context=context)
        timers.add(handle)

        def fire() -> None:
            timers.discard(handle)
            if not handle.cancelled():
                callback(*args)

        if when <= 1.0:
            loop.call_soon(fire, context=context)
        return handle

    monkeypatch.setattr(loop, "call_at", call_at)
    yield
    for timer in timers:
        timer.cancel()
