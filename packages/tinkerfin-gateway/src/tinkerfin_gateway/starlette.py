"""Optional Starlette responses with explicit ownership of prepared streams."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Mapping
from typing import TypeAlias

from ag_ui.core import BaseEvent
from anyio import CancelScope
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from tinkerfin_messaging import MessageSubscription

from ._cleanup import _capture_cleanup, _close_owned, _join_cleanup
from ._failures import _cancellation_only, _select_failure
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
        self._serving: asyncio.Task[BaseException | None] | None = None
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
            self._serve(scope, receive, send), name="tinkerfin-gateway-sse-send"
        )
        primary: BaseException | None = None
        try:
            primary = await asyncio.shield(self._serving)
        except asyncio.CancelledError as error:
            primary = error
        try:
            await self.aclose()
        except BaseException as error:  # noqa: BLE001 - deliver all failures after the reader is closed
            primary = error if primary is None else _select_failure(primary, error)
        if not self._serving.cancelled():
            outcome = self._serving.result()
            if outcome is not None:
                primary = (
                    outcome if primary is None else _select_failure(primary, outcome)
                )
        if primary is not None:
            raise primary

    async def _serve(
        self, scope: Scope, receive: Receive, send: Send
    ) -> BaseException | None:
        # Return control exceptions as data: asyncio tasks otherwise deliver
        # SystemExit/KeyboardInterrupt to the loop before their owner can clean up.
        try:
            spec = tuple(
                map(int, scope.get("asgi", {}).get("spec_version", "2.0").split("."))
            )
            if scope["type"] == "http" and spec < (2, 4):
                await self._serve_with_disconnect(receive, send)
                if self.background is not None:
                    await self.background()
            else:
                await super().__call__(scope, receive, send)
        except BaseException as error:  # noqa: BLE001 - the ASGI caller restores process control after cleanup
            return error
        return None

    async def _serve_with_disconnect(self, receive: Receive, send: Send) -> None:
        # Starlette StreamingResponse races sending against disconnect for ASGI
        # <2.4. Native tasks preserve that contract while delivering cancellation
        # once, so an authorization callback can return borrowed resources.
        # See test_disconnect_settles_authorization_resource_cleanup.
        async def sending() -> BaseException | None:
            return await _capture_cleanup(self.stream_response(send))

        async def listening() -> BaseException | None:
            return await _capture_cleanup(self.listen_for_disconnect(receive))

        sender = asyncio.create_task(sending(), name="tinkerfin-gateway-sse-body")
        listener = asyncio.create_task(listening(), name="tinkerfin-gateway-disconnect")
        primary: BaseException | None = None
        stopped: set[asyncio.Task[BaseException | None]] = set()
        try:
            await asyncio.wait((sender, listener), return_when=asyncio.FIRST_COMPLETED)
        except BaseException as error:  # noqa: BLE001 - join both children before restoring cancellation
            primary = error
        finally:
            for task in (sender, listener):
                if not task.done():
                    stopped.add(task)
                    task.cancel()
            # A request can be cancelled after disconnect has already started
            # child cleanup. Joining must never deliver a second cancellation to
            # those children or lose their independent failure outcomes.
            settled = asyncio.gather(sender, listener, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError as error:
                    primary = (
                        error if primary is None else _select_failure(primary, error)
                    )
            outcomes = settled.result()
        for task, outcome in zip((sender, listener), outcomes, strict=True):
            if outcome is None:
                continue
            # Starlette treats a cancelled sender as a completed disconnect.
            # A receive-side cancellation is suppressed only when we issued it.
            if (task is sender or task in stopped) and _cancellation_only(outcome):
                continue
            primary = outcome if primary is None else _select_failure(primary, outcome)
        if primary is not None:
            raise primary

    async def aclose(self) -> None:
        """Discard unsent output or stop sending, then settle all owned cleanup."""
        if self._closing is None:
            self._closing = asyncio.create_task(
                _capture_cleanup(self._close()), name="tinkerfin-gateway-sse-close"
            )
        # The caller may itself live in an AnyIO level-cancellation scope. Shield
        # only settlement; _join_cleanup still preserves explicit task cancels.
        with CancelScope(shield=True):
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
    request: Request | None = None,
) -> SseResponse:
    """Preflight and transfer a Gateway stream into a closeable HTTP response.

    Example:
        ``return await sse_response(gateway.stream(runtime, command))``

    Admission, authorization, and cursor failures happen before sending headers.
    The response owns cleanup even if its body never starts. The host still owns
    routing, authentication, error responses, and authorization scope selection.
    Pass ``request`` after reading its body to also cancel admission on client
    disconnect. A response prepared concurrently with disconnect is closed here.

    Args:
        stream: Prepared stream or awaitable whose resulting stream is transferred
            to this response. Admission failures remain observable by the host.
        headers: Additional response headers, overriding the default SSE headers.
        request: Borrowed HTTP request with its body already consumed. Its
            disconnect monitor ends before the response starts sending. Omitting
            it leaves admission cancellation to the host. Disconnect releases
            readers without cancelling an accepted durable run.

    Returns:
        A single-use response owning the prepared stream and its renderer.

    Raises:
        asyncio.CancelledError: Admission is cancelled or the provided request
            disconnects before handoff. Accepted readers are closed first.
    """
    caller = asyncio.current_task()
    stopping = False
    watching = False

    async def watch_disconnect() -> BaseException | None:
        nonlocal watching
        if stopping:
            return None
        watching = True
        try:
            assert request is not None
            while not await request.is_disconnected():
                await asyncio.sleep(0.1)
            failure: BaseException = asyncio.CancelledError("HTTP client disconnected")
        except BaseException as error:  # noqa: BLE001 - return transport failures to the admitting caller
            if stopping and _cancellation_only(error):
                return None
            failure = error
        assert caller is not None
        caller.cancel()
        return failure

    disconnect = (
        asyncio.create_task(watch_disconnect(), name="tinkerfin-gateway-sse-preflight")
        if request is not None
        else None
    )
    source: _Source | None = None
    response: SseResponse | None = None
    failure: BaseException | None = None
    try:
        source = await stream if inspect.isawaitable(stream) else stream
        body = source.to_sse()
        response = SseResponse(source, body, headers)
        if request is not None and await request.is_disconnected():
            raise asyncio.CancelledError("HTTP client disconnected")
    except BaseException as error:  # noqa: BLE001 - settle the monitor and any adopted source before delivery
        failure = error
    finally:
        if disconnect is not None:
            stopping = True
            if watching and not disconnect.done():
                disconnect.cancel()
            try:
                with CancelScope(shield=True):
                    await _join_cleanup(disconnect)
            except BaseException as error:  # noqa: BLE001 - cancellation during handoff must still close the response
                failure = error if failure is None else _select_failure(failure, error)
    # A completed Future can own a source before its awaiting task resumes. Task
    # cancellation at that handoff must adopt and close the result as well.
    if (
        source is None
        and isinstance(stream, asyncio.Future)
        and stream.done()
        and not stream.cancelled()
    ):
        try:
            source = stream.result()
        except BaseException as error:  # noqa: BLE001 - retrieve an accepted awaitable's original failure
            failure = error if failure is None else _select_failure(failure, error)
    if failure is not None:
        try:
            with CancelScope(shield=True):
                if response is not None:
                    await _close_owned(response.aclose())
                elif source is not None:
                    await _close_owned(source.aclose())
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve control and independent cleanup failures
            failure = _select_failure(failure, cleanup_error)
        raise failure
    assert response is not None
    return response


__all__ = ["SseResponse", "sse_response"]
