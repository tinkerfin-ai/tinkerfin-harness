"""Safe single-use SSE bodies for direct Runtime object streams."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._failure_evidence import select_failure
from ._tasks import (
    _capture_operation,
    _join_operation,
    _OperationOutcome,
    stop_operation,
)
from .errors import TinkerFinLifecycleError

ChunkT_co = TypeVar("ChunkT_co", covariant=True)
SourceT = TypeVar("SourceT")

SsePreflight = Callable[[], Awaitable[None]]


class SsePayload(BaseModel):
    """Validated payload fields controlled by a custom SSE mapper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data: str = Field(description="Complete SSE data value before line framing.")
    event: str | None = Field(
        default=None,
        description="Optional SSE event name without line breaks.",
    )
    retry: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Optional non-negative SSE reconnection delay in milliseconds.",
    )

    @field_validator("event")
    @classmethod
    def _event_has_no_line_breaks(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("event must not contain line breaks")
        return value


SseMapper = Callable[[SourceT], Awaitable[SsePayload | None]]
SseEventIdResolver = Callable[[SourceT], Awaitable[str | int | None]]


def encode_sse_payload(
    payload: SsePayload,
    *,
    event_id: str | int | None = None,
) -> bytes:
    """Encode validated payload fields as one complete UTF-8 SSE frame."""

    lines: list[str] = []
    if event_id is not None:
        if isinstance(event_id, bool) or not isinstance(event_id, str | int):
            raise TypeError("SSE event ID must be a string, integer, or None")
        value = str(event_id)
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError("SSE event ID must not contain line breaks or null bytes")
        lines.append(f"id: {value}")
    if payload.event is not None:
        lines.append(f"event: {payload.event}")
    if payload.retry is not None:
        lines.append(f"retry: {payload.retry}")
    normalized_data = payload.data.replace("\r\n", "\n").replace("\r", "\n")
    data_lines = normalized_data.split("\n")
    lines.extend(f"data: {line}" for line in data_lines)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class SseBody(Generic[ChunkT_co]):
    """Own one lazy single-use response iterator, each pull, and upstream cleanup."""

    is_tinkerfin_sse_body = True

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[ChunkT_co]],
        close: Callable[[], Awaitable[None]],
    ) -> None:
        """Initialize a lazy single-use body and its owned close callback.

        Args:
            source_factory: Factory invoked only by the first response pull.
            close: Idempotent asynchronous cleanup for the upstream stream.
        """

        self._source_factory = source_factory
        self._close = close
        self._source: AsyncIterator[ChunkT_co] | None = None
        self._started = False
        self._closed = False
        self._prepared = False
        self._active_task: asyncio.Task[_OperationOutcome[ChunkT_co]] | None = None
        self._close_task: asyncio.Task[_OperationOutcome[None]] | None = None

    async def prepare(self, *, preflight: SsePreflight | None = None) -> None:
        """Run optional host preflight without opening or pulling the source."""

        if self._closed:
            raise TinkerFinLifecycleError("a closed SSE body cannot be prepared")
        if self._started:
            raise TinkerFinLifecycleError("an active SSE body cannot be prepared")
        if self._prepared:
            raise TinkerFinLifecycleError("an SSE body can only be prepared once")
        if preflight is not None and not callable(preflight):
            raise TypeError("preflight must be an async callable or None")
        self._prepared = True
        try:
            if preflight is not None:
                outcome = preflight()
                if not inspect.isawaitable(outcome):
                    raise TypeError("preflight must return an awaitable")
                await outcome
        except BaseException:
            await self.aclose()
            raise

    def __aiter__(self) -> SseBody[ChunkT_co]:
        """Return this single-use asynchronous response body."""

        return self

    async def __anext__(self) -> ChunkT_co:
        """Return one chunk without exposing the upstream pull to caller cancellation."""

        if self._closed:
            raise StopAsyncIteration
        if self._active_task is not None:
            raise TinkerFinLifecycleError("an SSE body operation is already active")
        self._started = True

        async def pull_next() -> ChunkT_co:
            source = self._source
            if source is None:
                source = self._source_factory()
                self._source = source
            return await anext(source)

        pull = asyncio.create_task(
            _capture_operation(pull_next), name="tinkerfin-sse-body-pull"
        )
        self._active_task = pull
        try:
            try:
                return await _join_operation(pull, cancel_on_interrupt=True)
            except StopAsyncIteration:
                await self._finish(None)
                raise
            except BaseException as error:
                await self._finish(error)
                raise
        finally:
            if self._active_task is pull:
                self._active_task = None

    async def aclose(self) -> None:
        """Close the encoded iterator and its object stream idempotently."""

        current = asyncio.current_task()
        active = self._active_task
        if active is current and active is not None:
            raise TinkerFinLifecycleError(
                "an SSE body cannot close its active operation"
            )
        self._closed = True
        primary: BaseException | None = None
        if active is not None:
            try:
                await stop_operation(active)
            except BaseException as error:  # noqa: BLE001 - release resources before propagating
                primary = error
        await self._finish(primary)
        if primary is not None:
            raise primary

    async def _finish(self, primary: BaseException | None) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                _capture_operation(self._close_once),
                name="tinkerfin-sse-body-close",
            )
            self._close_task = task
        try:
            await _join_operation(task, cancel_on_interrupt=False)
        except BaseException as cleanup_error:
            if primary is not None and not isinstance(primary, GeneratorExit):
                selected = select_failure(
                    primary, cleanup_error, label="SSE cleanup also failed"
                )
                if selected is not primary:
                    if selected.__cause__ is None:
                        raise selected from primary
                    raise selected
                return
            raise

    async def _close_once(self) -> None:
        source = self._source
        self._source = None
        primary: BaseException | None = None
        if source is not None:
            close_source = getattr(source, "aclose", None)
            if close_source is not None:
                try:
                    await close_source()
                except BaseException as error:  # noqa: BLE001 - cleanup outcome
                    primary = error
        try:
            await self._close()
        except BaseException as error:  # noqa: BLE001 - cleanup outcome
            if primary is not None:
                primary = select_failure(
                    primary, error, label="SSE upstream cleanup also failed"
                )
            else:
                primary = error
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)


__all__ = [
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "encode_sse_payload",
]
