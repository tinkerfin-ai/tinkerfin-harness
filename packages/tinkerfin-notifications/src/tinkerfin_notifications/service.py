"""Own bounded, independently consumable resource-change subscriptions."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from types import TracebackType
from typing import Literal, Self

from pydantic import ValidationError

from ._failures import _select_failure
from .backend import MemoryBackend, NotificationBackend
from .errors import (
    InvalidNotification,
    NotificationCapacityExceeded,
    NotificationError,
    NotificationsClosed,
    NotificationsNotStarted,
    NotificationUnavailable,
)
from .models import Notification, NotificationLimits, NotificationScope, ResyncRequired

_Key = tuple[NotificationScope, str, str]
logger = logging.getLogger("tinkerfin.notifications.delivery")


async def _capture_failure(
    operation: Callable[[], Awaitable[None]],
) -> BaseException | None:
    # Owned tasks return control failures so only their awaiting owner can raise
    # them, after resource cleanup, without stopping the host event loop early.
    try:
        await operation()
    except BaseException as error:  # noqa: BLE001 - transfer the exact outcome to its owner
        return error
    return None


class NotificationSubscription(AsyncIterator[Notification | ResyncRequired]):
    """Own one bounded listener until its context exits or it is closed.

    Enter before reading an authoritative snapshot. Notifications received while a
    snapshot is being read remain pending. Closing wakes a blocked reader and does
    not close the shared service. Each subscription supports one concurrent pull.
    """

    def __init__(
        self,
        service: Notifications,
        *,
        scope: NotificationScope | None,
        topics: frozenset[str] | None,
        key: str | None,
    ) -> None:
        """Bind filters without registering until asynchronous context entry."""
        self._service = service
        self._scope = scope
        self._topics = topics
        self._key = key
        self._pending: OrderedDict[_Key, bytes] = OrderedDict()
        self._resync: ResyncRequired | None = None
        self._ready = asyncio.Event()
        self._state: Literal["new", "entering", "open", "closed"] = "new"
        self._failure: NotificationError | None = None
        self._pulling = False

    async def __aenter__(self) -> Self:
        """Register before the caller can start its authoritative read."""
        if self._state != "new":
            raise NotificationsClosed("Notification subscription is single-use")
        self._state = "entering"
        try:
            await self._service._backend.wait_ready()
            if self._state != "entering":
                raise NotificationsClosed(
                    "Notification subscription closed during entry"
                )
            # No await separates the current readiness decision and registration.
            self._service._register(self)
            self._state = "open"
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Remove this listener on normal, exceptional, and cancelled exits."""
        await self.aclose()

    def __aiter__(self) -> Self:
        """Consume this listener without opening a second subscription."""
        return self

    async def __anext__(self) -> Notification | ResyncRequired:
        """Return one owned value, prioritizing a sticky resynchronization signal."""
        if self._state in {"new", "entering"}:
            raise NotificationsNotStarted("Enter the notification subscription first")
        if self._pulling:
            raise RuntimeError("A notification subscription supports one reader")
        self._pulling = True
        try:
            while self._state == "open":
                if self._resync is not None:
                    result, self._resync = self._resync, None
                    return result
                if self._pending:
                    _, payload = self._pending.popitem(last=False)
                    return Notification.model_validate_json(payload)
                # No await separates checking pending work from clearing this
                # signal. A publication during wait sets it for the same reader.
                self._ready.clear()
                await self._ready.wait()
            if self._failure is not None:
                raise self._failure
            raise StopAsyncIteration
        finally:
            self._pulling = False

    def _matches(self, notification: Notification) -> bool:
        if self._state != "open":
            return False
        scope = self._scope
        if scope is not None and (
            scope.namespace != notification.scope.namespace
            or (
                scope.owner_id is not None
                and scope.owner_id != notification.scope.owner_id
            )
        ):
            return False
        if self._topics is not None and notification.topic not in self._topics:
            return False
        if self._key is not None and notification.key != self._key:
            return False
        return True

    def _accept(self, notification: Notification, payload: bytes) -> None:
        if not self._matches(notification):
            return
        key = (notification.scope, notification.topic, notification.key)
        if (
            key not in self._pending
            and len(self._pending) >= self._service.limits.max_pending_per_subscription
        ):
            self._invalidate(ResyncRequired("overflow"))
        self._pending[key] = payload
        self._ready.set()

    def _invalidate(self, signal: ResyncRequired) -> None:
        if self._state == "open":
            self._pending.clear()
            self._resync = signal
            self._ready.set()

    def _fail(self, error: NotificationError) -> None:
        if self._state == "open":
            self._failure = NotificationUnavailable(
                "Notification subscription stopped", cause=error
            )
            self._state = "closed"
            self._pending.clear()
            self._resync = None
            self._service._unregister(self)
            self._ready.set()

    async def aclose(self) -> None:
        """Idempotently release the listener and wake a blocked pull."""
        self._state = "closed"
        self._service._unregister(self)
        self._pending.clear()
        self._resync = None
        self._failure = None
        self._ready.set()


class Notifications:
    """Publish advisory changes through one explicitly owned transport.

    The default backend is in-process. A supplied backend transfers its lifecycle
    to this service; resources borrowed by that backend remain caller-owned.
    There is no durable history, acknowledgement from consumers, or global order.
    Publication only acknowledges a bounded queue entry. A supervised sender owns
    transport waits and diagnostics so notification latency cannot consume source
    leases. Shutdown discards unsent hints and joins an in-flight send. Consumers
    must retain authoritative reads for loss recovery.
    """

    def __init__(
        self,
        *,
        backend: NotificationBackend | None = None,
        limits: NotificationLimits | None = None,
    ) -> None:
        """Choose transport and capacity without opening resources."""
        self._backend = backend if backend is not None else MemoryBackend()
        self._limits = limits or NotificationLimits()
        self._subscriptions: set[NotificationSubscription] = set()
        self._state: Literal["new", "starting", "open", "closing", "closed"] = "new"
        self._start_task: asyncio.Task[BaseException | None] | None = None
        self._close_task: asyncio.Task[BaseException | None] | None = None
        self._pending: OrderedDict[_Key, bytes] = OrderedDict()
        self._pending_ready = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None
        self._sender_failure: BaseException | None = None
        self._transport_failure: NotificationError | None = None

    @property
    def limits(self) -> NotificationLimits:
        """Return this service's immutable resource bounds."""
        return self._limits

    async def __aenter__(self) -> Self:
        """Start once and expose only an effective transport subscription."""
        if self._state != "new":
            raise NotificationsClosed("Notifications is a single-use service")
        self._state = "starting"
        self._start_task = asyncio.create_task(
            _capture_failure(lambda: self._backend.start(self._receive)),
            name="tinkerfin-notifications-start",
        )
        try:
            failure = await self._start_task
            if failure is not None:
                raise failure
        except BaseException as startup_error:  # noqa: BLE001 - close adopted resources before restoring startup failure or control
            failure = startup_error
            try:
                await self.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - raise the selected outcome after leaving the cleanup exception context
                failure = _select_failure(startup_error, cleanup_error)
            raise failure
        if self._state != "starting":
            raise NotificationsClosed("Notifications closed during startup")
        self._state = "open"
        self._sender = asyncio.create_task(
            self._send_pending(), name="tinkerfin-notifications-sender"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close owned listeners and transport on every context exit."""
        failure: BaseException | None = None
        try:
            await self.aclose()
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and causes without Python restoring a cyclic context
            failure = (
                cleanup_error if exc is None else _select_failure(exc, cleanup_error)
            )
        if failure is not None:
            raise failure

    async def publish(self, notification: Notification) -> None:
        """Freeze and queue a committed change without waiting for network delivery.

        Args:
            notification: Source-owned routing facts for an authoritative change.

        Raises:
            NotificationCapacityExceeded: The message or pending publication count
                exceeds its bound. Updates to an already pending key are coalesced.
            NotificationError: The service cannot admit the hint. Success only
                confirms admission; transport failure and shutdown can lose hints.
        """
        self._require_open()
        if not isinstance(notification, Notification):
            raise TypeError("notification must be a Notification")
        # Validate the actual values before encoding: JSON serialization may turn
        # nonfinite floats into null. Freeze before the first scheduling point.
        frozen = Notification.model_validate(notification.model_dump(mode="python"))
        payload = frozen.model_dump_json().encode("utf-8")
        if len(payload) > self._limits.max_notification_bytes:
            raise NotificationCapacityExceeded("Notification exceeds its byte limit")
        key = (frozen.scope, frozen.topic, frozen.key)
        if (
            key not in self._pending
            and len(self._pending) >= self._limits.max_pending_publications
        ):
            raise NotificationCapacityExceeded("Notification publication queue is full")
        self._pending[key] = payload
        self._pending_ready.set()

    async def _send_pending(self) -> None:
        try:
            while self._state == "open":
                if not self._pending:
                    self._pending_ready.clear()
                    await self._pending_ready.wait()
                    continue
                _, payload = self._pending.popitem(last=False)
                try:
                    await self._backend.publish(payload)
                except NotificationError as error:
                    notification = Notification.model_validate_json(payload)
                    for subscription in tuple(self._subscriptions):
                        if subscription._matches(notification):
                            subscription._invalidate(ResyncRequired("disconnected"))
                    # Advisory delivery cannot change the already committed source.
                    # The service owns this transport outcome; source callers only
                    # observe admission to the bounded publication queue.
                    try:
                        logger.warning(
                            "Notification delivery failed",
                            extra={"tinkerfin_code": error.code.value},
                        )
                    except Exception:  # noqa: BLE001 - host diagnostics cannot stop delivery
                        pass
        except BaseException as error:  # noqa: BLE001 - retain background control for the closing owner
            failure = NotificationUnavailable(
                "Notification sender stopped", cause=error
            )
            self._sender_failure = failure if isinstance(error, Exception) else error
            self._receive(failure)

    def subscribe(
        self,
        *,
        scope: NotificationScope | None = None,
        topics: Collection[str] | None = None,
        key: str | None = None,
    ) -> NotificationSubscription:
        """Create an explicit listener for trusted in-process callers.

        An omitted scope selects every namespace; an omitted owner selects the
        namespace. Neither grants access to remote clients. Hosts must derive
        filters from authorization rather than accepting client-owned identities.
        Enter the returned context before loading an authoritative snapshot.
        """
        self._require_open()
        if scope is not None and not isinstance(scope, NotificationScope):
            raise TypeError("scope must be a NotificationScope")
        if isinstance(topics, str):
            raise TypeError("topics must be a collection, not a string")
        selected = None if topics is None else frozenset(topics)
        if selected is not None and any(not item for item in selected):
            raise ValueError("topics cannot contain empty names")
        return NotificationSubscription(self, scope=scope, topics=selected, key=key)

    def _require_open(self) -> None:
        if self._state in {"new", "starting"}:
            raise NotificationsNotStarted("Enter Notifications before using it")
        if self._state != "open":
            raise NotificationsClosed("Notifications is closed")
        if self._transport_failure is not None:
            raise NotificationUnavailable(
                "Notification delivery is unavailable", cause=self._transport_failure
            )

    def _register(self, subscription: NotificationSubscription) -> None:
        self._require_open()
        if len(self._subscriptions) >= self._limits.max_subscriptions:
            raise NotificationCapacityExceeded(
                "Notification subscription limit reached"
            )
        self._subscriptions.add(subscription)

    def _unregister(self, subscription: NotificationSubscription) -> None:
        self._subscriptions.discard(subscription)

    def _receive(self, payload: bytes | ResyncRequired | NotificationError) -> None:
        if isinstance(payload, NotificationError):
            self._transport_failure = payload
            for subscription in tuple(self._subscriptions):
                subscription._fail(payload)
            return
        if isinstance(payload, ResyncRequired):
            for subscription in tuple(self._subscriptions):
                subscription._invalidate(payload)
            return
        if len(payload) > self._limits.max_notification_bytes:
            raise InvalidNotification("Received notification exceeds its byte limit")
        try:
            notification = Notification.model_validate_json(payload)
        except ValidationError as error:
            raise InvalidNotification(
                "Received notification does not match the current contract", cause=error
            ) from error
        for subscription in tuple(self._subscriptions):
            subscription._accept(notification, payload)

    async def aclose(self) -> None:
        """Join owned cleanup even if a closing caller is cancelled.

        Repeated closes share the same outcome. Cancellation is restored after
        cleanup; one cancelled waiter cannot abandon the transport's reader task.
        """
        if asyncio.current_task() is self._sender:
            raise NotificationsClosed(
                "A publication cannot close its own notification service"
            )
        if self._close_task is None:
            self._state = "closing"
            self._pending.clear()
            self._pending_ready.set()
            self._close_task = asyncio.create_task(
                _capture_failure(self._close), name="tinkerfin-notifications-close"
            )
        cancellation: asyncio.CancelledError | None = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancellation = error
        failure = self._close_task.result()
        if failure is not None:
            raise (
                failure
                if cancellation is None
                else _select_failure(cancellation, failure)
            )
        if cancellation is not None:
            raise cancellation

    async def _close(self) -> None:
        startup_failure: BaseException | None = None
        try:
            if self._start_task is not None and not self._start_task.done():
                self._start_task.cancel()
                try:
                    startup_failure = await self._start_task
                except asyncio.CancelledError:
                    # This cancellation belongs to the explicitly cancelled child.
                    # aclose separately restores its own caller's cancellation.
                    pass
                except BaseException as error:  # noqa: BLE001 - startup failure cannot skip backend cleanup
                    startup_failure = error
                if isinstance(startup_failure, asyncio.CancelledError):
                    startup_failure = None
            for subscription in tuple(self._subscriptions):
                await subscription.aclose()
            # Queued hints are advisory and discarded at shutdown. An already
            # started transport send keeps its borrowed client until it settles.
            if self._sender is not None:
                await self._sender
            failure = startup_failure
            if self._sender_failure is not None:
                failure = (
                    self._sender_failure
                    if failure is None
                    else _select_failure(failure, self._sender_failure)
                )
            try:
                await self._backend.aclose()
            except BaseException as error:  # noqa: BLE001 - retain all failures until after cleanup's exception context
                failure = error if failure is None else _select_failure(failure, error)
            if failure is not None:
                raise failure
        finally:
            self._state = "closed"
