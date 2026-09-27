"""Expose authorized resource changes as a bounded browser notification stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, TypeAlias

from tinkerfin_notifications import (
    Notification,
    Notifications,
    NotificationScope,
    NotificationSubscription,
    ResyncRequired,
)

from ._cleanup import _capture_cleanup, _join_cleanup
from ._failures import _select_failure
from .errors import GatewayClosed

_Change: TypeAlias = Notification | ResyncRequired
NotificationAuthorization: TypeAlias = Callable[[], Awaitable[bool]]


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return asyncio.get_running_loop().time()


async def _wait_for_changes(
    tasks: Collection[asyncio.Task[_Change]],
    timeout: float,
) -> set[asyncio.Task[_Change]]:
    done, _ = await asyncio.wait(
        tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )
    return done


class NotificationStream:
    """Own authorized subscriptions through preflight, streaming, and disconnect.

    Obtain this from ``await gateway.notifications(...)``. The first SSE frame is
    ``ready`` and means all selected transport subscriptions are effective. The
    browser must then read its authoritative baseline. ``change`` invalidates one
    resource; ``resync`` invalidates continuity. No replay or delivery ACK is
    promised. Closing releases only these listeners, never the shared service.
    """

    def __init__(self) -> None:
        """Require the Gateway's authorized subscription preflight."""
        raise TypeError("use await gateway.notifications(...)")

    @classmethod
    async def _open(
        cls,
        service: Notifications,
        *,
        scopes: Collection[NotificationScope],
        topics: Collection[str] | None,
        expires_at: datetime | None,
        authorize: NotificationAuthorization | None,
    ) -> Self:
        if any(not isinstance(scope, NotificationScope) for scope in scopes):
            raise TypeError(
                "scopes must contain only authorized NotificationScope values"
            )
        selected = tuple(dict.fromkeys(scopes))
        selected_topics = None if topics is None else frozenset(topics)
        if not selected:
            raise ValueError("at least one authorized notification scope is required")
        if expires_at is not None and (
            expires_at.tzinfo is None or expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must include its timezone")
        stream = cls.__new__(cls)
        stream._stack = AsyncExitStack()
        stream._listeners = []
        stream._reads = {}
        stream._close_task = None
        stream._claimed = False
        stream._entered = False
        stream._expires_at = expires_at
        stream._authorize = authorize
        try:
            if not await stream._allowed():
                raise PermissionError("Notification access is no longer authorized")
            for scope in selected:
                stream._listeners.append(
                    await stream._stack.enter_async_context(
                        service.subscribe(scope=scope, topics=selected_topics)
                    )
                )
            if not await stream._allowed():
                raise PermissionError("Notification access is no longer authorized")
        except BaseException as error:
            try:
                await stream.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and independent cleanup failures
                raise _select_failure(error, cleanup_error)
            raise
        return stream

    _stack: AsyncExitStack
    _listeners: list[NotificationSubscription]
    _reads: dict[asyncio.Task[_Change], NotificationSubscription]
    _close_task: asyncio.Task[BaseException | None] | None
    _claimed: bool
    _entered: bool
    _expires_at: datetime | None
    _authorize: NotificationAuthorization | None

    async def __aenter__(self) -> Self:
        """Own one prepared notification stream, including an unconsumed stream."""
        if self._entered or self._close_task is not None:
            raise GatewayClosed("Notification stream is single-use")
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close all authorized subscriptions on every context exit."""
        failure: BaseException | None = None
        try:
            await self.aclose()
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and causes without Python restoring a cyclic context
            failure = (
                cleanup_error if exc is None else _select_failure(exc, cleanup_error)
            )
        if failure is not None:
            raise failure

    async def _allowed(self) -> bool:
        if self._expires_at is not None and _now() >= self._expires_at:
            return False
        allowed = self._authorize is None or await self._authorize()
        return allowed and (self._expires_at is None or _now() < self._expires_at)

    def to_sse(self) -> AsyncGenerator[bytes, None]:
        """Claim ready/change/resync frames with periodic access checks and heartbeats.

        Access is rechecked at most 15 seconds apart while being consumed. An
        explicit expiry shortens that interval. Revoked access ends the stream;
        host authorization failures propagate and still release subscriptions.
        """
        if self._claimed or self._close_task is not None:
            raise GatewayClosed("Notification stream is single-use")
        self._claimed = True
        return self._render()

    async def _render(self) -> AsyncGenerator[bytes, None]:
        primary: BaseException | None = None
        try:
            if self._close_task is not None:
                return
            # Authorization can change between preflight and response consumption.
            if not await self._allowed() or self._close_task is not None:
                return
            check_at = _monotonic() + 15.0
            yield b"event: ready\ndata: {}\n\n"
            if self._close_task is not None:
                return
            for listener in self._listeners:
                self._read(listener)
            while self._close_task is None:
                remaining = check_at - _monotonic()
                if self._expires_at is not None:
                    remaining = min(
                        remaining, (self._expires_at - _now()).total_seconds()
                    )
                if remaining <= 0:
                    if not await self._allowed() or self._close_task is not None:
                        return
                    check_at = _monotonic() + 15.0
                    yield b": heartbeat\n\n"
                    continue
                done = await _wait_for_changes(tuple(self._reads), remaining)
                for task in done:
                    if self._close_task is not None:
                        return
                    # Sending the preceding scope's frame can block. Recheck due
                    # authorization before each queued frame, not only each batch.
                    if _monotonic() >= check_at:
                        if not await self._allowed() or self._close_task is not None:
                            return
                        check_at = _monotonic() + 15.0
                    listener = self._reads.pop(task)
                    try:
                        item = task.result()
                    except StopAsyncIteration:
                        return
                    self._read(listener)
                    # A ready task may have waited behind a slow HTTP sender. Do
                    # not send a queued resource after its authorization expires.
                    if self._expires_at is not None and _now() >= self._expires_at:
                        return
                    if isinstance(item, ResyncRequired):
                        yield b"event: resync\ndata: {}\n\n"
                    else:
                        yield (
                            b"event: change\ndata: "
                            + item.model_dump_json().encode("utf-8")
                            + b"\n\n"
                        )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await self.aclose()
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                raise _select_failure(primary, cleanup_error)

    def _read(self, listener: NotificationSubscription) -> None:
        task = asyncio.create_task(
            anext(listener), name="tinkerfin-gateway-notification-read"
        )
        self._reads[task] = listener

    async def aclose(self) -> None:
        """Join all owned reads even on repeated or cancelled shutdown."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                _capture_cleanup(self._close()),
                name="tinkerfin-gateway-notification-close",
            )
        await _join_cleanup(self._close_task)

    async def _close(self) -> None:
        tasks = tuple(self._reads)
        for task in tasks:
            task.cancel()
        # These tasks are explicitly cancelled children. Their transport failures
        # are already delivered through the stream; shutdown still closes every scope.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._reads.clear()
        await self._stack.aclose()


__all__ = ["NotificationAuthorization", "NotificationStream"]
