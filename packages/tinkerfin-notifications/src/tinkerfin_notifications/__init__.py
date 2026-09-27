"""Bounded advisory changes with explicit subscription and transport ownership."""

from .backend import MemoryBackend, NotificationBackend
from .errors import (
    InvalidNotification,
    NotificationCapacityExceeded,
    NotificationError,
    NotificationErrorCode,
    NotificationsClosed,
    NotificationsNotStarted,
    NotificationTimeout,
    NotificationUnavailable,
)
from .models import Notification, NotificationLimits, NotificationScope, ResyncRequired
from .service import Notifications, NotificationSubscription

__all__ = [
    "InvalidNotification",
    "MemoryBackend",
    "Notification",
    "NotificationBackend",
    "NotificationCapacityExceeded",
    "NotificationError",
    "NotificationErrorCode",
    "NotificationLimits",
    "NotificationScope",
    "NotificationSubscription",
    "NotificationTimeout",
    "NotificationUnavailable",
    "Notifications",
    "NotificationsClosed",
    "NotificationsNotStarted",
    "ResyncRequired",
]
