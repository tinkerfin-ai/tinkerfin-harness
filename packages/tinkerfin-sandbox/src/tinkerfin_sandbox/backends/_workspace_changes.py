"""Read bounded change hints from an authenticated workspace collector."""

from __future__ import annotations

__all__ = ["_open_changes"]

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ..errors import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
)
from ._bounded_read import _close_response

_LINE_LIMIT = 4096
_HEARTBEAT_TIMEOUT = 45.0


class _WorkspaceReady(BaseModel):
    """Fix the collector and project identities before exposing a subscription."""

    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["ready"]
    source: str = Field(description="Canonical UUID fixed for one collector process")
    incarnation: str = Field(
        description="Canonical UUID fixed for one project instance"
    )

    @field_validator("source", "incarnation")
    @classmethod
    def canonical_identity(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("workspace change identities must be canonical UUIDs")
        return value


class _WorkspaceSignal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["changed", "heartbeat", "closed"]


class _WorkspaceResync(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["resync"]
    reason: Literal["topology", "overflow", "source_closed"]


_WorkspaceEvent = _WorkspaceReady | _WorkspaceSignal | _WorkspaceResync
_EVENT: TypeAdapter[_WorkspaceEvent] = TypeAdapter(_WorkspaceEvent)


async def _lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """Retain at most one bounded line and the current transport chunk."""
    if not isinstance(response.stream, httpx.AsyncByteStream):
        raise OpenSandboxBackendProtocolError(
            "Workspace changes require an asynchronous response"
        )
    pending = bytearray()
    async for chunk in response.stream:
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            if end - offset > _LINE_LIMIT - len(pending):
                raise OpenSandboxBackendProtocolError(
                    "Workspace change event exceeds its byte limit"
                )
            pending.extend(memoryview(chunk)[offset:end])
            if newline < 0:
                break
            yield bytes(pending)
            pending.clear()
            offset = end + 1
    if pending:
        raise OpenSandboxBackendProtocolError(
            "Workspace change stream ended inside an event"
        )


async def _next_line(lines: AsyncIterator[bytes], *, timeout: float) -> bytes:
    """Bound silence independently of consumer speed and reset only after a line."""
    async with asyncio.timeout(timeout):
        return await anext(lines)


async def _events(
    response: httpx.Response, *, ready_deadline: float
) -> AsyncGenerator[_WorkspaceEvent, None]:
    """Parse events without taking ownership of the enclosing response."""
    async with asyncio.timeout_at(ready_deadline):
        if response.status_code == 409:
            raise OpenSandboxBusyError("Workspace deletion is in progress")
        if response.status_code != 200:
            raise OpenSandboxBackendUnavailableError(
                "Workspace changes are unavailable for the selected project",
                context={"status": response.status_code},
            )
        if (
            response.headers.get("Content-Encoding", "identity") != "identity"
            or response.headers.get("Content-Type", "").split(";", 1)[0]
            != "application/x-ndjson"
        ):
            raise OpenSandboxBackendProtocolError(
                "Workspace changes require an uncompressed NDJSON response"
            )
        lines = _lines(response)
        try:
            ready = _WorkspaceReady.model_validate_json(await anext(lines))
        except StopAsyncIteration as error:
            raise OpenSandboxBackendProtocolError(
                "Workspace changes ended before becoming ready", cause=error
            ) from error
    yield ready
    while True:
        try:
            payload = await _next_line(lines, timeout=_HEARTBEAT_TIMEOUT)
        except StopAsyncIteration:
            return
        event = _EVENT.validate_json(payload)
        if isinstance(event, _WorkspaceReady):
            raise OpenSandboxBackendProtocolError(
                "Workspace changes cannot select another source"
            )
        yield event
        if event.type == "closed" or (
            isinstance(event, _WorkspaceResync) and event.reason == "source_closed"
        ):
            return


@asynccontextmanager
async def _open_changes(
    client: httpx.AsyncClient, project: str, *, request_timeout: float
) -> AsyncGenerator[AsyncIterator[_WorkspaceEvent], None]:
    """Own one fixed stream through execd's authenticated loopback proxy.

    The collector arms its existing root before ``ready``. Its 15-second
    heartbeats make a paused or disconnected parent observable without polling
    files, reconnecting, or holding a workspace execution lease. The protocol is
    exercised by ``test_workspace_changes``; every line has a strict byte bound.
    """
    request = client.build_request(
        "GET",
        f"/proxy/44773/projects/{project}/changes",
        timeout=httpx.Timeout(
            connect=client.timeout.connect,
            read=None,
            write=client.timeout.write,
            pool=client.timeout.pool,
        ),
    )
    deadline = asyncio.get_running_loop().time() + request_timeout
    async with asyncio.timeout_at(deadline):
        response = await client.send(request, stream=True)
    events = _events(response, ready_deadline=deadline)
    try:
        yield events
    finally:
        try:
            await events.aclose()
        finally:
            await _close_response(response)
