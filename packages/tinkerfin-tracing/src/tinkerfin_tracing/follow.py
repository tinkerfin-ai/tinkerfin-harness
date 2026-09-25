"""Cancellation-safe ownership for live Trace followers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable

from ._tasks import (
    TaskOutcome,
    _cancel_task_once,
    cancellation_only,
    capture,
    join_owned_task,
    select_failure,
)
from .errors import TraceFollowLifecycleError

_ItemT_co = TypeVar("_ItemT_co", covariant=True)
_END = object()


@runtime_checkable
class TraceFollow(Protocol[_ItemT_co]):
    """A single-use live Trace iterator that owns upstream settlement.

    Use the asynchronous context-manager scope when a loop may stop early. Only one
    pull may be active; external close may cancel that pull and waits for upstream
    settlement before returning.
    """

    def __aiter__(self) -> TraceFollow[_ItemT_co]:
        """Return this live subscription."""

        ...

    async def __anext__(self) -> _ItemT_co:
        """Return the next committed Trace update.

        Raises:
            StopAsyncIteration: The follower has completed or closed.
            TraceFollowLifecycleError: Another pull is already active.
        """

        ...

    async def aclose(self) -> None:
        """Stop following and wait until the upstream iterator is closed.

        Raises:
            TraceFollowLifecycleError: The active pull tries to close itself.
        """

        ...

    async def __aenter__(self) -> TraceFollow[_ItemT_co]:
        """Enter a scope that closes this follower on every exit path.

        Raises:
            TraceFollowLifecycleError: This follower is already closed.
        """

        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close while preserving body failures and process-control priority."""

        ...


async def _close_trace_source(
    source: object,
    *,
    primary_error: BaseException | None,
) -> None:
    """Close one followed source without replacing an earlier operation failure."""

    close = getattr(source, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except BaseException as close_error:
        if primary_error is None or isinstance(primary_error, GeneratorExit):
            # GeneratorExit requests normal shutdown; it must not hide a
            # failure to close the borrowed reader.
            raise
        failure = select_failure(primary_error, close_error)
        if failure is not primary_error:
            raise failure


class _OwnedTraceFollow(Generic[_ItemT_co]):
    """Own each upstream pull without taking ownership of the consumer task."""

    def __init__(
        self,
        source_factory: Callable[[], AsyncIterator[_ItemT_co]],
    ) -> None:
        self._source_factory = source_factory
        self._source: AsyncIterator[_ItemT_co] | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._pull_task: asyncio.Task[TaskOutcome[object]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._closed = False

    def __aiter__(self) -> _OwnedTraceFollow[_ItemT_co]:
        return self

    async def __aenter__(self) -> _OwnedTraceFollow[_ItemT_co]:
        if self._closed:
            raise TraceFollowLifecycleError("a closed Trace follower cannot be entered")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as close_error:
            if exc_value is None or isinstance(exc_value, GeneratorExit):
                raise
            failure = select_failure(exc_value, close_error)
            if failure is not exc_value:
                raise failure

    async def __anext__(self) -> _ItemT_co:
        if self._closed:
            raise StopAsyncIteration
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - async methods run in a Task
            raise TraceFollowLifecycleError("Trace following requires an asyncio task")
        if self._active_task is not None:
            raise TraceFollowLifecycleError(
                "a Trace follow operation is already active"
            )
        self._active_task = current
        try:
            try:
                source = self._source
                if source is None:
                    source = self._source_factory()
                    self._source = source

                async def pull_next() -> object:
                    try:
                        return await anext(source)
                    except StopAsyncIteration:
                        return _END

                pull = asyncio.create_task(
                    capture(pull_next()), name="tinkerfin-trace-follow-pull"
                )
                self._pull_task = pull
                result = await join_owned_task(pull, cancel_operation=True)
            except BaseException as error:
                await self._finish(error)
                raise
            if result is _END:
                await self._finish(None)
                raise StopAsyncIteration
            return cast(_ItemT_co, result)
        finally:
            self._active_task = None
            self._pull_task = None

    async def aclose(self) -> None:
        current = asyncio.current_task()
        pull = self._pull_task
        if current is not None and current in (self._active_task, pull):
            raise TraceFollowLifecycleError(
                "a Trace follower cannot close its active pull"
            )
        self._closed = True
        failure: BaseException | None = None
        if pull is not None:
            # Let capture enter before cancelling a newly scheduled pull. The
            # shared cancellation guard also protects concurrent close calls.
            cancel = asyncio.get_running_loop().call_soon(_cancel_task_once, pull)
            try:
                await join_owned_task(pull, cancel_operation=False)
            except asyncio.CancelledError as error:
                outcome = pull.result()
                # Cancellation requested by this close is expected. A separate
                # caller cancellation or a cancellation carrying failures is not.
                if error is not outcome or not cancellation_only(error):
                    failure = error
            except BaseException as error:  # noqa: BLE001 - close settles before raising
                failure = error
            finally:
                cancel.cancel()
        await self._finish(failure)
        if failure is not None:
            raise failure

    async def _finish(self, primary_error: BaseException | None) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-trace-follow-close"
            )
            self._close_task = task
        try:
            await join_owned_task(task, cancel_operation=False)
        except BaseException as close_error:
            if primary_error is None or isinstance(primary_error, GeneratorExit):
                raise
            failure = select_failure(primary_error, close_error)
            if failure is not primary_error:
                raise failure

    async def _close_once(self) -> None:
        source = self._source
        self._source = None
        if source is not None:
            await _close_trace_source(source, primary_error=None)


def create_trace_follow(
    source_factory: Callable[[], AsyncIterator[_ItemT_co]],
) -> TraceFollow[_ItemT_co]:
    return _OwnedTraceFollow(source_factory)


__all__ = ["TraceFollow"]
