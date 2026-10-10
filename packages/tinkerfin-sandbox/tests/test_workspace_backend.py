"""Fixed workspace HTTP ownership, file routing, and cancellation contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, blocked: bool = False) -> None:
        self.body = body
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield self.body

    async def aclose(self) -> None:
        self.closed = True
