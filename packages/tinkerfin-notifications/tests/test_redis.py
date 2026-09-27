"""Control Pub/Sub acknowledgements and network outcomes through the Redis API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.typing import ChannelT

import tinkerfin_notifications.redis as redis_module
from tinkerfin_notifications import (
    Notification,
    Notifications,
    NotificationScope,
    NotificationTimeout,
    NotificationUnavailable,
    ResyncRequired,
)
from tinkerfin_notifications.redis import RedisBackend

_Frame = dict[str, object]


@pytest.fixture(autouse=True)
def controlled_deadlines(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> list[asyncio.Timeout]:
    scopes: list[asyncio.Timeout] = []
    if request.node.get_closest_marker("docker_integration") is not None:
        return scopes

    def timeout(_seconds: float) -> asyncio.Timeout:
        scope = asyncio.timeout(None)
        scopes.append(scope)
        return scope

    controlled = ModuleType("controlled_asyncio")
    controlled.__dict__.update(vars(asyncio))
    setattr(controlled, "timeout", timeout)
    monkeypatch.setattr(redis_module, "asyncio", controlled)
    return scopes


class ControlledPubSub(PubSub):
    def __init__(self, client: ControlledRedis) -> None:
        super().__init__(client.connection_pool)
        self.client = client
        self.frames: asyncio.Queue[_Frame | BaseException] = asyncio.Queue(maxsize=16)
        self.read_started = asyncio.Event()
        self.closed = False
        self.close_error: Exception | None = None
        self.close_attempted = asyncio.Event()

    async def subscribe(self, *args: ChannelT, **kwargs: Callable[..., object]) -> None:
        self.client.subscribing.put_nowait(self)
        self.client.bus.add(self)
        if self.client.auto_ack:
            self.ack()

    def ack(self) -> None:
        self.frames.put_nowait({"type": "subscribe", "channel": "changes", "data": 1})

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = 0.0
    ) -> _Frame:
        self.read_started.set()
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def aclose(self) -> None:
        self.close_attempted.set()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        self.client.bus.discard(self)


class ControlledRedis(Redis):
    def __init__(self, bus: set[ControlledPubSub], *, auto_ack: bool = True) -> None:
        super().__init__()
        self.bus = bus
        self.auto_ack = auto_ack
        self.subscribing: asyncio.Queue[ControlledPubSub] = asyncio.Queue(maxsize=8)
        self.children: list[ControlledPubSub] = []
        self.publish_error: BaseException | None = None
        self.was_closed = False

    def pubsub(self, **kwargs: Any) -> ControlledPubSub:
        # kwargs are the untyped third-party construction boundary.
        assert kwargs.get("ignore_subscribe_messages") is False
        child = ControlledPubSub(self)
        self.children.append(child)
        return child

    async def publish(
        self, channel: ChannelT, message: object, **kwargs: object
    ) -> int:
        if self.publish_error is not None:
            raise self.publish_error
        for subscription in tuple(self.bus):
            subscription.frames.put_nowait(
                {"type": "message", "channel": channel, "data": message}
            )
        return len(self.bus)

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        self.was_closed = True
        await super().aclose(close_connection_pool)


def notification() -> Notification:
    return Notification(
        scope=NotificationScope("project"), topic="documents.changed", key="one"
    )


async def test_start_waits_for_ack_and_does_not_own_client() -> None:
    client = ControlledRedis(set(), auto_ack=False)
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    entering = asyncio.create_task(service.__aenter__())
    subscription = await client.subscribing.get()
    await subscription.read_started.wait()
    assert not entering.done()
    subscription.ack()
    await entering
    await service.aclose()
    assert subscription.closed
    assert not client.was_closed
    await client.aclose()


async def test_subscription_deadline_closes_owned_pubsub_before_reporting_timeout(
    controlled_deadlines: list[asyncio.Timeout],
) -> None:
    client = ControlledRedis(set(), auto_ack=False)
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    entering = asyncio.create_task(service.__aenter__())
    try:
        child = await client.subscribing.get()
        await child.read_started.wait()
        controlled_deadlines[0].reschedule(0)
        with pytest.raises(NotificationTimeout):
            await entering
        assert child.closed
        assert not client.was_closed
    finally:
        await service.aclose()
        await client.aclose()


async def test_two_services_broadcast_independent_objects() -> None:
    bus: set[ControlledPubSub] = set()
    first_client, second_client = ControlledRedis(bus), ControlledRedis(bus)
    async with Notifications(
        backend=RedisBackend(first_client, channel="changes")
    ) as first:
        async with Notifications(
            backend=RedisBackend(second_client, channel="changes")
        ) as second:
            async with first.subscribe() as a, second.subscribe() as b:
                await first.publish(notification())
                assert await anext(a) == notification()
                assert await anext(b) == notification()
    assert not bus
    assert not first_client.was_closed and not second_client.was_closed
    await first_client.aclose()
    await second_client.aclose()


async def test_transparent_resubscription_ack_requires_resync() -> None:
    client = ControlledRedis(set())
    async with Notifications(
        backend=RedisBackend(client, channel="changes")
    ) as service:
        async with service.subscribe() as subscription:
            client.children[0].ack()
            assert await anext(subscription) == ResyncRequired("reconnected")
            await service.publish(notification())
            assert await anext(subscription) == notification()
    await client.aclose()


async def test_disconnect_reconnect_is_controlled_and_invalidates_continuity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_waiting, retry_allowed = asyncio.Event(), asyncio.Event()

    async def wait_for_retry(_seconds: float) -> None:
        retry_waiting.set()
        await retry_allowed.wait()

    monkeypatch.setattr(
        "tinkerfin_notifications.redis._reconnect_delay", wait_for_retry
    )
    client = ControlledRedis(set())
    backend = RedisBackend(client, channel="changes")
    async with Notifications(backend=backend) as service:
        async with service.subscribe() as subscription:
            old = await client.subscribing.get()
            old.frames.put_nowait(RedisConnectionError("controlled loss"))
            assert await anext(subscription) == ResyncRequired("disconnected")
            await retry_waiting.wait()
            assert old.closed
            with pytest.raises(NotificationUnavailable):
                await backend.publish(notification().model_dump_json().encode())
            retry_allowed.set()
            assert await anext(subscription) == ResyncRequired("reconnected")
            await service.publish(notification())
            assert await anext(subscription) == notification()
    assert all(child.closed for child in client.children)
    await client.aclose()


async def test_cancelled_start_closes_pubsub_waiting_for_ack() -> None:
    client = ControlledRedis(set(), auto_ack=False)
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    entering = asyncio.create_task(service.__aenter__())
    subscription = await client.subscribing.get()
    await subscription.read_started.wait()
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert subscription.closed
    assert not client.was_closed
    await client.aclose()


@pytest.mark.parametrize("phase", ["disconnect", "retry"])
async def test_close_during_connection_recovery_keeps_caller_uncancelled(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    recovery_waiting = asyncio.Event()
    original_close = ControlledPubSub.aclose

    async def close(pubsub: ControlledPubSub) -> None:
        if phase == "disconnect" and not recovery_waiting.is_set():
            recovery_waiting.set()
            await asyncio.Event().wait()
        await original_close(pubsub)

    async def retry(_seconds: float) -> None:
        recovery_waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ControlledPubSub, "aclose", close)
    monkeypatch.setattr(redis_module, "_reconnect_delay", retry)
    client = ControlledRedis(set())
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    await service.__aenter__()
    child = await client.subscribing.get()
    try:
        child.frames.put_nowait(RedisConnectionError("controlled loss"))
        await recovery_waiting.wait()
        await service.aclose()
        assert child.closed
        assert not client.was_closed
    finally:
        await asyncio.gather(service.aclose(), return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("failure_type", [RedisConnectionError, ValueError])
async def test_publication_failure_preserves_cause_without_sensitive_message(
    failure_type: type[Exception],
) -> None:
    client = ControlledRedis(set())
    original = failure_type("redis://secret@private")
    backend = RedisBackend(client, channel="changes")
    async with Notifications(backend=backend):
        client.publish_error = original
        with pytest.raises(NotificationUnavailable) as caught:
            await backend.publish(notification().model_dump_json().encode())
        assert caught.value.cause is original
        assert "secret" not in str(caught.value)
    await client.aclose()


async def test_service_close_joins_accepted_publication_before_releasing_client() -> (
    None
):
    publishing, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldRedis(ControlledRedis):
        async def publish(
            self, channel: ChannelT, message: object, **kwargs: object
        ) -> int:
            publishing.set()
            await release.wait()
            return await super().publish(channel, message, **kwargs)

    client = HeldRedis(set())
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    await service.__aenter__()
    publication = asyncio.create_task(service.publish(notification()))
    await publishing.wait()

    async def close() -> None:
        closing.set()
        await service.aclose()

    cleanup = asyncio.create_task(close())
    await closing.wait()
    assert not cleanup.done()
    release.set()
    await publication
    await cleanup
    assert client.children[0].closed
    assert not client.was_closed
    await client.aclose()


@pytest.mark.parametrize(
    "failure", [ValueError("invalid reply"), asyncio.CancelledError()]
)
async def test_reader_exit_terminates_existing_and_future_subscriptions(
    failure: BaseException,
) -> None:
    client = ControlledRedis(set())
    async with Notifications(
        backend=RedisBackend(client, channel="changes")
    ) as service:
        async with service.subscribe() as listener:
            child = client.children[0]
            child.frames.put_nowait(failure)
            with pytest.raises(NotificationUnavailable):
                await anext(listener)
            with pytest.raises(NotificationUnavailable):
                async with service.subscribe():
                    pytest.fail("a stopped reader cannot admit a subscriber")
    assert child.closed
    await client.aclose()


@pytest.mark.parametrize(
    "read_failure_type",
    [ValueError, RedisConnectionError, asyncio.CancelledError, BaseException],
)
async def test_failed_connection_cleanup_is_not_reported_as_success(
    read_failure_type: type[BaseException],
) -> None:
    client = ControlledRedis(set())
    service = Notifications(backend=RedisBackend(client, channel="changes"))
    await service.__aenter__()
    child = client.children[0]
    read_failure = read_failure_type("reader stopped")
    close_failure = OSError("cannot close")
    child.close_error = close_failure
    child.frames.put_nowait(read_failure)
    await child.close_attempted.wait()
    expected = (
        NotificationUnavailable
        if isinstance(read_failure, Exception)
        else read_failure_type
    )
    try:
        with pytest.raises(expected) as caught:
            await service.aclose()
        if not isinstance(read_failure, Exception):
            assert caught.value is read_failure
        failures: list[BaseException] = [caught.value]
        seen: set[int] = set()
        while failures:
            failure = failures.pop()
            if id(failure) in seen:
                continue
            seen.add(id(failure))
            if isinstance(failure, BaseExceptionGroup):
                failures.extend(failure.exceptions)
            cause = getattr(failure, "cause", None)
            failures.extend(
                item
                for item in (failure.__cause__, failure.__context__, cause)
                if isinstance(item, BaseException)
            )
        assert id(read_failure) in seen and id(close_failure) in seen
        assert not child.closed
        with pytest.raises(expected):
            await service.aclose()
    finally:
        # A failed shutdown is visible. Release the deliberately failed fake
        # resource explicitly, including when an assertion fails.
        child.close_error = None
        await child.aclose()
        await client.aclose()


async def test_logging_failure_cannot_stop_connection_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_waiting, retry_allowed = asyncio.Event(), asyncio.Event()

    async def retry(_seconds: float) -> None:
        retry_waiting.set()
        await retry_allowed.wait()

    def broken_log(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("host logging failed")

    monkeypatch.setattr("tinkerfin_notifications.redis._reconnect_delay", retry)
    monkeypatch.setattr("tinkerfin_notifications.redis.logger.warning", broken_log)
    client = ControlledRedis(set())
    async with Notifications(
        backend=RedisBackend(client, channel="changes")
    ) as service:
        async with service.subscribe() as listener:
            client.children[0].frames.put_nowait(RedisConnectionError("lost"))
            assert await anext(listener) == ResyncRequired("disconnected")
            await retry_waiting.wait()
            retry_allowed.set()
            assert await anext(listener) == ResyncRequired("reconnected")
            await service.publish(notification())
            assert await anext(listener) == notification()
    await client.aclose()


@pytest.mark.docker_integration
@pytest.mark.redis_e2e
async def test_real_redis_broadcasts_after_subscription_ack(redis_url: str) -> None:
    from uuid import uuid4

    channel = f"test:notifications:{uuid4().hex}"
    first_client, second_client = Redis.from_url(redis_url), Redis.from_url(redis_url)
    try:
        async with Notifications(
            backend=RedisBackend(first_client, channel=channel)
        ) as first:
            async with Notifications(
                backend=RedisBackend(second_client, channel=channel)
            ) as second:
                async with second.subscribe() as subscription:
                    await first.publish(notification())
                    assert await anext(subscription) == notification()
    finally:
        await first_client.aclose()
        await second_client.aclose()
