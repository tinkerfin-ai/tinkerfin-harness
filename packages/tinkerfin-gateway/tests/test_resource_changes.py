"""Bound resource watches use Gateway authorization and response ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Collection
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime

import pytest

from tinkerfin_gateway import Gateway
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notification, Notifications, ResyncRequired


@pytest.fixture(autouse=True)
def controlled_time(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait(
        tasks: Collection[asyncio.Task[str | Notification | ResyncRequired]],
        _timeout: float,
    ) -> set[asyncio.Task[str | Notification | ResyncRequired]]:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return done

    monkeypatch.setattr("tinkerfin_gateway.notifications._monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "tinkerfin_gateway.notifications._now", lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    monkeypatch.setattr("tinkerfin_gateway.notifications._wait_for_changes", wait)


class _Watch:
    def __init__(self) -> None:
        self.entered = 0
        self.closed = 0
        self.reading = asyncio.Event()
        self.queue: asyncio.Queue[str | ResyncRequired | None] = asyncio.Queue(2)
        self.read_error: Exception | None = None
        self.close_error: Exception | None = None

    async def _changes(self) -> AsyncGenerator[str | ResyncRequired, None]:
        while True:
            self.reading.set()
            if self.read_error:
                raise self.read_error
            value = await self.queue.get()
            if value is None:
                return
            yield value

    @asynccontextmanager
    async def watch(self) -> AsyncGenerator[AsyncIterator[str | ResyncRequired], None]:
        self.entered += 1
        try:
            async with aclosing(self._changes()) as changes:
                yield changes
        finally:
            self.closed += 1
            if self.close_error:
                raise self.close_error


async def test_revoked_access_never_opens_a_watch_and_late_revocation_closes_it() -> (
    None
):
    source = _Watch()
    allowed = False

    async def authorize() -> bool:
        return allowed

    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        with pytest.raises(PermissionError):
            await gateway.resource_changes(
                watch_changes=source.watch, authorize=authorize
            )
        assert source.entered == 0
        allowed = True
        stream = await gateway.resource_changes(
            watch_changes=source.watch, authorize=authorize
        )
        allowed = False
        assert [frame async for frame in stream.to_sse()] == []
        assert source.closed == 1


async def test_cancelling_a_waiting_reader_closes_only_its_watch() -> None:
    first, other = _Watch(), _Watch()
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=first.watch)
        second = await gateway.resource_changes(watch_changes=other.watch)
        body = stream.to_sse()
        await anext(body)
        pull = asyncio.create_task(anext(body))
        await first.reading.wait()
        pull.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pull
        assert first.closed == 1 and other.closed == 0
        await second.aclose()


async def test_source_failure_and_cleanup_failure_remain_observable() -> None:
    source = _Watch()
    source.read_error = ValueError("source failed")
    source.close_error = RuntimeError("close failed")
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.resource_changes(watch_changes=source.watch)
        body = stream.to_sse()
        await anext(body)
        with pytest.raises(ValueError) as caught:
            await anext(body)
        assert caught.value is source.read_error
        assert source.close_error is caught.value.__cause__
        assert source.closed == 1
