"""Typed committed-event reads exposed by MessageChannel."""

from __future__ import annotations

from typing import ClassVar

import pytest
from backend_harness import MessagingBackendHarness

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    FiniteMessageSource,
    InvalidCursor,
    MessageChannel,
    Messaging,
    RunNotFound,
    StreamDeleted,
)


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


async def _commit(messaging: Messaging, *items: str) -> MessageChannel[str, str]:
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        FiniteMessageSource.from_events(items),
        identity=_identity(),
        after=0,
    )
    assert [message.data async for message in subscription] == list(items)
    return channel


async def test_channel_reads_typed_committed_pages_and_follows_one_run(
    messaging_backend: MessagingBackendHarness,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first", "second")

        assert await channel.latest_seq(identity=_identity()) == 2
        first = await channel.read(identity=_identity(), after=0, limit=1)
        second = await channel.read(identity=_identity(), after=1, limit=1000)
        following = await channel.follow(identity=_identity(), after=0)

        assert [(message.envelope.seq, message.data) for message in first] == [
            (1, "first")
        ]
        assert [(message.envelope.seq, message.data) for message in second] == [
            (2, "second")
        ]
        assert [message.data async for message in following] == ["first", "second"]


async def test_channel_empty_committed_stream_returns_zero_and_empty_page(
    messaging_backend: MessagingBackendHarness,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        empty = _identity(thread_id="empty")
        assert await channel.latest_seq(identity=empty) == 0
        assert await channel.read(identity=empty, after=0, limit=100) == ()


async def test_channel_run_status_exposes_every_durable_state(
    messaging_backend: MessagingBackendHarness,
) -> None:
    """Hosts can reconcile durable state without probing a blocking follower."""

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        missing = _identity(run_id="run-missing")
        with pytest.raises(RunNotFound):
            await channel.get_run_status(identity=missing)

        running = await messaging_backend.prepare(
            channel=channel.name,
            identity=_identity(run_id="run-running"),
            codec=_TextCodec.codec_id,
            after=0,
            cancellable=True,
            recoverable=False,
        )
        assert (
            await channel.get_run_status(identity=running.handle.identity) == "running"
        )
        await messaging_backend.finish(running.handle, status="completed")

        cancelling = await messaging_backend.prepare(
            channel=channel.name,
            identity=_identity(run_id="run-cancelling"),
            codec=_TextCodec.codec_id,
            after=0,
            cancellable=True,
            recoverable=False,
        )
        assert await messaging_backend.request_cancel(cancelling.handle) is True
        assert (
            await channel.get_run_status(identity=cancelling.handle.identity)
            == "cancel_requested"
        )
        await messaging_backend.finish(cancelling.handle, status="cancelled")

        for final_status in ("completed", "cancelled", "failed", "owner_lost"):
            prepared = await messaging_backend.prepare(
                channel=channel.name,
                identity=_identity(run_id=f"run-{final_status}"),
                codec=_TextCodec.codec_id,
                after=0,
                cancellable=True,
                recoverable=False,
            )
            failure = (
                RuntimeError(final_status)
                if final_status in {"failed", "owner_lost"}
                else None
            )
            await messaging_backend.finish(
                prepared.handle,
                status=final_status,
                error=failure,
            )
            assert (
                await channel.get_run_status(identity=prepared.handle.identity)
                == final_status
            )


async def test_channel_read_and_follow_reject_cursors_beyond_thread_tail(
    messaging_backend: MessagingBackendHarness,
) -> None:
    """Direct read and follow must enforce the same strict cursor contract."""

    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first")

        with pytest.raises(InvalidCursor) as read_error:
            await channel.read(identity=_identity(), after=999)
        with pytest.raises(InvalidCursor) as follow_error:
            await channel.follow(identity=_identity(), after=999)

        assert read_error.value.latest == 1
        assert follow_error.value.latest == 1


async def test_channel_follow_keeps_its_committed_terminal_snapshot_during_deletion(
    messaging_backend: MessagingBackendHarness,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first", "second")
        following = await channel.follow(identity=_identity(), after=0)
        iterator = aiter(following)

        assert (await anext(iterator)).data == "first"
        await channel.delete_stream(identity=_identity())

        assert (await anext(iterator)).data == "second"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)


async def test_channel_follow_binds_generation_before_first_pull(
    messaging_backend: MessagingBackendHarness,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "old")
        reader = messaging.channel(name="events", codec=_TextCodec())
        stale = await reader.follow(identity=_identity(), after=0)

        await channel.delete_stream(identity=_identity())
        replacement = await channel.wrap(
            FiniteMessageSource.from_events(("new",)),
            identity=_identity(),
            after=0,
        )
        assert [message.data async for message in replacement] == ["new"]

        with pytest.raises(StreamDeleted):
            await anext(aiter(stale))
