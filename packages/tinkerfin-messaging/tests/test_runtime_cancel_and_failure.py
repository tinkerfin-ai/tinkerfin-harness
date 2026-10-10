"""Cancellation races and producer-failure contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

import pytest
from backend_harness import MessagingBackendHarness

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    BackendOwnershipLost,
    CancelContext,
    CancellationUnsupported,
    MessageSubscription,
    Messaging,
    RunProducerFailed,
)


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="conversation-1", run_id="run-1")


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        if item == "encode-failure":
            raise ValueError("cannot encode payload")
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _CancellableSource:
    def __init__(
        self,
        *,
        release: asyncio.Event,
        before: tuple[str, ...] = ("started",),
        after: tuple[str, ...] = (),
    ) -> None:
        self.release = release
        self.before = before
        self.after = after
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for value in self.before:
                yield value
            await self.release.wait()
            for value in self.after:
                yield value

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


class _JoinOnCloseSource:
    """Model a source whose close waits for its active consumer to settle."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.force_join_return = asyncio.Event()
        self._consumer: asyncio.Task[object] | None = None
        self._closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self._consumer = asyncio.current_task()
            self.started.set()
            yield "started"
            await asyncio.Event().wait()

        return iterate()

    async def aclose(self) -> None:
        if self._closed:
            return
        consumer = self._consumer
        current = asyncio.current_task()
        if consumer is not None and consumer is not current and not consumer.done():
            consumer.cancel()
            forced = asyncio.create_task(self.force_join_return.wait())
            try:
                while not consumer.done() and not forced.done():
                    try:
                        await asyncio.wait(
                            {consumer, forced},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except asyncio.CancelledError:
                        continue
            finally:
                if not forced.done():
                    forced.cancel()
                await asyncio.gather(forced, return_exceptions=True)
        self._closed = True
        self.closed.set()


class _AbortableSource:
    """Cancel its active consumer and return protocol-neutral tail values."""

    def __init__(
        self,
        *,
        before: tuple[str, ...] = ("started",),
        tail: tuple[str, ...] = ("cancelled-tail",),
    ) -> None:
        self.before = before
        self.tail = tail
        self.started = asyncio.Event()
        self.aborted = asyncio.Event()
        self.close_calls = 0
        self.abort_calls = 0
        self._consumer: asyncio.Task[object] | None = None
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            consumer = asyncio.current_task()
            if consumer is None:
                raise RuntimeError("test source requires an asyncio task")
            self._consumer = consumer
            self.started.set()
            for value in self.before:
                yield value
            await asyncio.Event().wait()

        self._iterator = iterate()
        return self._iterator

    def abort(self) -> tuple[str, ...]:
        self.abort_calls += 1
        consumer = self._consumer
        if consumer is not None and not consumer.done():
            consumer.cancel()
        self.aborted.set()
        return self.tail

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def test_cancel_invokes_callback_once_and_commits_callback_output(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(
        release=release,
        after=("cancelled-payload",),
    )
    cancel_calls = 0

    async def cancel_run() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        release.set()

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        await source.started.wait()

        cancelled = await channel.cancel(identity=_identity())

        assert cancelled is True
        assert await _data(subscription) == ["started", "cancelled-payload"]

    assert cancel_calls == 1
    assert source.close_calls == 1


async def test_cancel_callback_receives_the_owned_run_context(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)
    received: list[CancelContext] = []

    async def cancel_run(context: CancelContext) -> tuple[str, ...]:
        received.append(context)
        release.set()
        return ("context-cancelled-tail",)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        await source.started.wait()

        assert await channel.cancel(identity=_identity()) is True
        assert await _data(subscription) == ["started", "context-cancelled-tail"]

    assert received == [CancelContext(channel="events", identity=_identity())]


async def test_cancel_callback_prefers_the_compatible_zero_argument_shape(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)
    received: list[CancelContext | None] = []

    def cancel_run(context: CancelContext | None = None) -> tuple[str, ...]:
        received.append(context)
        release.set()
        return ("ambiguous-cancelled-tail",)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )

        assert await channel.cancel(identity=_identity()) is True
        assert await _data(subscription) == ["started", "ambiguous-cancelled-tail"]

    assert received == [None]


async def test_cancel_callback_type_error_is_not_retried_without_context(
    messaging_backend: MessagingBackendHarness,
) -> None:
    source = _AbortableSource()
    calls = 0

    def cancel_run(_context: CancelContext) -> None:
        nonlocal calls
        calls += 1
        raise TypeError("cancel callback body failed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "started"

        with pytest.raises(RunProducerFailed) as captured:
            await channel.cancel(identity=_identity())
        with pytest.raises(RunProducerFailed):
            await anext(delivery)

    assert calls == 1
    assert captured.value.cause is not None
    assert "cancel callback body failed" in str(captured.value.cause)


async def test_sync_cancel_callback_can_return_tail_messages(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)

    def cancel_run() -> tuple[str, ...]:
        release.set()
        return ("sync-cancelled-tail",)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        await source.started.wait()

        assert await channel.cancel(identity=_identity()) is True
        assert await _data(subscription) == ["started", "sync-cancelled-tail"]


async def test_concurrent_cancel_callers_share_one_callback_and_settlement(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)
    cancel_calls = 0
    callback_started, release_callback = asyncio.Event(), asyncio.Event()
    caller_started = (asyncio.Event(), asyncio.Event())

    async def cancel_run() -> tuple[str, ...]:
        nonlocal cancel_calls
        cancel_calls += 1
        callback_started.set()
        await release_callback.wait()
        release.set()
        return ("async-cancelled-tail",)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )

        async def cancel(index: int) -> bool:
            caller_started[index].set()
            return await channel.cancel(identity=_identity())

        callers = tuple(asyncio.create_task(cancel(index)) for index in range(2))
        try:
            await asyncio.gather(
                *(started.wait() for started in caller_started), callback_started.wait()
            )
        finally:
            release_callback.set()
            results = await asyncio.gather(*callers)
        assert await _data(subscription) == ["started", "async-cancelled-tail"]

    assert sorted(results) == [False, True]
    assert cancel_calls == 1


async def test_cancel_without_callback_is_rejected_without_stopping_source(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        await source.started.wait()

        with pytest.raises(CancellationUnsupported):
            await channel.cancel(identity=_identity())
        assert not source.closed.is_set()

        release.set()
        await _data(subscription)


async def test_cancel_after_natural_completion_returns_false(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    release.set()
    source = _CancellableSource(release=release)

    async def cancel_run() -> None:
        pytest.fail("completed run must not invoke its cancellation callback")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        assert await _data(subscription) == ["started"]

        assert await channel.cancel(identity=_identity()) is False


async def test_backend_rejects_a_second_cancel_settlement_claim(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await messaging_backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.text.v1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    assert await messaging_backend.request_cancel(prepared.handle) is True
    assert await messaging_backend.begin_settlement(prepared.handle) is True
    try:
        await messaging_backend.begin_settlement(prepared.handle)
    except BackendOwnershipLost:
        rejected = True
    else:
        rejected = False
    await messaging_backend.finish(prepared.handle, status="cancelled")

    assert rejected


async def test_cancel_callback_failure_becomes_the_run_failure(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)
    cause = RuntimeError("cancel integration failed")

    async def cancel_run() -> None:
        release.set()
        raise cause

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )

        with pytest.raises(RunProducerFailed) as captured:
            await channel.cancel(identity=_identity())

        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "started"
        with pytest.raises(RunProducerFailed):
            await anext(delivery)

    assert captured.value.cause is not None
    assert str(cause) in str(captured.value.cause)


async def test_cancel_tail_codec_failure_preserves_the_committed_prefix(
    messaging_backend: MessagingBackendHarness,
) -> None:
    source = _AbortableSource(
        before=("committed",),
        tail=("encode-failure",),
    )

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=source.abort,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "committed"

        with pytest.raises(RunProducerFailed) as captured:
            await channel.cancel(identity=_identity())
        with pytest.raises(RunProducerFailed):
            await anext(delivery)

    assert captured.value.cause is not None
    assert "cannot encode payload" in str(captured.value.cause)


async def test_cancel_callback_can_join_the_source_consumer_without_deadlock(
    messaging_backend: MessagingBackendHarness,
) -> None:
    source = _JoinOnCloseSource()

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=source.aclose,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "started"
        await source.started.wait()

        cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
        try:
            # A source that joins its consumer must close without a forced release.
            # Database round-trip latency is unrelated to this ownership guarantee.
            await source.closed.wait()
            assert not source.force_join_return.is_set()
            assert await cancelling is True
        finally:
            source.force_join_return.set()
            await asyncio.gather(cancelling, return_exceptions=True)

    assert source.closed.is_set()


async def test_codec_failure_preserves_the_committed_prefix(
    messaging_backend: MessagingBackendHarness,
) -> None:
    release = asyncio.Event()
    release.set()
    source = _CancellableSource(
        release=release,
        before=("committed", "encode-failure"),
    )

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        delivery = aiter(subscription)

        assert (await anext(delivery)).data == "committed"
        with pytest.raises(RunProducerFailed) as captured:
            await anext(delivery)

    assert captured.value.cause is not None
    assert "cannot encode payload" in str(captured.value.cause)
    assert source.close_calls == 1
