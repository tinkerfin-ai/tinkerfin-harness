"""Transport lifecycle shared by the in-process and Redis implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias

from .errors import NotificationError, NotificationsClosed, NotificationsNotStarted
from .models import ResyncRequired

NotificationReceiver: TypeAlias = Callable[
    [bytes | ResyncRequired | NotificationError], None
]


class NotificationBackend(Protocol):
    """Transport immutable JSON hints without acquiring consumer lifetimes.

    The owning Notifications service calls start once and closes the backend on
    every exit, including failed startup. start returns only when subscription is
    effective. A receiver must be invoked on the owner's event loop. Backends may
    deliver duplicates and must report disconnected/reconnected continuity loss.
    """

    async def start(self, receive_notification: NotificationReceiver) -> None:
        """Establish delivery before exposing the service to publishers."""
        ...

    async def publish(self, payload: bytes) -> None:
        """Accept an immutable message or raise a stable transport failure."""
        ...

    async def wait_ready(self) -> None:
        """Wait for current delivery readiness without cancelling shared startup."""
        ...

    async def aclose(self) -> None:
        """Release owned resources and await owned tasks; preserve cancellation."""
        ...


class MemoryBackend:
    """Deliver hints to one Notifications service in the same event loop.

    Share the service, rather than a backend instance, among in-process producers
    and consumers. This backend owns no background tasks or external resources.
    """

    def __init__(self) -> None:
        """Create a single-use transport without starting delivery."""
        self._receive: NotificationReceiver | None = None
        self._closed = False

    async def start(self, receive_notification: NotificationReceiver) -> None:
        """Bind the owning service exactly once."""
        if self._closed or self._receive is not None:
            raise NotificationsClosed("Notification backend cannot be entered again")
        self._receive = receive_notification

    async def publish(self, payload: bytes) -> None:
        """Broadcast an immutable hint without awaiting downstream consumers."""
        if self._closed:
            raise NotificationsClosed("Notification backend is closed")
        if self._receive is None:
            raise NotificationsNotStarted("Notification backend is not started")
        self._receive(payload)

    async def wait_ready(self) -> None:
        """Require an effective in-process receiver."""
        if self._closed:
            raise NotificationsClosed("Notification backend is closed")
        if self._receive is None:
            raise NotificationsNotStarted("Notification backend is not started")

    async def aclose(self) -> None:
        """Release the receiver; repeated close is safe."""
        self._closed = True
        self._receive = None
