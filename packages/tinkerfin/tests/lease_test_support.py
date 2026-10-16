"""Drive fake Redis leases through operation signals without wall-clock deadlines."""

import asyncio
from types import ModuleType, SimpleNamespace

import pytest

import tinkerfin.redis._lease_lock as lease_module


class LeaseClock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waiting = asyncio.Event()
        self.tick = asyncio.Event()
        controlled = ModuleType("controlled_asyncio")
        controlled.__dict__.update(vars(asyncio))
        setattr(controlled, "sleep", self.sleep)
        setattr(
            controlled, "get_running_loop", lambda: SimpleNamespace(time=lambda: 0.0)
        )
        setattr(controlled, "timeout", lambda _delay: asyncio.timeout(None))
        setattr(controlled, "timeout_at", lambda _deadline: asyncio.timeout(None))
        monkeypatch.setattr(lease_module, "asyncio", controlled)

    async def sleep(self, seconds: float) -> None:
        self.waiting.set()
        await self.tick.wait()
        self.tick.clear()
