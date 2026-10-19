"""Connect the public MCP transport contract to an owned native process scope."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

import anyio
import mcp_types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import Client
from mcp.shared.message import SessionMessage
from pydantic import ConfigDict, JsonValue, TypeAdapter

from ._process import finish_owned
from .errors import LocalError, LocalErrorCode

if TYPE_CHECKING:
    from .session import LocalSession

_Value = TypeVar("_Value")
_json: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(
    dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False)
)
_FRAME_BYTES = 1024 * 1024
_Streams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]


@asynccontextmanager
async def _transport(
    session: LocalSession, arguments: Sequence[str]
) -> AsyncGenerator[_Streams]:
    # MCP 2.3.0 Client accepts a public Transport context. Only newline framing
    # lives here; the SDK owns JSON-RPC dispatch, initialization and MCP semantics.
    async with session.open_process(arguments) as process:
        incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](
            8
        )
        write, outgoing = anyio.create_memory_object_stream[SessionMessage](8)
        received = 0

        def count(size: int) -> None:
            nonlocal received
            received += size
            if received > session._limits.output_bytes:
                raise LocalError(
                    LocalErrorCode.LIMIT, "MCP output exceeds the workspace limit"
                )

        async def receive() -> None:
            buffered = bytearray()
            async with incoming:
                while chunk := await process.stdout.read(64 * 1024):
                    count(len(chunk))
                    buffered.extend(chunk)
                    while b"\n" in buffered:
                        line, _delimiter, tail = buffered.partition(b"\n")
                        buffered = bytearray(tail)
                        if len(line) > _FRAME_BYTES:
                            raise LocalError(
                                LocalErrorCode.LIMIT,
                                "MCP message exceeds its byte limit",
                            )
                        message = mcp_types.jsonrpc_message_adapter.validate_json(
                            bytes(line), by_name=False
                        )
                        await incoming.send(SessionMessage(message))
                    if len(buffered) > _FRAME_BYTES:
                        raise LocalError(
                            LocalErrorCode.LIMIT, "MCP message exceeds its byte limit"
                        )
                if buffered:
                    raise LocalError(
                        LocalErrorCode.PROTOCOL, "MCP output ended inside a message"
                    )

        async def send() -> None:
            async with outgoing:
                async for item in outgoing:
                    payload = item.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    ).encode()
                    if len(payload) > _FRAME_BYTES:
                        raise LocalError(
                            LocalErrorCode.LIMIT, "MCP request exceeds its byte limit"
                        )
                    process.stdin.write(payload + b"\n")
                    await process.stdin.drain()

        async def diagnostics() -> None:
            # Server diagnostics are untrusted and can contain secrets. Drain
            # within the shared byte budget without publishing their contents.
            while chunk := await process.stderr.read(64 * 1024):
                count(len(chunk))

        async with read, write, anyio.create_task_group() as group:
            group.start_soon(receive)
            group.start_soon(send)
            group.start_soon(diagnostics)
            try:
                yield read, write
            finally:
                group.cancel_scope.cancel()
        process.stdin.close()
        # A successful exchange permits a bounded graceful server shutdown.
        # Cancellation skips this wait and the enclosing process scope stops it.
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            pass


def _known_failure(error: BaseException) -> LocalError | None:
    if isinstance(error, LocalError):
        return error
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        for child in group.exceptions:
            if found := _known_failure(child):
                return found
    return None


async def _invoke(
    session: LocalSession,
    arguments: Sequence[str],
    timeout: float,
    operation: Callable[[Client], Awaitable[_Value]],
) -> _Value:
    async def run() -> _Value:
        try:
            async with asyncio.timeout(timeout):
                async with Client(_transport(session, arguments), cache=None) as client:
                    return await operation(client)
        except TimeoutError as error:
            raise LocalError(
                LocalErrorCode.TIMEOUT, "MCP operation timed out", cause=error
            ) from error
        except Exception as error:
            if known := _known_failure(error):
                raise known
            raise LocalError(
                LocalErrorCode.PROTOCOL, "MCP operation could not complete", cause=error
            ) from error

    # One owned task keeps SDK cancel scopes on their entering task. Repeated
    # caller cancellation never interrupts that task's accepted cleanup.
    running = asyncio.create_task(run())
    try:
        await asyncio.wait({running})
        return running.result()
    except asyncio.CancelledError as cancellation:
        running.cancel()
        try:
            await finish_owned(running)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            raise cancellation from error
        raise


async def list_tools(
    session: LocalSession, arguments: Sequence[str], timeout: float
) -> tuple[dict[str, JsonValue], ...]:
    async def collect(client: Client) -> tuple[dict[str, JsonValue], ...]:
        result: list[dict[str, JsonValue]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(32):
            page = await client.list_tools(cursor=cursor)
            result.extend(
                _json.validate_python(
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
                for tool in page.tools
            )
            if len(result) > 256:
                raise LocalError(
                    LocalErrorCode.LIMIT, "MCP tool catalog exceeds its capacity"
                )
            cursor = page.next_cursor
            if cursor is None:
                return tuple(result)
            if cursor in seen:
                raise LocalError(LocalErrorCode.PROTOCOL, "MCP tool cursor repeated")
            seen.add(cursor)
        raise LocalError(
            LocalErrorCode.LIMIT, "MCP tool catalog exceeds its page limit"
        )

    return await _invoke(session, arguments, timeout, collect)


async def call_tool(
    session: LocalSession,
    command: Sequence[str],
    name: str,
    arguments: dict[str, JsonValue],
    timeout: float,
) -> dict[str, JsonValue]:
    if not name or len(name) > 128:
        raise ValueError("MCP tool name must contain 1 to 128 characters")
    if len(json.dumps(arguments, allow_nan=False).encode()) > _FRAME_BYTES - 4096:
        raise LocalError(LocalErrorCode.LIMIT, "MCP tool input exceeds its byte limit")

    async def call(client: Client) -> dict[str, JsonValue]:
        result = await client.call_tool(name, arguments)
        return _json.validate_python(
            result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    return await _invoke(session, command, timeout, call)
