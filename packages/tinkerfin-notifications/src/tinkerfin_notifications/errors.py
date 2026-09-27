"""Public failures for advisory resource-change delivery."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class NotificationErrorCode(StrEnum):
    """Stable categories that never expose transport credentials or payloads."""

    ERROR = "notifications.error"
    NOT_STARTED = "notifications.not_started"
    CLOSED = "notifications.closed"
    CAPACITY_EXCEEDED = "notifications.capacity_exceeded"
    UNAVAILABLE = "notifications.unavailable"
    TIMEOUT = "notifications.timeout"
    INVALID_MESSAGE = "notifications.invalid_message"


class NotificationError(Exception):
    """Separate client-safe failure information from the original trusted cause."""

    code = NotificationErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Copy contexts and retain the original exception without exposing it."""
        self.message = message
        self.context = MappingProxyType(dict(context or {}))
        self.diagnostic_context = MappingProxyType(dict(diagnostic_context or {}))
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class NotificationsNotStarted(NotificationError):
    """The notification service has not entered its asynchronous lifecycle."""

    code = NotificationErrorCode.NOT_STARTED


class NotificationsClosed(NotificationError):
    """The service or subscription cannot accept another operation."""

    code = NotificationErrorCode.CLOSED


class NotificationCapacityExceeded(NotificationError):
    """A subscription or individual message exceeds the configured capacity."""

    code = NotificationErrorCode.CAPACITY_EXCEEDED


class NotificationUnavailable(NotificationError):
    """The selected transport cannot currently accept notifications."""

    code = NotificationErrorCode.UNAVAILABLE


class NotificationTimeout(NotificationUnavailable):
    """A bounded transport operation did not finish; delivery may be unknown."""

    code = NotificationErrorCode.TIMEOUT


class InvalidNotification(NotificationError):
    """A received message does not satisfy the current notification contract."""

    code = NotificationErrorCode.INVALID_MESSAGE
