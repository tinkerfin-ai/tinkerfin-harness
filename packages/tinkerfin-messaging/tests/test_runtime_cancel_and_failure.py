"""Cancellation races and producer-failure contracts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import ClassVar, cast

import pytest
from backend_harness import MessagingBackendHarness

import tinkerfin_messaging.sources as source_adapters
from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    BackendOwnershipLost,
    CancelCallback,
    CancelContext,
    CancellationUnsupported,
    MemoryBackend,
    MessageSubscription,
    Messaging,
    RunNotFound,
    RunProducerFailed,
    UnexpectedMessagingBackendError,
)
from tinkerfin_messaging.backend_contract import (
    MessagingTransition,
    MessagingTransitionResult,
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


class _SourceAndCloseFailure:
    def __init__(
        self,
        *,
        source_error: BaseException,
        close_error: BaseException,
    ) -> None:
        self.source_error = source_error
        self.close_error = close_error
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            if False:
                yield "unreachable"
            raise self.source_error

        return iterate()

    async def aclose(self) -> None:
        self.close_calls += 1
        raise self.close_error


class _CloseFailingAbortableSource(_AbortableSource):
    def __init__(self, *, close_error: BaseException) -> None:
        super().__init__()
        self.close_error = close_error

    async def aclose(self) -> None:
        await super().aclose()
        raise self.close_error


class _ControlledAppendBackend(MemoryBackend):
    def __init__(
        self,
        *,
        blocked_payload: bytes | None = None,
        failed_payload: bytes | None = None,
    ) -> None:
        super().__init__()
        self.blocked_payload = blocked_payload
        self.failed_payload = failed_payload
        self.append_started = asyncio.Event()
        self.release_append = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if (
            transition.kind == "append_message"
            and transition.payload == self.blocked_payload
        ):
            self.append_started.set()
            await self.release_append.wait()
        if (
            transition.kind == "append_message"
            and transition.payload == self.failed_payload
        ):
            raise RuntimeError("cannot append payload")
        return await super().commit_messaging_transition(transition)


async def test_channel_uses_deferred_source_owned_cancel_callback() -> None:
    """A source-declared callback must make the durable producer cancellable."""

    opened = _AbortableSource()
    open_calls = 0

    async def open_source():
        nonlocal open_calls
        open_calls += 1
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=opened.abort,
        )

    deferred = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            deferred,
            identity=_identity(),
            after=0,
        )
        await opened.started.wait()

        assert await channel.cancel(identity=_identity()) is True
        assert [message.data async for message in subscription] == [
            "started",
            "cancelled-tail",
        ]

    assert open_calls == 1
    assert opened.abort_calls == 1
    assert opened.close_calls == 1


@pytest.mark.parametrize("mapped", [False, True])
async def test_channel_reuses_equivalent_source_owned_cancel_callback(
    mapped: bool,
) -> None:
    """The same owner may be supplied explicitly without creating a second owner."""

    opened = _AbortableSource()

    async def open_source():
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=opened.abort,
        )

    deferred = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    source = source_adapters.map_source(deferred, str.upper) if mapped else deferred
    expected = (
        ["STARTED", "CANCELLED-TAIL"]
        if mapped
        else [
            "started",
            "cancelled-tail",
        ]
    )

    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
            cancel=deferred.cancel,
        )
        await opened.started.wait()

        assert await channel.cancel(identity=_identity()) is True
        assert [message.data async for message in subscription] == expected

    assert opened.abort_calls == 1


async def test_source_owned_and_explicit_cancel_are_rejected_before_claim() -> None:
    """One producer must never have two competing cancellation owners."""

    open_calls = 0

    async def open_source():
        nonlocal open_calls
        open_calls += 1
        opened = _AbortableSource()
        return source_adapters.MessageSourceBinding(
            source=opened,
            cancel=opened.abort,
        )

    deferred = source_adapters.DeferredMessageSource(
        open_source,
        cancellable=True,
    )
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(TypeError, match="source owns cancellation"):
            await channel.wrap(
                deferred,
                identity=_identity(),
                after=0,
                cancel=lambda: (),
            )
        released = asyncio.Event()
        released.set()
        replacement = await channel.wrap(
            _CancellableSource(release=released, before=()),
            identity=_identity(),
            after=0,
        )
        assert [message.data async for message in replacement] == []

    assert open_calls == 0


class _FinishOwnershipLostBackend(MemoryBackend):
    def __init__(self, *, append_error: BaseException | None = None) -> None:
        super().__init__()
        self.append_error = append_error
        self.append_started = asyncio.Event()
        self.release_append = asyncio.Event()
        self.finish_started = asyncio.Event()
        self.release_finish = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "append_message" and self.append_error is not None:
            self.append_started.set()
            await self.release_append.wait()
            raise self.append_error
        if transition.kind == "finish_run":
            self.finish_started.set()
            await self.release_finish.wait()
            raise BackendOwnershipLost("Producer lost ownership during finish")
        return await super().commit_messaging_transition(transition)


def _active_producer(run: str) -> asyncio.Task[None]:
    name = f"tinkerfin-messaging-producer:{run}"
    return cast(
        asyncio.Task[None],
        next(task for task in asyncio.all_tasks() if task.get_name() == name),
    )


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


@pytest.mark.parametrize(
    "cancel_run",
    (
        pytest.param(lambda _context, _extra: None, id="two-required-arguments"),
        pytest.param(lambda *, context: context, id="required-keyword-argument"),
    ),
)
async def test_invalid_cancel_signature_is_rejected_before_claiming_the_run(
    cancel_run: Callable[..., object],
) -> None:
    release = asyncio.Event()
    source = _CancellableSource(release=release)

    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(
            TypeError,
            match="must accept no arguments or one positional CancelContext",
        ):
            await channel.wrap(
                source,
                identity=_identity(),
                after=0,
                cancel=cast(CancelCallback[str], cancel_run),
            )
        with pytest.raises(RunNotFound):
            await channel.cancel(identity=_identity())

    assert source.closed.is_set()


async def test_uninspectable_cancel_callback_is_rejected_before_claiming_the_run() -> (
    None
):
    class OpaqueCancel:
        __signature__ = "opaque"

        def __call__(self) -> None:
            raise AssertionError("an uninspectable callback must not run")

    release = asyncio.Event()
    source = _CancellableSource(release=release)

    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(TypeError, match="must expose an inspectable signature"):
            await channel.wrap(
                source,
                identity=_identity(),
                after=0,
                cancel=OpaqueCancel(),
            )
        with pytest.raises(RunNotFound):
            await channel.cancel(identity=_identity())

    assert source.closed.is_set()


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


async def test_cancel_commits_an_in_flight_message_before_callback_tail() -> None:
    backend = _ControlledAppendBackend(blocked_payload=b"in-flight")
    source = _AbortableSource(before=("in-flight",))
    cancelling: asyncio.Task[bool] | None = None

    try:
        async with Messaging(backend=backend) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            subscription = await channel.wrap(
                source,
                identity=_identity(),
                after=0,
                cancel=source.abort,
            )
            await backend.append_started.wait()

            cancelling = asyncio.create_task(channel.cancel(identity=_identity()))
            await source.aborted.wait()
            await asyncio.sleep(0)
            assert not cancelling.done()

            backend.release_append.set()
            assert await cancelling is True
            assert await _data(subscription) == ["in-flight", "cancelled-tail"]
    finally:
        backend.release_append.set()
        if cancelling is not None:
            await asyncio.gather(cancelling, return_exceptions=True)

    assert source.abort_calls == 1
    assert source.close_calls == 1


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


async def test_cancel_tail_append_failure_preserves_the_committed_prefix() -> None:
    backend = _ControlledAppendBackend(failed_payload=b"append-failure")
    source = _AbortableSource(
        before=("committed",),
        tail=("append-failure",),
    )

    async with Messaging(backend=backend) as messaging:
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

    assert isinstance(captured.value.cause, UnexpectedMessagingBackendError)
    assert "cannot append payload" in str(captured.value.cause.cause)


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


async def test_commit_failure_remains_primary_when_finish_loses_ownership(
    caplog: pytest.LogCaptureFixture,
) -> None:
    append_error = ValueError("append failed")
    backend = _FinishOwnershipLostBackend(append_error=append_error)
    source = _CancellableSource(
        release=asyncio.Event(),
        before=("uncommitted",),
    )
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    try:
        await messaging.channel(name="events", codec=_TextCodec()).wrap(
            source,
            identity=_identity(),
            after=0,
        )
        await backend.append_started.wait()
        producer = _active_producer("run-1")

        with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
            backend.release_append.set()
            await backend.finish_started.wait()
            backend.release_finish.set()
            with pytest.raises(UnexpectedMessagingBackendError) as captured:
                await producer

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        records = [
            record
            for record in caplog.records
            if getattr(record, "tinkerfin_secondary_stage", None) == "finish"
        ]
        assert captured.value.cause is append_error
        assert "BackendOwnershipLost" in notes
        assert "finish" in notes
        assert len(records) == 1
        record_fields = vars(records[0])
        assert record_fields["tinkerfin_primary_stage"] == "commit"
        assert record_fields["tinkerfin_ownership_lost"] is True
        assert "channel" not in record_fields
        assert "thread_id" not in record_fields
        assert "run_id" not in record_fields
    finally:
        backend.release_append.set()
        backend.release_finish.set()
        await messaging.__aexit__(None, None, None)


async def test_finish_ownership_loss_is_primary_without_an_earlier_failure() -> None:
    backend = _FinishOwnershipLostBackend()
    release = asyncio.Event()
    release.set()
    source = _CancellableSource(release=release, before=("committed",))
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    try:
        await messaging.channel(name="events", codec=_TextCodec()).wrap(
            source,
            identity=_identity(),
            after=0,
        )
        await backend.finish_started.wait()
        producer = _active_producer("run-1")
        backend.release_finish.set()

        with pytest.raises(BackendOwnershipLost):
            await producer
    finally:
        backend.release_finish.set()
        await messaging.__aexit__(None, None, None)


async def test_source_failure_retains_later_source_close_failure() -> None:
    source_error = ValueError("source failed")
    source = _SourceAndCloseFailure(
        source_error=source_error,
        close_error=RuntimeError("source close failed"),
    )

    async with Messaging() as messaging:
        subscription = await messaging.channel(
            name="events",
            codec=_TextCodec(),
        ).wrap(
            source,
            identity=_identity(),
            after=0,
        )
        with pytest.raises(RunProducerFailed) as captured:
            await anext(aiter(subscription))

    cause = captured.value.cause
    assert cause is source_error
    notes = "\n".join(getattr(cause, "__notes__", ()))
    assert "source_close" in notes
    assert "RuntimeError: source close failed" in notes
    assert source.close_calls == 1


async def test_callback_failure_retains_later_source_close_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    callback_error = ValueError("callback failed")
    source = _CloseFailingAbortableSource(
        close_error=RuntimeError("source close failed")
    )

    def cancel() -> None:
        source.abort()
        raise callback_error

    with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
        async with Messaging() as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            subscription = await channel.wrap(
                source,
                identity=_identity(),
                after=0,
                cancel=cancel,
            )
            delivery = aiter(subscription)
            assert (await anext(delivery)).data == "started"
            with pytest.raises(RunProducerFailed) as captured:
                await channel.cancel(identity=_identity())

    cause = captured.value.cause
    assert cause is callback_error
    notes = "\n".join(getattr(cause, "__notes__", ()))
    records = [
        vars(record)
        for record in caplog.records
        if getattr(record, "tinkerfin_secondary_stage", None) == "source_close"
    ]
    assert "source_close" in notes
    assert "RuntimeError: source close failed" in notes
    assert len(records) == 1
    assert records[0]["tinkerfin_primary_stage"] == "callback"
    assert records[0]["tinkerfin_ownership_lost"] is False
    assert source.close_calls == 1
