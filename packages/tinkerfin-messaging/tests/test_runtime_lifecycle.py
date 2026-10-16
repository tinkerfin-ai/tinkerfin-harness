"""Messaging, source, producer, and subscription lifecycle contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    Messaging,
)
from tinkerfin_messaging.backend_contract import (
    MessagingBackend,
)


def _identity(*, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="conversation-1", run_id=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _Source:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.items = items
        self.release = release
        self.error = error
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self.items:
                yield item
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


async def test_messaging_shutdown_closes_active_source_and_owned_tasks(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source("one", release=release)
    messaging = Messaging(backend=messaging_backend, settlement_timeout=None)

    async def cancel() -> None:
        release.set()

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    await source.started.wait()
    await messaging.__aexit__(None, None, None)

    live_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    assert source.close_calls == 1
    assert not any(name.startswith("tinkerfin-messaging-") for name in live_names)


async def test_shutdown_waits_for_an_accepted_cancel_callback_tail(
    messaging_backend: MessagingBackend,
) -> None:
    source_release = asyncio.Event()
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=messaging_backend)

    async def cancel() -> tuple[str, ...]:
        callback_started.set()
        await callback_release.wait()
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        identity=_identity(),
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
    await callback_started.wait()

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    callback_release.set()
    cancel_result = await cancelling
    await closing
    replay = [first.data, *[message.data async for message in delivery]]

    assert shutdown_waited
    assert cancel_result is True
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1
