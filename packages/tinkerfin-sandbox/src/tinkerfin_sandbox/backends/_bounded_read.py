"""Read execd binary responses with bounded retention and explicit ownership."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from opensandbox import Sandbox
from opensandbox.exceptions import SandboxApiException
from opensandbox.transport import unwrap_retry_transport

from ..errors import OpenSandboxBackendProtocolError, OpenSandboxFileTooLargeError

_ReadT = TypeVar("_ReadT")


async def await_owned_read(read: Callable[[], Awaitable[_ReadT]]) -> _ReadT:
    """Cancel a read once and await cleanup despite repeated caller cancellation."""

    async def run() -> _ReadT:
        return await read()

    task = asyncio.create_task(run(), name="tinkerfin-binary-read")
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
                task.cancel()
        except BaseException:  # noqa: BLE001 - consume the owned result below
            break
    if cancellation is not None:
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                cancellation.add_note(
                    f"Binary read cleanup failed: {type(failure).__name__}"
                )
        raise cancellation
    return task.result()


async def _close_response(response: httpx.Response) -> None:
    """Complete bounded response cleanup even when the read deadline expires here."""

    async def close() -> None:
        async with asyncio.timeout(5):
            await response.aclose()

    task = asyncio.create_task(close(), name="tinkerfin-binary-response-close")
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:  # noqa: BLE001 - consume the owned result below
            break
    if cancellation is not None:
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                cancellation.add_note(
                    f"Response cleanup failed: {type(failure).__name__}"
                )
        raise cancellation
    task.result()


def validate_read_limits(max_bytes: int, timeout: float) -> None:
    """Keep the deadline within the descriptor helper's finite lifetime."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if not math.isfinite(timeout) or not 0 < timeout <= 290:
        raise ValueError("timeout must be finite and between 0 and 290 seconds")


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Let each read close its client without closing the Sandbox connection."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)


async def read_binary(sandbox: Sandbox, path: str, *, max_bytes: int) -> bytes:
    """Own one HTTP response, including early rejection and interrupted reads.

    OpenSandbox 0.1.16 FilesystemAdapter.read_bytes_stream returns a bare
    Response.aiter_bytes iterator: early iterator close does not close its response,
    and error bodies are read without a bound. Use the same public execd endpoint,
    headers and download route while owning the response context explicitly.
    Streaming bypasses the SDK retry transport, as its streaming adapters do.
    """
    endpoint = await sandbox.get_endpoint(44772)
    config = sandbox.connection_config
    transport = config.transport
    async with httpx.AsyncClient(
        transport=(
            _BorrowedTransport(unwrap_retry_transport(transport))
            if transport is not None
            else None
        ),
        timeout=config.request_timeout.total_seconds(),
        headers={
            "User-Agent": config.user_agent,
            **config.headers,
            **endpoint.headers,
            "Accept-Encoding": "identity",
        },
        follow_redirects=False,
    ) as client:
        request = client.build_request(
            "GET",
            f"{config.protocol}://{endpoint.endpoint}/files/download",
            params={"path": path},
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code == 404:
                raise FileNotFoundError("Sandbox file was not found")
            if response.status_code == 403:
                raise PermissionError("Sandbox file access was denied")
            if response.status_code != 200:
                raise SandboxApiException(
                    "Sandbox file download was rejected",
                    status_code=response.status_code,
                )
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise OpenSandboxBackendProtocolError(
                    "Sandbox binary downloads require identity encoding"
                )
            content = bytearray()
            # Iterate the raw async transport body: HTTPX aiter_raw() closes on
            # exhaustion inside the read deadline, which could interrupt cleanup.
            # This explicit owner performs all closure under its separate deadline.
            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise OpenSandboxBackendProtocolError(
                    "Sandbox returned a synchronous body"
                )
            async for chunk in response.stream:
                if len(chunk) > max_bytes - len(content):
                    raise OpenSandboxFileTooLargeError(
                        "Sandbox file exceeds the byte limit",
                        context={"max_bytes": max_bytes},
                    )
                content.extend(chunk)
            return bytes(content)
        finally:
            # The outer read task receives at most one cancellation. Cleanup has
            # its own deadline and cannot be interrupted by a second caller cancel.
            await _close_response(response)
