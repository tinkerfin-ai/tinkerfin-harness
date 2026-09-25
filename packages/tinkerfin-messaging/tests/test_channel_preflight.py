"""Async preflight and source-ownership contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import ClassVar, cast

import pytest

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    InvalidCursor,
    MessageSource,
    MessageSubscription,
    Messaging,
    MessagingClosed,
    RecoverableMessage,
    RecoveryCheckpoint,
    RunAlreadyActive,
    SseRenderingUnsupported,
)
from tinkerfin_messaging.backend_contract import MessagingBackend


def _identity(
    *,
    thread_id: str = "conversation-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


@dataclass(frozen=True, slots=True)
class _TextSseRenderer:
    def render(self, *, seq: int, payload: str) -> bytes:
        return f"id: {seq}\ndata: {payload}\n\n".encode()


class _ControlledSource:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
    ) -> None:
        self._items = items
        self._release = release
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        if self._iterator is not None:
            raise RuntimeError("source is single-use")

        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self._items:
                yield item
            if self._release is not None:
                await self._release.wait()

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        if self._release is not None:
            self._release.set()
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


class _NeverOpenedRecoverable:
    def __init__(self) -> None:
        self.open_calls = 0

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> MessageSource[RecoverableMessage[str]]:
        del checkpoint
        self.open_calls += 1
        raise AssertionError("closed Messaging opened a recoverable source")


class _FailingCloseSource(_ControlledSource):
    def __init__(self, error: BaseException) -> None:
        super().__init__("never-consumed")
        self._error = error

    async def aclose(self) -> None:
        await super().aclose()
        raise self._error


class _ClosingRecoverable:
    def __init__(self, messaging: Messaging, close_error: BaseException) -> None:
        self._messaging = messaging
        self.opened = _FailingCloseSource(close_error)
        self.open_calls = 0

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> MessageSource[RecoverableMessage[str]]:
        del checkpoint
        self.open_calls += 1
        await self._messaging.aclose()
        return cast(MessageSource[RecoverableMessage[str]], self.opened)


async def _collect_data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


@pytest.mark.parametrize("render_sse", [False, True])
async def test_closed_messaging_settles_an_unregistered_ordinary_delivery(
    render_sse: bool,
) -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(
        name="events",
        codec=_TextCodec(),
        renderer=_TextSseRenderer(),
    )
    await messaging.aclose()
    source = _ControlledSource("never-consumed")
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        assert source.closed.is_set()
        not_started += 1

    with pytest.raises(MessagingClosed):
        if render_sse:
            await channel.open_sse(
                source,
                identity=_identity(),
                on_delivery_not_started=delivery_not_started,
            )
        else:
            await channel.wrap(
                source,
                identity=_identity(),
                on_delivery_not_started=delivery_not_started,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()
    assert not_started == 1


async def test_closed_messaging_does_not_open_a_recoverable_delivery() -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await messaging.aclose()
    source = _NeverOpenedRecoverable()
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        not_started += 1

    with pytest.raises(MessagingClosed):
        await channel.wrap_recoverable(
            source,
            identity=_identity(),
            on_delivery_not_started=delivery_not_started,
        )

    assert source.open_calls == 0
    assert not_started == 1


async def test_closed_messaging_propagates_source_cleanup_cancellation() -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await messaging.aclose()
    source = _FailingCloseSource(asyncio.CancelledError("cleanup cancelled"))
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        not_started += 1

    with pytest.raises(asyncio.CancelledError) as captured:
        await channel.wrap(
            source,
            identity=_identity(),
            on_delivery_not_started=delivery_not_started,
        )

    assert source.close_calls == 1
    assert not_started == 1
    assert any(
        "MessagingClosed" in note for note in getattr(captured.value, "__notes__", ())
    )


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
async def test_registered_wrap_propagates_cleanup_process_control(
    error_type: type[BaseException],
) -> None:
    source = _FailingCloseSource(error_type("cleanup stopped"))
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        not_started += 1

    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(error_type) as captured:
            await channel.wrap(
                source,
                identity=_identity(),
                after=True,
                on_delivery_not_started=delivery_not_started,
            )

    assert source.close_calls == 1
    assert not_started == 1
    assert any("TypeError" in note for note in getattr(captured.value, "__notes__", ()))


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
async def test_sse_cursor_failure_propagates_cleanup_process_control(
    error_type: type[BaseException],
) -> None:
    source = _FailingCloseSource(error_type("cleanup stopped"))
    not_started = 0

    def resolve_after() -> int:
        raise RuntimeError("cursor lookup failed")

    async def delivery_not_started() -> None:
        nonlocal not_started
        not_started += 1

    async with Messaging() as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        with pytest.raises(error_type) as captured:
            await channel.open_sse(
                source,
                identity=_identity(),
                after=resolve_after,
                on_delivery_not_started=delivery_not_started,
            )

    assert source.close_calls == 1
    assert not_started == 1
    assert any(
        "RuntimeError" in note for note in getattr(captured.value, "__notes__", ())
    )


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
async def test_recoverable_preflight_propagates_opened_cleanup_process_control(
    error_type: type[BaseException],
) -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    source = _ClosingRecoverable(messaging, error_type("cleanup stopped"))
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        not_started += 1

    try:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(error_type) as captured:
            await channel.wrap_recoverable(
                source,
                identity=_identity(),
                on_delivery_not_started=delivery_not_started,
            )
    finally:
        await messaging.aclose()

    assert source.open_calls == 1
    assert source.opened.close_calls == 1
    assert not_started == 1
    assert any(
        "MessagingClosed" in note for note in getattr(captured.value, "__notes__", ())
    )


async def test_close_waits_for_preflight_not_the_callers_later_work() -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    source = _ControlledSource("unused")
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    request_continued = asyncio.Event()
    release_request = asyncio.Event()

    async def source_ready() -> None:
        callback_started.set()
        await release_callback.wait()

    async def request() -> None:
        with pytest.raises(MessagingClosed):
            await channel.wrap(
                source,
                identity=_identity(),
                on_source_ready=source_ready,
            )
        request_continued.set()
        await release_request.wait()

    request_task = asyncio.create_task(request())
    await callback_started.wait()
    close_task = asyncio.create_task(messaging.aclose())
    await asyncio.sleep(0)
    release_callback.set()
    await request_continued.wait()

    await asyncio.wait_for(asyncio.shield(close_task), timeout=1)
    assert not request_task.done()

    release_request.set()
    await request_task


async def test_source_ready_can_close_messaging_without_preflight_deadlock() -> None:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    source = _ControlledSource("unused")

    async def source_ready() -> None:
        await messaging.aclose()

    with pytest.raises(MessagingClosed):
        await asyncio.wait_for(
            channel.wrap(
                source,
                identity=_identity(),
                on_source_ready=source_ready,
            ),
            timeout=1,
        )

    assert source.close_calls == 1


async def test_invalid_cursor_is_rejected_during_wrap_and_closes_source(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")
    not_started = 0

    async def delivery_not_started() -> None:
        nonlocal not_started
        assert source.closed.is_set()
        not_started += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(InvalidCursor) as captured:
            await channel.wrap(
                source,
                identity=_identity(),
                after=1,
                on_delivery_not_started=delivery_not_started,
            )

    assert captured.value.after == 1
    assert captured.value.latest == 0
    assert source.close_calls == 1
    assert not source.started.is_set()
    assert not_started == 1


async def test_boolean_cursor_is_rejected_during_wrap_and_closes_source(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(TypeError, match="after must be an integer or None"):
            await channel.wrap(
                source,
                identity=_identity(),
                after=True,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_sse_requires_a_renderer_without_affecting_async_replay(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )

        with pytest.raises(SseRenderingUnsupported, match="no SSE renderer"):
            subscription.to_sse()
        assert await _collect_data(subscription) == ["message"]


async def test_validate_cursor_checks_tail_without_claiming_a_run(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        await channel.validate_cursor(identity=_identity(), after=0)
        with pytest.raises(InvalidCursor) as captured:
            await channel.validate_cursor(identity=_identity(), after=1)
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )
        assert await _collect_data(subscription) == ["message"]

    assert captured.value.after == 1
    assert captured.value.latest == 0
    assert source.close_calls == 1


def test_overlong_identity_is_rejected_before_source_ownership() -> None:
    source = _ControlledSource("never-consumed")

    with pytest.raises(ValueError, match="at most 1024"):
        _identity(thread_id="x" * 1025)

    assert source.close_calls == 0
    assert not source.started.is_set()


async def test_same_run_attaches_and_closes_the_unused_candidate_source(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    owner = _ControlledSource("first", release=release)
    candidate = _ControlledSource("must-not-run")
    candidate_ready = 0
    candidate_not_started = 0

    async def candidate_source_ready() -> None:
        nonlocal candidate_ready
        candidate_ready += 1

    async def candidate_delivery_not_started() -> None:
        nonlocal candidate_not_started
        candidate_not_started += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            owner,
            identity=_identity(),
            after=0,
        )
        await asyncio.wait_for(owner.started.wait(), timeout=1)

        second = await channel.wrap(
            candidate,
            identity=_identity(),
            after=0,
            on_source_ready=candidate_source_ready,
            on_delivery_not_started=candidate_delivery_not_started,
        )

        release.set()
        first_data, second_data = await asyncio.gather(
            _collect_data(first),
            _collect_data(second),
        )

    assert first_data == ["first"]
    assert second_data == ["first"]
    assert candidate.close_calls == 1
    assert not candidate.started.is_set()
    assert owner.close_calls == 1
    assert candidate_ready == 0
    assert candidate_not_started == 0


async def test_owner_callbacks_run_after_source_preflight_and_before_production(
    messaging_backend: MessagingBackend,
) -> None:
    order: list[str] = []

    class _SourceWithPreflight(_ControlledSource):
        async def messaging_owner_preflight(self) -> None:
            order.append("source_preflight")

        def __aiter__(self) -> AsyncIterator[str]:
            order.append("source_pull")
            return super().__aiter__()

    source = _SourceWithPreflight("message")

    async def source_ready() -> None:
        order.append("host_ready")

    async def delivery_not_started() -> None:
        order.append("not_started")

    async with Messaging(backend=messaging_backend) as messaging:
        subscription = await messaging.channel(
            name="events",
            codec=_TextCodec(),
        ).wrap(
            source,
            identity=_identity(),
            after=0,
            on_source_ready=source_ready,
            on_delivery_not_started=delivery_not_started,
        )
        assert await _collect_data(subscription) == ["message"]

    assert order[:2] == ["source_preflight", "host_ready"]
    assert order.count("source_pull") == 1
    assert "not_started" not in order


async def test_source_ready_failure_retains_the_started_delivery(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("must-not-run")
    order: list[str] = []

    async def source_ready() -> None:
        order.append("source_ready")
        raise RuntimeError("business activation failed")

    async def delivery_not_started() -> None:
        assert source.closed.is_set()
        order.append("not_started")

    async with Messaging(backend=messaging_backend) as messaging:
        with pytest.raises(RuntimeError, match="business activation failed"):
            await messaging.channel(name="events", codec=_TextCodec()).wrap(
                source,
                identity=_identity(),
                after=0,
                on_source_ready=source_ready,
                on_delivery_not_started=delivery_not_started,
            )

    assert order == ["source_ready"]
    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_wrap_rejects_an_active_run_conflict_before_returning(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    owner = _ControlledSource("first", release=release)
    active_conflict = _ControlledSource("active-conflict")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            owner,
            identity=_identity(),
            after=0,
        )
        await asyncio.wait_for(owner.started.wait(), timeout=1)

        with pytest.raises(RunAlreadyActive):
            await channel.wrap(
                active_conflict,
                identity=_identity(run_id="run-2"),
                after=0,
            )

        release.set()
        await _collect_data(subscription)

    assert active_conflict.close_calls == 1
    assert not active_conflict.started.is_set()


async def test_sse_renderer_uses_the_durable_sequence(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("one", "two")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        subscription = await channel.wrap(
            source,
            identity=_identity(),
            after=0,
        )

        frames = [frame async for frame in subscription.to_sse()]

    assert frames == [
        b"id: 1\ndata: one\n\n",
        b"id: 2\ndata: two\n\n",
    ]


async def test_channel_sse_resolves_cursor_callback_once(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("one", "two")
    resolver_calls = 0

    def resolve_after() -> int:
        nonlocal resolver_calls
        resolver_calls += 1
        return 0

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        body = await channel.open_sse(
            source,
            identity=_identity(),
            after=resolve_after,
        )
        frames = [frame async for frame in body]

    assert resolver_calls == 1
    assert frames == [
        b"id: 1\ndata: one\n\n",
        b"id: 2\ndata: two\n\n",
    ]
    assert source.close_calls == 1


async def test_channel_sse_cursor_callback_can_follow_from_current_tail(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")
    resolver_calls = 0

    def resolve_after() -> None:
        nonlocal resolver_calls
        resolver_calls += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        body = await channel.open_sse(
            source,
            identity=_identity(),
            after=resolve_after,
        )
        frames = [frame async for frame in body]

    assert resolver_calls == 1
    assert frames == [b"id: 1\ndata: message\n\n"]
    assert source.close_calls == 1


async def test_channel_sse_rejects_invalid_resolved_cursor_before_iteration(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")
    invalid_resolver = cast(Callable[[], int | None], lambda: "invalid")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        with pytest.raises(TypeError, match="after must be an integer or None"):
            await channel.open_sse(
                source,
                identity=_identity(),
                after=invalid_resolver,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_channel_sse_closes_source_when_cursor_callback_fails(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")
    resolver_calls = 0
    not_started = 0

    def resolve_after() -> int:
        nonlocal resolver_calls
        resolver_calls += 1
        raise RuntimeError("cursor lookup failed")

    async def delivery_not_started() -> None:
        nonlocal not_started
        assert source.closed.is_set()
        not_started += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        with pytest.raises(RuntimeError, match="cursor lookup failed"):
            await channel.open_sse(
                source,
                identity=_identity(),
                after=resolve_after,
                on_delivery_not_started=delivery_not_started,
            )

    assert resolver_calls == 1
    assert source.close_calls == 1
    assert not source.started.is_set()
    assert not_started == 1


async def test_sse_construction_failure_retains_reader_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("reader close failed")
    subscribed = []
    close = MessageSubscription.aclose

    async def subscription_ready() -> None:
        subscribed.append(True)

    async def close_subscription(subscription: MessageSubscription[object]) -> None:
        await close(subscription)
        raise failure

    async with Messaging() as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with monkeypatch.context() as patch:
            patch.setattr(MessageSubscription, "aclose", close_subscription)
            with pytest.raises(SseRenderingUnsupported) as caught:
                await channel.open_sse(
                    _ControlledSource("committed"),
                    identity=_identity(),
                    after=0,
                    on_subscribed=subscription_ready,
                )
        assert caught.value.__cause__ is failure
        replay = await channel.follow(identity=_identity(), after=0)
        assert [item.data async for item in replay] == ["committed"]
    assert subscribed == [True]
