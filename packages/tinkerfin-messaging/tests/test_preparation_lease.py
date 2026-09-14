"""Ownership remains live across all source preparation stages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import ClassVar, Generic, TypeVar

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    DeferredMessageSource,
    MemoryBackend,
    MessageSourceBinding,
    Messaging,
    MessagingClosed,
    RecoverableMessage,
    RecoveryCheckpoint,
)
from tinkerfin_messaging.backend_contract import (
    MessagingBackendSettings,
    MessagingTransition,
    MessagingTransitionResult,
)

T = TypeVar("T")


class _Source(Generic[T]):
    def __init__(self, iterator: AsyncGenerator[T, None]) -> None:
        self.iterator = iterator

    def __aiter__(self) -> AsyncIterator[T]:
        return self.iterator

    async def aclose(self) -> None:
        await self.iterator.aclose()


class _LeaseBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = 0

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        return replace(
            super().messaging_settings,
            producer_renew_interval_seconds=0.01,
            producer_lease_seconds=0.1,
        )

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        result = await super().commit_messaging_transition(transition)
        if transition.kind == "renew_producer_ownership":
            self.renewed += 1
        return result


class _TextCodec:
    codec_id: ClassVar[str] = "preparation.text"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


async def test_recoverable_opener_can_close_its_leased_messaging_owner() -> None:
    messaging = Messaging(backend=_LeaseBackend())
    await messaging.__aenter__()
    closed = asyncio.Event()

    class Source:
        def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
            async def items() -> AsyncIterator[RecoverableMessage[str]]:
                if False:
                    yield RecoverableMessage(
                        data="unused",
                        message_id="unused",
                        checkpoint=RecoveryCheckpoint(
                            position=b"", last_message_id="unused"
                        ),
                    )

            return items()

        async def aclose(self) -> None:
            closed.set()

    class Factory:
        async def open(self, checkpoint: RecoveryCheckpoint | None) -> Source:
            assert checkpoint is None
            await messaging.aclose()
            return Source()

    with pytest.raises(MessagingClosed):
        await asyncio.wait_for(
            messaging.channel(name="events", codec=_TextCodec()).wrap_recoverable(
                Factory(),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
            ),
            1,
        )
    assert closed.is_set()


async def test_cancelled_recoverable_opener_can_close_messaging_during_cleanup() -> (
    None
):
    entered = asyncio.Event()
    cleanup_closed = asyncio.Event()

    async with Messaging(backend=_LeaseBackend(), settlement_timeout=0.1) as messaging:

        class Factory:
            async def open(
                self, checkpoint: RecoveryCheckpoint | None
            ) -> _Source[RecoverableMessage[str]]:
                assert checkpoint is None
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await messaging.aclose()
                    cleanup_closed.set()
                raise AssertionError("cancelled reconstruction cannot return a source")

        operation = asyncio.create_task(
            messaging.channel(name="events", codec=_TextCodec()).wrap_recoverable(
                Factory(),
                identity=RunIdentity(
                    namespace="test", thread_id="thread", run_id="run"
                ),
            )
        )
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        assert cleanup_closed.is_set()


@pytest.mark.parametrize("recoverable", [False, True])
async def test_cancelled_preparation_retains_cleanup_failure_without_unhandled_error(
    recoverable: bool,
) -> None:
    entered = asyncio.Event()
    unhandled: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()

    def capture_unhandled(
        event_loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        assert event_loop is loop
        unhandled.append(context)

    async def prepare() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("private cleanup failure")

    class Factory:
        async def open(
            self, checkpoint: RecoveryCheckpoint | None
        ) -> _Source[RecoverableMessage[str]]:
            assert checkpoint is None
            await prepare()
            raise AssertionError("cancelled reconstruction cannot return a source")

    async def opener() -> MessageSourceBinding[str]:
        raise AssertionError("cancelled preparation must not open its source")

    loop.set_exception_handler(capture_unhandled)
    try:
        async with Messaging(backend=_LeaseBackend()) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
            operation = asyncio.create_task(
                channel.wrap_recoverable(Factory(), identity=identity)
                if recoverable
                else channel.wrap(
                    DeferredMessageSource(
                        opener, cancellable=False, on_owner_preflight=prepare
                    ),
                    identity=identity,
                )
            )
            await entered.wait()
            operation.cancel()
            with pytest.raises(asyncio.CancelledError) as captured:
                await asyncio.wait_for(operation, 1)
        await asyncio.sleep(0)
        assert unhandled == []
        assert any(
            "private cleanup failure" in note
            for note in getattr(captured.value, "__notes__", ())
        )
    finally:
        loop.set_exception_handler(original_handler)
