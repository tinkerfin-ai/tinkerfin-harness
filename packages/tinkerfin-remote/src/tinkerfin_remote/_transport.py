"""Own duplex delivery without owning accepted tool execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ._tasks import settle


async def changed_or_poll(changed: asyncio.Event) -> None:
    try:
        async with asyncio.timeout(1):
            await changed.wait()
    except TimeoutError:
        return


async def serve(
    *,
    send: Callable[[str], Awaitable[None]],
    receive: Callable[[], Awaitable[str]],
    outgoing: Callable[[], Awaitable[str]],
    incoming: Callable[[str], Awaitable[None]],
    changed: asyncio.Event,
) -> None:
    async def produce() -> None:
        while True:
            changed.clear()
            await send(await outgoing())
            await changed_or_poll(changed)

    async def consume() -> None:
        while True:
            await incoming(await receive())

    tasks = (asyncio.create_task(produce()), asyncio.create_task(consume()))
    try:
        completed, _pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in completed:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await settle(asyncio.gather(*tasks, return_exceptions=True))
