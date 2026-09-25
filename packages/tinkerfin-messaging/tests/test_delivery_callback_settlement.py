"""Delivery callbacks complete without callers supplying cancellation wrappers."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    FiniteMessageSource,
    Messaging,
    RecoverableMessage,
    RecoveryCheckpoint,
)
from tinkerfin_messaging.errors import MessagingClosed


class _TextCodec:
    codec_id: ClassVar[str] = "test.delivery-settlement"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


@pytest.mark.parametrize(
    "boundary",
    [
        "closed",
        "invalid",
        "resolver",
        "ready",
        "recoverable_invalid",
        "recoverable_ready",
    ],
)
@pytest.mark.parametrize("callback_fails", [False, True])
async def test_delivery_callback_settles_before_repeated_caller_cancellation(
    boundary: str, callback_fails: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    finished = False
    failure = OSError("callback cleanup failed")

    async def callback() -> None:
        nonlocal finished
        entered.set()
        await release.wait()
        finished = True
        if callback_fails:
            raise failure

    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    if boundary == "closed":
        await messaging.aclose()
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    source = FiniteMessageSource(("item",))

    def cursor() -> int:
        raise ValueError("cursor rejected")

    async def deliver() -> None:
        if boundary.startswith("recoverable"):

            class Factory:
                async def open(
                    self, checkpoint: RecoveryCheckpoint | None
                ) -> FiniteMessageSource[RecoverableMessage[str]]:
                    return FiniteMessageSource(
                        (
                            RecoverableMessage(
                                message_id="message",
                                data="item",
                                checkpoint=RecoveryCheckpoint(
                                    position=b"1", last_message_id="message"
                                ),
                            ),
                        )
                    )

            subscription = await channel.wrap_recoverable(
                Factory(),
                identity=identity,
                after=-1 if boundary == "recoverable_invalid" else None,
                on_source_ready=callback if boundary == "recoverable_ready" else None,
                on_delivery_not_started=callback
                if boundary == "recoverable_invalid"
                else None,
            )
            await subscription.aclose()
        elif boundary == "resolver":
            body = await channel.open_sse(
                source,
                identity=identity,
                after=cursor,
                on_delivery_not_started=callback,
            )
            await body.aclose()
        else:
            subscription = await channel.wrap(
                source,
                identity=identity,
                after=-1 if boundary == "invalid" else None,
                on_source_ready=callback if boundary == "ready" else None,
                on_delivery_not_started=None if boundary == "ready" else callback,
            )
            await subscription.aclose()

    request = asyncio.create_task(deliver())
    await entered.wait()
    request.cancel("first cancellation")
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()
    request.cancel("repeated cancellation")
    premature = request.done()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await request
        assert not premature
        assert finished
        if callback_fails:
            pending: list[BaseException] = [cancelled.value]
            seen: set[int] = set()
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                pending.extend(
                    item
                    for item in (error.__cause__, error.__context__)
                    if item is not None
                )
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
            assert id(failure) in seen
    finally:
        await messaging.aclose()


async def test_rejected_delivery_finishes_source_cleanup_before_caller_cancellation() -> (
    None
):
    entered, release = asyncio.Event(), asyncio.Event()
    closed = False

    class Source(FiniteMessageSource[str]):
        async def aclose(self) -> None:
            nonlocal closed
            entered.set()
            await release.wait()
            await super().aclose()
            closed = True

    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        request = asyncio.create_task(
            channel.wrap(
                Source(("item",)),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
                after=-1,
            )
        )
        await entered.wait()
        request.cancel()
        delivered = asyncio.Event()
        asyncio.get_running_loop().call_soon(delivered.set)
        await delivered.wait()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert closed


@pytest.mark.parametrize("external_first", [False, True])
@pytest.mark.parametrize("boundary", ["ready", "subscribed"])
async def test_callback_close_and_external_close_wait_for_their_owned_work(
    external_first: bool,
    boundary: str,
) -> None:
    entered, close_from_callback, callback_stopped, finish_callback = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    messaging = Messaging(settlement_timeout=None)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())

    async def ready() -> None:
        entered.set()
        await close_from_callback.wait()
        await messaging.aclose()
        callback_stopped.set()
        await finish_callback.wait()

    async def deliver() -> None:
        with pytest.raises(MessagingClosed):
            await channel.open_sse(
                FiniteMessageSource(("item",)),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
                on_source_ready=ready if boundary == "ready" else None,
                on_subscribed=ready if boundary == "subscribed" else None,
            )

    external_entered = asyncio.Event()

    async def close_external() -> None:
        external_entered.set()
        await messaging.aclose()

    delivery = asyncio.create_task(deliver())
    await entered.wait()
    external: asyncio.Task[None] | None = None
    if external_first:
        external = asyncio.create_task(close_external())
        await external_entered.wait()
    close_from_callback.set()
    await callback_stopped.wait()
    if not external_first:
        external = asyncio.create_task(close_external())
        await external_entered.wait()
    assert external is not None
    try:
        assert not external.done()
        assert not delivery.done()
    finally:
        finish_callback.set()
        await delivery
        await external
    await messaging.aclose()
