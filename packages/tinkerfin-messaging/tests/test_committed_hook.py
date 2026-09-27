"""Post-commit observation contracts for ordinary and cancellation messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

from tinkerfin import RunIdentity
from tinkerfin_messaging import MemoryBackend, MessageEnvelope, Messaging


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="stream-1", run_id="run-1")


class _TextCodec:
    codec_id: ClassVar[str] = "test.committed-hook-text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()

    def render(self, *, seq: int, payload: str) -> bytes:
        return f"id: {seq}\ndata: {payload}\n\n".encode()


class _TrackedSource:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
    ) -> None:
        self._items = items
        self._release = release
        self.pulled: list[str] = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self._items:
                self.pulled.append(item)
                yield item
            if self._release is not None:
                await self._release.wait()

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


async def _collect(body: AsyncIterator[bytes]) -> list[bytes]:
    return [frame async for frame in body]


async def test_on_committed_receives_authoritative_envelopes_only_from_owner() -> None:
    """A replay attachment must not re-notify commits from the owning producer."""

    backend = MemoryBackend()
    observed: list[MessageEnvelope] = []
    replay_observed: list[MessageEnvelope] = []

    async def observe(envelope: MessageEnvelope) -> None:
        observed.append(envelope)

    async def observe_replay(envelope: MessageEnvelope) -> None:
        replay_observed.append(envelope)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        body = await channel.open_sse(
            _TrackedSource("one", "two"),
            identity=_identity(),
            after=0,
            on_committed=observe,
        )
        first_frames = await _collect(body)

        unused = _TrackedSource("unused")
        replay = await channel.open_sse(
            unused,
            identity=_identity(),
            after=0,
            on_committed=observe_replay,
        )
        replay_frames = await _collect(replay)

    assert first_frames == [b"id: 1\ndata: one\n\n", b"id: 2\ndata: two\n\n"]
    assert replay_frames == first_frames
    assert [envelope.seq for envelope in observed] == [1, 2]
    assert [envelope.message_id for envelope in observed] == ["run-1:1", "run-1:2"]
    assert [envelope.payload for envelope in observed] == [b"one", b"two"]
    assert all(envelope.identity == _identity() for envelope in observed)
    assert replay_observed == []
    assert unused.close_calls == 1


async def test_blocked_on_committed_keeps_first_sse_frame_available() -> None:
    """A committed frame must remain deliverable while its observer is awaiting."""

    hook_started = asyncio.Event()
    hook_release = asyncio.Event()
    source = _TrackedSource("one", "two")

    async def observe(_envelope: MessageEnvelope) -> None:
        hook_started.set()
        await hook_release.wait()

    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        body = await channel.open_sse(
            source,
            identity=_identity(),
            after=0,
            on_committed=observe,
        )
        await hook_started.wait()

        first = await anext(body)
        assert first == b"id: 1\ndata: one\n\n"
        assert source.pulled == ["one"]

        hook_release.set()
        assert [frame async for frame in body] == [b"id: 2\ndata: two\n\n"]


async def test_on_committed_failure_is_logged_without_failing_the_run(
    caplog,
) -> None:
    """Observer failure must preserve committed replay and the completed run status."""

    async def fail(envelope: MessageEnvelope) -> None:
        raise RuntimeError(f"projection unavailable: {envelope.payload.decode()}")

    caplog.set_level(logging.ERROR, logger="tinkerfin.messaging")
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        body = await channel.open_sse(
            _TrackedSource("secret-payload"),
            identity=_identity(),
            after=0,
            on_committed=fail,
        )
        assert await _collect(body) == [b"id: 1\ndata: secret-payload\n\n"]

        replay = await channel.open_sse(
            _TrackedSource("unused"),
            identity=_identity(),
            after=0,
        )
        assert await _collect(replay) == [b"id: 1\ndata: secret-payload\n\n"]

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Messaging committed hook failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.__dict__["tinkerfin_error_type"] == "RuntimeError"
    assert record.exc_info is None
    assert "events" not in caplog.text
    assert "stream-1" not in caplog.text
    assert "run-1" not in caplog.text
    assert "projection unavailable" not in caplog.text
    assert "secret-payload" not in caplog.text


async def test_on_committed_observes_the_accepted_cancellation_tail() -> None:
    """Messages returned by an accepted cancel callback remain observable commits."""

    source_release = asyncio.Event()
    source = _TrackedSource("started", release=source_release)
    observed: list[bytes] = []

    async def observe(envelope: MessageEnvelope) -> None:
        observed.append(envelope.payload)

    async def cancel_source() -> list[str]:
        source_release.set()
        return ["cancelled-tail"]

    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        body = await channel.open_sse(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_source,
            on_committed=observe,
        )
        delivery = asyncio.create_task(_collect(body))
        await source.started.wait()
        assert await channel.cancel(identity=_identity()) is True
        frames = await delivery

    assert frames == [
        b"id: 1\ndata: started\n\n",
        b"id: 2\ndata: cancelled-tail\n\n",
    ]
    assert observed == [b"started", b"cancelled-tail"]
