"""Optional Starlette responses with explicit ownership of prepared streams."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Mapping
from typing import TypeAlias

from ag_ui.core import BaseEvent
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from tinkerfin_messaging import MessageSubscription

from ._cleanup import _capture_cleanup, _close_owned, _join_cleanup
from ._failures import _select_failure
from .errors import GatewayClosed
from .notifications import NotificationStream

_Source: TypeAlias = MessageSubscription[BaseEvent] | NotificationStream


class SseResponse(StreamingResponse):
    """Send one prepared stream and close it on every ASGI exit.

    Obtain this through ``await sse_response(...)``. If the response will not be
    sent, explicitly call ``aclose()``. Client disconnect only releases the reader;
    it does not cancel durable Agent execution. Repeated close shares its outcome.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        source: _Source,
        body: AsyncGenerator[bytes, None],
        headers: Mapping[str, str] | None,
        *,
        status_code: int = 200,
    ) -> None:
        """Adopt one already prepared reader and its renderer."""
        self._source = source
        self._body = body
        self._serving: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[BaseException | None] | None = None
        super().__init__(
            body,
            status_code=status_code,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **(headers or {}),
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Own ASGI sending so failed headers and disconnects release the reader."""
        if self._serving is not None or self._closing is not None:
            raise GatewayClosed("SSE response is single-use")
        self._serving = asyncio.create_task(
            super().__call__(scope, receive, send), name="tinkerfin-gateway-sse-send"
        )
        primary: BaseException | None = None
        try:
            await self._serving
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

    async def aclose(self) -> None:
        """Discard unsent output or stop sending, then settle all owned cleanup."""
        if self._closing is None:
            self._closing = asyncio.create_task(
                _capture_cleanup(self._close()), name="tinkerfin-gateway-sse-close"
            )
        await _join_cleanup(self._closing)

    async def _close(self) -> None:
        if self._serving is not None:
            if not self._serving.done():
                self._serving.cancel()
            # The sending caller observes its own failure. Cleanup must still
            # release both an unused renderer and a pending subscription pull.
            await asyncio.gather(self._serving, return_exceptions=True)
        primary: BaseException | None = None
        try:
            await self._source.aclose()
        except BaseException as error:  # noqa: BLE001 - still close the renderer after a failed reader cleanup
            primary = error
        try:
            await self._body.aclose()
        except BaseException as error:
            if primary is not None:
                raise _select_failure(primary, error)
            raise
        if primary is not None:
            raise primary


async def sse_response(
    stream: _Source | Awaitable[_Source],
    *,
    headers: Mapping[str, str] | None = None,
) -> SseResponse:
    """Preflight and transfer a Gateway stream into a closeable HTTP response.

    Example:
        ``return await sse_response(gateway.stream(runtime, command))``

    Admission, authorization, and cursor failures happen before sending headers.
    The response owns cleanup even if its body never starts. The host still owns
    routing, authentication, error responses, and authorization scope selection.
    """
    source = await stream if inspect.isawaitable(stream) else stream
    try:
        body = source.to_sse()
        return SseResponse(source, body, headers)
    except BaseException as error:
        try:
            await _close_owned(source.aclose())
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and independent cleanup failures
            raise _select_failure(error, cleanup_error)
        raise


__all__ = ["SseResponse", "sse_response"]
