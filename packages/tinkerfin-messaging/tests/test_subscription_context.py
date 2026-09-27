"""Reader contexts preserve cancellation while settling independent cleanup failures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import ClassVar

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MemoryBackend,
    MessageEnvelope,
    Messaging,
    UnexpectedMessagingBackendError,
)
from tinkerfin_messaging.backend_contract import MessagingChangeWait


class TextCodec:
    codec_id: ClassVar[str] = "context.text"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


async def test_context_cancellation_survives_pending_reader_cleanup_failure() -> None:
    waiting, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = OSError("Controlled reader cleanup failure")
    cancellation = asyncio.CancelledError("Reader context cancelled")

    class FailedReader(MemoryBackend):
        async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise failure

    async def source() -> AsyncGenerator[str, None]:
        yield "first"
        await release.wait()

    async def observe(_event: MessageEnvelope) -> None:
        committed.set()

    async with Messaging(backend=FailedReader()) as messaging:
        subscription = await messaging.channel(name="context", codec=TextCodec()).wrap(
            source(),
            identity=RunIdentity(namespace="app", thread_id="thread", run_id="run"),
            after=0,
            on_committed=observe,
        )
        await committed.wait()
        reader = aiter(subscription)
        # Consume only after the first commit, so the controlled wait belongs to
        # the pending second pull that the context must detach.
        await anext(reader)
        pending = asyncio.ensure_future(anext(reader))
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                async with subscription:
                    await waiting.wait()
                    raise cancellation
            assert caught.value is cancellation
            assert isinstance(caught.value.__cause__, UnexpectedMessagingBackendError)
            assert caught.value.__cause__.cause is failure
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
