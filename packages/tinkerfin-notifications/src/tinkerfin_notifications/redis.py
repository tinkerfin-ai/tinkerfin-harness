"""Redis Pub/Sub delivery over a borrowed asynchronous client."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from ._failures import _cancellation_only, _select_failure
from .backend import NotificationReceiver
from .errors import (
    InvalidNotification,
    NotificationCapacityExceeded,
    NotificationError,
    NotificationsClosed,
    NotificationsNotStarted,
    NotificationTimeout,
    NotificationUnavailable,
)
from .models import ResyncRequired

logger = logging.getLogger("tinkerfin.notifications.redis")


class _RedisPubSub(Protocol):
    """Narrow redis-py's untyped replies at the actual driver boundary."""

    async def subscribe(self, channel: str) -> None: ...

    async def get_message(self, *, timeout: float | None) -> object: ...

    async def aclose(self) -> None: ...


class _RedisCommands(Protocol):
    """Describe the asynchronous operations used from redis-py 7.4.1."""

    def pubsub(self, *, ignore_subscribe_messages: bool) -> _RedisPubSub: ...

    async def publish(self, channel: str, message: bytes) -> int: ...


async def _reconnect_delay(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RedisBackend:
    """Broadcast hints across services through one Redis Pub/Sub channel.

    The client remains borrowed. This backend owns its Pub/Sub connection and one
    supervised reader. Subscribe acknowledgement establishes readiness; repeated
    acknowledgement detects redis-py's transparent resubscription as continuity
    loss. Publications are not persisted and a timeout can mean unknown delivery.
    """

    def __init__(
        self,
        client: Redis,
        *,
        channel: str = "tinkerfin:notifications",
        operation_timeout_seconds: float = 1.0,
        reconnect_delay_seconds: float = 1.0,
        max_concurrent_publications: int = 32,
    ) -> None:
        """Borrow a client and bound readiness and publication operations.

        Args:
            client: Open client owned and closed by the host.
            channel: Deployment-specific Pub/Sub channel shared by all instances.
            operation_timeout_seconds: Maximum wait for connection, acknowledgement,
                readiness, or an individual publication.
            reconnect_delay_seconds: Bounded retry spacing after reader failures.
            max_concurrent_publications: Accepted network publications; excess calls
                fail immediately instead of creating another unbounded wait queue.
        """
        if not isinstance(client, Redis):
            raise TypeError("client must be redis.asyncio.Redis")
        if not channel or channel != channel.strip():
            raise ValueError("channel must be a nonempty canonical string")
        for value in (operation_timeout_seconds, reconnect_delay_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("transport durations must be finite positive seconds")
        if (
            isinstance(max_concurrent_publications, bool)
            or not isinstance(max_concurrent_publications, int)
            or max_concurrent_publications < 1
        ):
            raise ValueError("max_concurrent_publications must be positive")
        # redis-py's kwargs and parsed Pub/Sub replies are incompletely annotated.
        # This narrowed driver boundary preserves its actual async public methods;
        # received object values are validated by _message before use.
        self._client = cast(_RedisCommands, client)
        self._channel = channel
        self._timeout = float(operation_timeout_seconds)
        self._reconnect_seconds = float(reconnect_delay_seconds)
        self._receive: NotificationReceiver | None = None
        self._pubsub: _RedisPubSub | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._available = False
        self._closed = False
        self._fatal: NotificationError | None = None
        self._max_publications = max_concurrent_publications
        self._publications = 0
        self._publications_idle = asyncio.Event()
        self._publications_idle.set()

    async def start(self, receive_notification: NotificationReceiver) -> None:
        """Subscribe and consume the real ACK before starting shared delivery."""
        if self._closed or self._receive is not None:
            raise NotificationsClosed("Redis notification backend is single-use")
        self._receive = receive_notification
        await self._connect()
        self._reader = asyncio.create_task(
            self._listen(), name="tinkerfin-notifications-redis-reader"
        )

    async def _connect(self) -> None:
        if self._closed:
            raise NotificationsClosed("Redis notification backend is closed")
        try:
            pubsub = self._client.pubsub(ignore_subscribe_messages=False)
            self._pubsub = pubsub
            async with asyncio.timeout(self._timeout):
                await pubsub.subscribe(self._channel)
                while True:
                    message = await pubsub.get_message(timeout=None)
                    if message is None:
                        continue
                    kind, channel, _payload = _message(message)
                    if kind == "subscribe" and channel == self._channel:
                        self._available = True
                        self._ready.set()
                        return
        except TimeoutError as error:
            raise NotificationTimeout(
                "Redis notification subscription timed out", cause=error
            ) from error
        except NotificationError:
            raise
        except Exception as error:
            raise NotificationUnavailable(
                "Redis notification subscription is unavailable", cause=error
            ) from error

    async def wait_ready(self) -> None:
        """Require current ACK readiness; one cancelled waiter leaves the reader alive."""
        if self._receive is None:
            raise NotificationsNotStarted("Redis notifications are not started")
        try:
            async with asyncio.timeout(self._timeout):
                while not self._available:
                    if self._closed:
                        raise NotificationsClosed("Redis notifications are closed")
                    if self._fatal is not None:
                        raise self._fatal
                    await self._ready.wait()
        except TimeoutError as error:
            raise NotificationTimeout(
                "Redis notification readiness timed out", cause=error
            ) from error
        if self._closed:
            raise NotificationsClosed("Redis notifications are closed")

    async def publish(self, payload: bytes) -> None:
        """Publish once with a deadline; never wait for subscribers to process it."""
        if self._closed:
            raise NotificationsClosed("Redis notifications are closed")
        if self._receive is None:
            raise NotificationsNotStarted("Redis notifications are not started")
        if not self._available:
            raise NotificationUnavailable("Redis notification delivery is disconnected")
        if self._publications >= self._max_publications:
            raise NotificationCapacityExceeded(
                "Redis notification publication limit reached"
            )
        self._publications += 1
        self._publications_idle.clear()
        try:
            async with asyncio.timeout(self._timeout):
                await self._client.publish(self._channel, payload)
        except TimeoutError as error:
            raise NotificationTimeout(
                "Redis notification publication timed out; delivery is unknown",
                cause=error,
            ) from error
        except NotificationError:
            raise
        except Exception as error:
            raise NotificationUnavailable(
                "Redis notification publication failed", cause=error
            ) from error
        finally:
            self._publications -= 1
            if not self._publications:
                self._publications_idle.set()

    def _signal(self, reason: ResyncRequired | NotificationError) -> None:
        if not self._closed and self._receive is not None:
            self._receive(reason)

    async def _listen(self) -> None:
        failure: BaseException | None = None
        try:
            while not self._closed:
                try:
                    pubsub = self._pubsub
                    if pubsub is None:
                        await self._connect()
                        self._signal(ResyncRequired("reconnected"))
                        pubsub = self._pubsub
                    assert pubsub is not None
                    message = await pubsub.get_message(timeout=None)
                    if message is None:
                        continue
                    kind, channel, payload = _message(message)
                    if channel != self._channel:
                        raise InvalidNotification("Redis returned another channel")
                    if kind == "subscribe":
                        # redis-py can reconnect inside get_message without raising.
                        # An ACK after startup therefore invalidates continuity too.
                        self._signal(ResyncRequired("reconnected"))
                    elif kind == "message":
                        if not isinstance(payload, str | bytes):
                            raise InvalidNotification(
                                "Redis returned nontext notification data"
                            )
                        receiver = self._receive
                        assert receiver is not None
                        receiver(
                            payload.encode("utf-8")
                            if isinstance(payload, str)
                            else payload
                        )
                    else:
                        raise InvalidNotification(
                            "Redis returned an unexpected subscription frame"
                        )
                except (RedisError, OSError, NotificationError) as error:
                    connection_failure = error
                else:
                    continue
                self._available = False
                self._ready.clear()
                self._signal(ResyncRequired("disconnected"))
                try:
                    logger.warning(
                        "Notification subscription lost continuity",
                        extra={
                            "tinkerfin_error_type": type(connection_failure).__name__
                        },
                    )
                except Exception:  # noqa: BLE001 - host logging cannot stop recovery
                    pass
                # Recovery has handled the connection failure. Await outside its
                # except block so normal shutdown cancellation does not inherit
                # that handled error as an independent cleanup failure.
                disconnect_failure: BaseException | None = None
                try:
                    await self._disconnect()
                except Exception as error:  # noqa: BLE001 - retain the connection cause alongside failed cleanup
                    disconnect_failure = _select_failure(error, connection_failure)
                if disconnect_failure is not None:
                    raise disconnect_failure
                await _reconnect_delay(self._reconnect_seconds)
        except BaseException as error:
            # Wake waiters and preserve control failures through cleanup.
            failure = error
            if not isinstance(error, Exception):
                raise
        finally:
            # Every non-closing exit invalidates readiness and terminates existing
            # listeners. An unexpected cancellation must not leave a set ready
            # event behind an unavailable transport and spin future waiters.
            self._available = False
            if not self._closed:
                self._fatal = NotificationUnavailable(
                    "Redis notification reader stopped", cause=failure
                )
                self._ready.set()
                self._signal(self._fatal)
            cleanup_failure: BaseException | None = None
            try:
                await self._disconnect()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve the reader and its independent cleanup failure
                cleanup_failure = cleanup_error
            if cleanup_failure is not None:
                primary = (
                    failure
                    if failure is not None and not isinstance(failure, Exception)
                    else NotificationUnavailable(
                        "Redis notification reader stopped", cause=failure
                    )
                    if failure is not None
                    else None
                )
                raise (
                    cleanup_failure
                    if primary is None
                    else _select_failure(primary, cleanup_failure)
                )

    async def _disconnect(self) -> None:
        pubsub = self._pubsub
        if pubsub is not None:
            try:
                async with asyncio.timeout(self._timeout):
                    await pubsub.aclose()
            except TimeoutError as error:
                raise NotificationTimeout(
                    "Redis notification connection close timed out", cause=error
                ) from error
            except NotificationError:
                raise
            except Exception as error:
                raise NotificationUnavailable(
                    "Redis notification connection could not close", cause=error
                ) from error
            # Keep ownership on failure so service shutdown can retry cleanup and
            # cannot report success while the Pub/Sub resource remains open.
            self._pubsub = None

    async def aclose(self) -> None:
        """Stop and await the owned reader and Pub/Sub, leaving the client open."""
        self._closed = True
        self._available = False
        self._ready.set()
        await self._publications_idle.wait()
        task = self._reader
        failure: BaseException | None = None
        try:
            if task is not None:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError as error:
                    # Only the child is cancelled; the facade shields this cleanup
                    # and restores cancellation to its external closing caller.
                    current = asyncio.current_task()
                    if (
                        current is not None and current.cancelling()
                    ) or not _cancellation_only(error):
                        failure = error
                except BaseException as error:  # noqa: BLE001 - disconnect must finish before the reader failure is restored
                    failure = error
        finally:
            try:
                await self._disconnect()
            except BaseException as error:  # noqa: BLE001 - retain each independently failed owned cleanup
                failure = error if failure is None else _select_failure(failure, error)
            self._receive = None
        if failure is not None:
            raise failure


def _message(value: object) -> tuple[str, str, object]:
    if not isinstance(value, Mapping):
        raise InvalidNotification("Redis returned an invalid subscription frame")
    fields = cast(Mapping[object, object], value)
    kind, channel = fields.get("type"), fields.get("channel")
    try:
        if isinstance(kind, bytes):
            kind = kind.decode("utf-8")
        if isinstance(channel, bytes):
            channel = channel.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidNotification(
            "Redis returned a non-UTF-8 subscription identity", cause=error
        ) from error
    if not isinstance(kind, str) or not isinstance(channel, str):
        raise InvalidNotification("Redis returned an invalid subscription identity")
    return kind, channel, fields.get("data")


__all__ = ["RedisBackend"]
