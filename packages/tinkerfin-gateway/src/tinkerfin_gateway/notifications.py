"""Expose authorized resource changes as a bounded browser notification stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import UTC, datetime
from functools import partial
from types import TracebackType
from typing import Self, TypeAlias

from tinkerfin_notifications import (
    Notification,
    Notifications,
    NotificationScope,
    ResyncRequired,
)

from ._cleanup import _capture_cleanup, _join_cleanup
from ._failures import _cancellation_only, _select_failure
from .errors import GatewayClosed, GatewayError

_Change: TypeAlias = Notification | ResyncRequired | str
NotificationAuthorization: TypeAlias = Callable[[], Awaitable[bool]]
ResourceChangeWatch: TypeAlias = Callable[
    [], AbstractAsyncContextManager[AsyncIterator[str | ResyncRequired]]
]


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


class _ChangeSource:
    """Keep context entry, pulls, and exit in one task with one outstanding demand.

    Watches may own ContextVar tokens or task-bound cancellation scopes. Their
    owner captures failures as values so cancellation cannot erase a cleanup
    cause before the stream joins it. No pull starts until a consumer asks.
    """

    def __init__(
        self, watch: Callable[[], AbstractAsyncContextManager[AsyncIterator[_Change]]]
    ) -> None:
        self.ready = asyncio.Event()
        self._demand = asyncio.Event()
        self._reply: asyncio.Future[_Change | BaseException] | None = None
        self._stopping = False
        self._started = False
        self._closing = False
        self.task = asyncio.create_task(
            self._run(watch), name="tinkerfin-gateway-change-source"
        )

    async def start(self) -> None:
        await self.ready.wait()
        if self.task.done() and (failure := self.task.result()) is not None:
            raise failure

    async def read(self) -> _Change:
        if self.task.done():
            raise self.task.result() or StopAsyncIteration()
        if self._stopping:
            raise StopAsyncIteration
        self._reply = asyncio.get_running_loop().create_future()
        self._demand.set()
        outcome = await asyncio.shield(self._reply)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def stop(self) -> None:
        if not self.task.done() and not self._stopping:
            self._stopping = True
            if self._started and not self._closing:
                self.task.cancel()

    async def _run(
        self, watch: Callable[[], AbstractAsyncContextManager[AsyncIterator[_Change]]]
    ) -> BaseException | None:
        stack = AsyncExitStack()
        failure: BaseException | None = None
        try:
            self._started = True
            if self._stopping:
                return None
            changes = await stack.enter_async_context(watch())
            self.ready.set()
            while not self._stopping:
                await self._demand.wait()
                self._demand.clear()
                value = await anext(changes)
                if self._reply is not None:
                    self._reply.set_result(value)
        except StopAsyncIteration:
            pass
        except BaseException as error:  # noqa: BLE001 - the stream restores control after joining its source
            if not (self._stopping and _cancellation_only(error)):
                failure = error
        finally:
            self._closing = True
            try:
                await stack.aclose()
            except BaseException as error:  # noqa: BLE001 - retain independent source and context failures
                failure = error if failure is None else _select_failure(failure, error)
            if self._reply is not None and not self._reply.done():
                self._reply.set_result(failure or StopAsyncIteration())
            self.ready.set()
        return failure


class NotificationStream:
    """Own authorized subscriptions through preflight, streaming, and disconnect.

    Obtain this from ``gateway.notifications`` or ``gateway.resource_changes``.
    The first SSE frame is
    ``ready`` and means all selected transport subscriptions are effective. The
    browser must then read its authoritative baseline. ``change`` invalidates one
    resource; ``resync`` invalidates continuity. No replay or delivery ACK is
    promised. Closing releases only these listeners, never the shared service.
    """

    def __init__(self) -> None:
        """Require the Gateway's authorized subscription preflight."""
        raise TypeError(
            "use await gateway.notifications(...) or gateway.resource_changes(...)"
        )

    @classmethod
    def _prepare(
        cls, expires_at: datetime | None, authorize: NotificationAuthorization | None
    ) -> Self:
        if expires_at is not None and (
            expires_at.tzinfo is None or expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must include its timezone")
        stream = cls.__new__(cls)
        stream._listeners = []
        stream._reads = {}
        stream._close_task = None
        stream._claimed = False
        stream._entered = False
        stream._expires_at = expires_at
        stream._authorize = authorize
        return stream

    @classmethod
    async def _watch(
        cls,
        watch_changes: ResourceChangeWatch,
        *,
        expires_at: datetime | None,
        authorize: NotificationAuthorization | None,
    ) -> Self:
        stream = cls._prepare(expires_at, authorize)
        try:
            if not await stream._allowed():
                raise PermissionError("Resource access is no longer authorized")
            source = _ChangeSource(watch_changes)
            stream._listeners.append(source)
            await source.start()
            if not await stream._allowed():
                raise PermissionError("Resource access is no longer authorized")
        except BaseException as error:
            try:
                await stream.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - retain both admission and cleanup outcomes
                raise _select_failure(error, cleanup_error)
            raise
        return stream

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
        stream = cls._prepare(expires_at, authorize)
        try:
            if not await stream._allowed():
                raise PermissionError("Notification access is no longer authorized")
            for scope in selected:
                source = _ChangeSource(
                    partial(service.subscribe, scope=scope, topics=selected_topics)
                )
                stream._listeners.append(source)
                await source.start()
            if not await stream._allowed():
                raise PermissionError("Notification access is no longer authorized")
        except BaseException as error:
            try:
                await stream.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and independent cleanup failures
                raise _select_failure(error, cleanup_error)
            raise
        return stream

    _listeners: list[_ChangeSource]
    _reads: dict[asyncio.Task[_Change], _ChangeSource]
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
                    elif isinstance(item, str):
                        if not item or len(item.encode("utf-8")) > 1024:
                            raise GatewayError(
                                "Resource change hint exceeds its bounds"
                            )
                        yield (
                            b"event: change\ndata: "
                            + json.dumps({"kind": item}).encode("utf-8")
                            + b"\n\n"
                        )
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

    def _read(self, listener: _ChangeSource) -> None:
        async def next_change() -> _Change:
            return await listener.read()

        task = asyncio.create_task(
            next_change(), name="tinkerfin-gateway-notification-read"
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
        for source in self._listeners:
            source.stop()
        if self._listeners:
            await asyncio.wait(tuple(source.task for source in self._listeners))
        failure: BaseException | None = None
        for source in self._listeners:
            outcome = source.task.result()
            if outcome is not None:
                failure = (
                    outcome if failure is None else _select_failure(failure, outcome)
                )
        tasks = tuple(self._reads)
        stopped: set[asyncio.Task[_Change]] = set()
        for task in tasks:
            if not task.done():
                stopped.add(task)
                task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        self._reads.clear()
        for task in tasks:
            try:
                task.result()
            except StopAsyncIteration:
                pass
            except BaseException as error:  # noqa: BLE001 - task.result retains the original cancellation and its causes
                if not (task in stopped and _cancellation_only(error)):
                    failure = (
                        error if failure is None else _select_failure(failure, error)
                    )
        if failure is not None:
            raise failure


__all__ = ["NotificationAuthorization", "NotificationStream", "ResourceChangeWatch"]
