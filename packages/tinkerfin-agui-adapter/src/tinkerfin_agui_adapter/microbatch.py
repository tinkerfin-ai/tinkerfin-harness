"""Bounded streaming micro-batching for AG-UI text content events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from time import monotonic
from typing import cast

from ag_ui.core import BaseEvent, TextMessageContentEvent

CHAR_THRESHOLD = 1024
TIME_THRESHOLD_SECONDS = 0.3


class ContentBatcher:
    """Merge consecutive deltas for one message until a bounded flush point."""

    def __init__(
        self,
        *,
        char_threshold: int = CHAR_THRESHOLD,
        time_threshold_seconds: float = TIME_THRESHOLD_SECONDS,
        now: Callable[[], float] = monotonic,
    ) -> None:
        """Initialize an in-memory batch with bounded size and elapsed time.

        Args:
            char_threshold: Maximum buffered character count before a flush.
            time_threshold_seconds: Maximum age of a non-empty batch in seconds.
            now: Monotonic clock used to calculate the remaining wait.
        """

        self._char_threshold = char_threshold
        self._time_threshold = time_threshold_seconds
        self._now = now
        self._message_id: str | None = None
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._raw_event: object | None = None
        self._buffer_started_at: float | None = None

    def add(self, event: BaseEvent) -> list[BaseEvent]:
        """Buffer a text delta or flush it before returning a non-text event."""

        if isinstance(event, TextMessageContentEvent):
            return self._add_content(event)
        emitted = self._flush()
        emitted.append(event)
        return emitted

    def flush(self) -> list[BaseEvent]:
        """Return the pending merged text event, or an empty list."""

        return self._flush()

    def seconds_until_flush(self) -> float | None:
        """Return the remaining bounded wait for the current text batch."""

        if not self._buffer or self._buffer_started_at is None:
            return None
        elapsed = self._now() - self._buffer_started_at
        return max(0.0, self._time_threshold - elapsed)

    def _add_content(self, event: TextMessageContentEvent) -> list[BaseEvent]:
        message_id = event.message_id
        now = self._now()
        emitted: list[BaseEvent] = []
        if self._buffer and (
            message_id != self._message_id
            or (
                self._buffer_started_at is not None
                and (now - self._buffer_started_at) >= self._time_threshold
            )
        ):
            emitted.extend(self._flush())
        if not self._buffer:
            self._message_id = message_id
            self._raw_event = event.raw_event
            self._buffer_started_at = now
        delta = event.delta or ""
        self._buffer.append(delta)
        self._buffer_chars += len(delta)
        if self._buffer_chars >= self._char_threshold:
            emitted.extend(self._flush())
        return emitted

    def _flush(self) -> list[BaseEvent]:
        if not self._buffer:
            return []
        if self._message_id is None:
            self._buffer = []
            self._buffer_chars = 0
            self._raw_event = None
            self._buffer_started_at = None
            return []
        merged = TextMessageContentEvent(
            message_id=self._message_id,
            delta="".join(self._buffer),
            raw_event=self._raw_event,
        )
        self._buffer = []
        self._buffer_chars = 0
        self._message_id = None
        self._raw_event = None
        self._buffer_started_at = None
        return [merged]


async def micro_batch(
    events: AsyncIterator[BaseEvent],
    *,
    batcher: ContentBatcher | None = None,
) -> AsyncGenerator[BaseEvent, None]:
    """Merge only consecutive `TEXT_MESSAGE_CONTENT` events with backpressure.

    A batch flushes at 1,024 characters, after 0.3 seconds, before every other
    event type, or at end of stream. Event order is preserved, Tool/reasoning/
    state/lifecycle events are never batched, and the caller-provided iterator is
    closed on completion, cancellation, or consumer abandonment.

    Args:
        events: Single-use AG-UI event iterator transferred to this generator.
        batcher: Optional request-scoped batch policy and pending text owner.

    Yields:
        Original non-text events and bounded merged text-content events in order.

    Raises:
        asyncio.CancelledError: The consumer cancels while pulling or settling input.
        BaseException: The input iterator, batching operation, or owned close fails.
    """

    batcher = batcher or ContentBatcher()
    iterator = aiter(events)
    pending_event: asyncio.Future[BaseEvent] | None = None
    primary_error: BaseException | None = None
    try:
        while True:
            timeout = batcher.seconds_until_flush()
            if pending_event is None and timeout is None:
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    break
                for emitted in batcher.add(event):
                    yield emitted
                continue
            if pending_event is None:
                pending_event = asyncio.ensure_future(anext(iterator))
            done, _pending = await asyncio.wait(
                {pending_event},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                for emitted in batcher.flush():
                    yield emitted
                continue

            completed_event = pending_event
            pending_event = None
            try:
                event = completed_event.result()
            except StopAsyncIteration:
                break
            for emitted in batcher.add(event):
                yield emitted
    except BaseException as error:
        primary_error = error
        raise
    finally:

        async def cleanup() -> tuple[BaseException | None, BaseException | None]:
            pending_error: BaseException | None = None
            if pending_event is not None:
                if not pending_event.done():
                    pending_event.cancel()
                results = await asyncio.gather(pending_event, return_exceptions=True)
                result = results[0]
                if isinstance(result, BaseException) and not isinstance(
                    result,
                    (asyncio.CancelledError, StopAsyncIteration),
                ):
                    pending_error = result
            close = getattr(iterator, "aclose", None)
            close_error: BaseException | None = None
            try:
                if close is not None:
                    await cast(Callable[[], Awaitable[object]], close)()
            except BaseException as error:  # noqa: BLE001
                close_error = error
            return pending_error, close_error

        cleanup_task = asyncio.create_task(cleanup())
        deferred_cancellation: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as error:
                if (
                    not isinstance(primary_error, asyncio.CancelledError)
                    and deferred_cancellation is None
                ):
                    deferred_cancellation = error
        pending_error, close_error = cleanup_task.result()

        if deferred_cancellation is not None:
            for stage, secondary_error in (
                ("pending upstream pull", pending_error),
                ("upstream close", close_error),
            ):
                if secondary_error is not None:
                    deferred_cancellation.add_note(
                        f"{stage} also failed: "
                        f"{type(secondary_error).__name__}: {secondary_error}"
                    )
            raise deferred_cancellation.with_traceback(
                deferred_cancellation.__traceback__
            )
        if primary_error is not None and not isinstance(primary_error, GeneratorExit):
            for stage, secondary_error in (
                ("pending upstream pull", pending_error),
                ("upstream close", close_error),
            ):
                if secondary_error is not None:
                    primary_error.add_note(
                        f"{stage} also failed: "
                        f"{type(secondary_error).__name__}: {secondary_error}"
                    )
        elif pending_error is not None:
            if close_error is not None:
                pending_error.add_note(
                    "upstream close also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            raise pending_error.with_traceback(pending_error.__traceback__)
        elif close_error is not None:
            raise close_error.with_traceback(close_error.__traceback__)
    for emitted in batcher.flush():
        yield emitted
