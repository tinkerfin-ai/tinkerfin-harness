"""Subscription notifications and their owned cleanup through public SSE APIs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import MessageSubscription, Messaging, RunNotFound


class _Events:
    messaging_codec_profile = "agui.event"
    messaging_source_type = BaseEvent
    messaging_replay_type = BaseEvent
    messaging_cancel_waits_for_first_item = True

    def __init__(self, *, release: asyncio.Event | None = None) -> None:
        self.messaging_identity = RunIdentity(
            namespace="test", thread_id="thread", run_id="run"
        )
        self.release = release
        self.prepared = 0
        self.pulled = False
        self.closed = 0
        self.cancelled = 0
        self.iterator = self.events()

    async def messaging_owner_preflight(self) -> None:
        self.prepared += 1

    async def events(self) -> AsyncGenerator[BaseEvent, None]:
        self.pulled = True
        yield RunStartedEvent(thread_id="thread", run_id="run")
        if self.release is not None:
            await self.release.wait()
        yield RunFinishedEvent(thread_id="thread", run_id="run")

    def __aiter__(self) -> AsyncGenerator[BaseEvent, None]:
        return self.iterator

    async def messaging_cancel_callback(self) -> list[BaseEvent]:
        self.cancelled += 1
        return [RunErrorEvent(message="cancelled", code="cancelled")]

    async def aclose(self) -> None:
        self.closed += 1
        await self.iterator.aclose()


def _event_types(frames: list[bytes]) -> list[str]:
    return [
        json.loads(line[6:])["type"]
        for frame in frames
        for line in frame.splitlines()
        if line.startswith(b"data: ")
    ]


def _contains(root: BaseException, target: BaseException) -> bool:
    pending = [root]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if error is target:
            return True
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return False


@pytest.mark.parametrize("agui", [False, True])
async def test_subscription_notification_runs_once_per_owner_and_attachment(
    agui: bool,
) -> None:
    ready, subscribed, not_started = [], [], []

    async def source_ready() -> None:
        ready.append(True)

    async def subscription_ready() -> None:
        subscribed.append(True)

    async def delivery_not_started() -> None:
        not_started.append(True)

    async with Messaging() as messaging:
        channel = (
            messaging.agui_channel(name="events")
            if agui
            else messaging.channel(name="events")
        )
        first = _Events()
        body = await channel.open_sse(
            first,
            after=0,
            on_source_ready=source_ready,
            on_subscribed=subscription_ready,
            on_delivery_not_started=delivery_not_started,
        )
        assert len(subscribed) == 1
        frames = [frame async for frame in body]
        unused = _Events()
        replay = await channel.open_sse(
            unused,
            after=0,
            on_source_ready=source_ready,
            on_subscribed=subscription_ready,
            on_delivery_not_started=delivery_not_started,
        )
        assert len(subscribed) == 2
        assert [frame async for frame in replay] == frames
        assert (
            await channel.get_run_status(identity=first.messaging_identity)
            == "completed"
        )

    assert _event_types(frames) == ["RUN_STARTED", "RUN_FINISHED"]
    assert ready == [True]
    assert not_started == []
    assert first.prepared == first.closed == 1
    assert unused.prepared == 0 and not unused.pulled
    assert unused.closed == 1


@pytest.mark.parametrize("agui", [False, True])
@pytest.mark.parametrize("callback", [42])
async def test_invalid_subscription_callback_precedes_cursor_and_source_side_effects(
    agui: bool,
    callback,
) -> None:
    source = _Events()
    cursors, not_started = [], []

    def after() -> int:
        cursors.append(True)
        return 0

    async def delivery_not_started() -> None:
        not_started.append(True)

    async with Messaging() as messaging:
        channel = (
            messaging.agui_channel(name="events")
            if agui
            else messaging.channel(name="events")
        )
        with pytest.raises(TypeError, match="on_subscribed must be an async callable"):
            await channel.open_sse(
                source,
                after=after,
                on_subscribed=callback,
                on_delivery_not_started=delivery_not_started,
            )
        with pytest.raises(RunNotFound):
            await channel.get_run_status(identity=source.messaging_identity)

    assert cursors == []
    assert source.prepared == 0 and not source.pulled
    assert source.closed == 1
    assert not_started == [True]


@pytest.mark.parametrize("attachment", [False, True])
@pytest.mark.parametrize(
    ("failure_type", "cleanup_type", "expected_type"),
    [
        (RuntimeError, OSError, RuntimeError),
        (TimeoutError, OSError, TimeoutError),
        (asyncio.CancelledError, OSError, asyncio.CancelledError),
        (KeyboardInterrupt, OSError, KeyboardInterrupt),
        (RuntimeError, asyncio.CancelledError, asyncio.CancelledError),
        (asyncio.CancelledError, KeyboardInterrupt, KeyboardInterrupt),
    ],
)
async def test_subscription_failure_detaches_only_its_reader_and_retains_causes(
    attachment: bool,
    failure_type: type[BaseException],
    cleanup_type: type[BaseException],
    expected_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    source = _Events(release=release)
    candidate = _Events() if attachment else source
    notification = failure_type("notification failed")
    notification_cause = LookupError("notification cause")
    notification.__cause__ = notification_cause
    cleanup = cleanup_type("subscription close failed")
    cleanup_cause = LookupError("cleanup cause")
    cleanup.__cause__ = cleanup_cause
    detached: list[MessageSubscription[object]] = []
    close = MessageSubscription.aclose
    not_started = []

    async def close_subscription(subscription: MessageSubscription[object]) -> None:
        await close(subscription)
        detached.append(subscription)
        raise cleanup

    async def subscribed() -> None:
        raise notification

    async def delivery_not_started() -> None:
        not_started.append(True)

    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="events")
        if attachment:
            first = await channel.open_sse(source, after=0)
            await first.aclose()
        with monkeypatch.context() as patch:
            patch.setattr(MessageSubscription, "aclose", close_subscription)
            with pytest.raises(expected_type) as caught:
                await channel.open_sse(
                    candidate,
                    on_subscribed=subscribed,
                    on_delivery_not_started=delivery_not_started,
                )
        assert caught.value is (
            notification if expected_type is failure_type else cleanup
        )
        assert all(
            _contains(caught.value, error)
            for error in (notification, notification_cause, cleanup, cleanup_cause)
        )
        assert len(detached) == 1
        with pytest.raises(RuntimeError, match="subscription is closed"):
            aiter(detached[0])
        assert source.cancelled == source.closed == 0
        assert (
            await channel.get_run_status(identity=source.messaging_identity)
            == "running"
        )
        release.set()
        replay = await channel.follow(identity=source.messaging_identity)
        events = [item.data async for item in replay]
        assert [item.type.value for item in events] == ["RUN_STARTED", "RUN_FINISHED"]
        assert (
            await channel.get_run_status(identity=source.messaging_identity)
            == "completed"
        )
    assert source.cancelled == 0 and source.closed == 1
    assert not_started == []
    if attachment:
        assert candidate.prepared == 0 and not candidate.pulled
        assert candidate.closed == 1


@pytest.mark.parametrize("callback", [lambda: None])
async def test_subscription_notification_requires_an_awaitable(
    callback,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detached: list[MessageSubscription[object]] = []
    close = MessageSubscription.aclose

    async def close_subscription(subscription: MessageSubscription[object]) -> None:
        detached.append(subscription)
        await close(subscription)

    monkeypatch.setattr(MessageSubscription, "aclose", close_subscription)
    async with Messaging() as messaging:
        channel = messaging.channel(name="events")
        with pytest.raises(TypeError, match="on_subscribed must return an awaitable"):
            await channel.open_sse(_Events(), on_subscribed=callback)
        assert len(detached) == 1
        with pytest.raises(RuntimeError, match="subscription is closed"):
            aiter(detached[0])


@pytest.mark.parametrize("callback_fails", [False, True])
async def test_repeated_cancellation_joins_notification_and_late_subscription_cleanup(
    callback_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, finish_callback, closing, finish_close, producer_release = (
        asyncio.Event() for _ in range(5)
    )
    source = _Events(release=producer_release)
    callback_finished = False
    closed = False
    notification = RuntimeError("notification failed")
    cleanup = OSError("subscription cleanup failed")
    close = MessageSubscription.aclose

    async def subscribed() -> None:
        nonlocal callback_finished
        entered.set()
        await finish_callback.wait()
        callback_finished = True
        if callback_fails:
            raise notification

    async def close_subscription(subscription: MessageSubscription[object]) -> None:
        nonlocal closed
        closing.set()
        await finish_close.wait()
        await close(subscription)
        closed = True
        raise cleanup

    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="events")
        with monkeypatch.context() as patch:
            patch.setattr(MessageSubscription, "aclose", close_subscription)
            request = asyncio.create_task(
                channel.open_sse(source, on_subscribed=subscribed)
            )
            await entered.wait()
            request.cancel("notification cancellation")
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await delivered.wait()
            request.cancel("repeated notification cancellation")
            assert not request.done()
            finish_callback.set()
            await closing.wait()
            assert callback_finished
            request.cancel("cleanup cancellation")
            assert not request.done()
            finish_close.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await request
        assert closed
        assert _contains(caught.value, cleanup)
        if callback_fails:
            assert _contains(caught.value, notification)
        assert source.cancelled == source.closed == 0
        producer_release.set()
        replay = await channel.follow(identity=source.messaging_identity)
        assert [item.data.type.value async for item in replay] == [
            "RUN_STARTED",
            "RUN_FINISHED",
        ]
    assert source.cancelled == 0 and source.closed == 1
