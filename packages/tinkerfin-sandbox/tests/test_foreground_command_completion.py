"""Foreground terminal events settle commands independently of HTTP stream EOF."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from test_sdk_upgrade import _ServiceTransport

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendUnavailableError,
)


class _CommandBody(httpx.AsyncByteStream):
    def __init__(self, *, exit_code: int | None = 0) -> None:
        self.exit_code = exit_code
        self.tail_requested = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.failure: Exception | None = None
        self.close_failure = False
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        events: list[dict[str, object]] = [
            {"type": "init", "text": "command-1", "timestamp": 1},
            {"type": "stdout", "text": "first", "timestamp": 2},
            {"type": "stdout", "text": "last", "timestamp": 3},
            {"type": "stderr", "text": "notice", "timestamp": 4},
        ]
        if self.exit_code:
            events.append(
                {
                    "type": "error",
                    "timestamp": 5,
                    "error": {"ename": "CommandError", "evalue": str(self.exit_code)},
                }
            )
        if self.exit_code is not None:
            events.append({"type": "execution_complete", "timestamp": 6})
        for event in events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        self.tail_requested.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.close_entered.set()
        try:
            await self.close_release.wait()
        finally:
            self.closed = True
        if self.close_failure:
            raise httpx.ReadError("Controlled stream close failure")


class _Commands(_ServiceTransport):
    def __init__(self, body: _CommandBody) -> None:
        super().__init__()
        self.body = body
        self.running = body.exit_code is None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/command" and request.method == "POST":
            self.calls[request.method, path] += 1
            assert json.loads(request.content).get("background", False) is False
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=self.body
            )
        if path == "/command/status/command-1":
            self.calls[request.method, path] += 1
            return httpx.Response(
                200,
                json={
                    "id": "command-1",
                    "running": self.running,
                    "exit_code": None if self.running else self.body.exit_code or 130,
                },
            )
        if path == "/command" and request.method == "DELETE":
            self.calls[request.method, path] += 1
            self.running = False
            return httpx.Response(200)
        return await super().handle_async_request(request)


@asynccontextmanager
async def _backend(
    body: _CommandBody,
) -> AsyncIterator[tuple[OpenSandboxBackend, _Commands]]:
    transport = _Commands(body)
    sandbox = await Sandbox.connect(
        "existing",
        skip_health_check=True,
        connection_config=ConnectionConfig(domain="audit.invalid", transport=transport),
    )
    backend = OpenSandboxBackend(sandbox=sandbox)
    try:
        yield backend, transport
    finally:
        body.release.set()
        await backend.aclose()


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_confirmed_foreground_completion_closes_stream_without_waiting_for_eof(
    exit_code: int,
) -> None:
    body = _CommandBody(exit_code=exit_code)
    async with _backend(body) as (backend, transport):
        task = asyncio.create_task(backend.aexecute("synthetic command"))
        tail = asyncio.create_task(body.tail_requested.wait())
        try:
            await asyncio.wait((task, tail), return_when=asyncio.FIRST_COMPLETED)
            assert task.done(), "A confirmed foreground command must not wait for EOF"
            result = await task
            assert result.exit_code == exit_code
            assert result.output == "first\nlast\n<stderr>notice</stderr>"
            assert body.closed
            assert not body.tail_requested.is_set()
            assert transport.calls["GET", "/command/status/command-1"] == 0
            assert transport.calls["DELETE", "/command"] == 0
        finally:
            body.release.set()
            tail.cancel()
            await asyncio.gather(task, tail, return_exceptions=True)


@pytest.mark.parametrize("ending", ["cancel", "disconnect", "timeout", "eof"])
async def test_unconfirmed_command_retains_remote_settlement(
    ending: Literal["cancel", "disconnect", "timeout", "eof"],
) -> None:
    body = _CommandBody(exit_code=None)
    async with _backend(body) as (backend, transport):
        task = asyncio.create_task(backend.aexecute("synthetic command"))
        try:
            await body.tail_requested.wait()
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif ending == "eof":
                body.release.set()
                result = await task
                assert result.exit_code is None
                assert result.output == "first\nlast\n<stderr>notice</stderr>"
            else:
                body.failure = (
                    httpx.ReadTimeout("Controlled timeout")
                    if ending == "timeout"
                    else httpx.ReadError("Controlled disconnect")
                )
                body.release.set()
                with pytest.raises(OpenSandboxBackendUnavailableError):
                    await task
            await backend.aclose()
            assert body.closed
            assert not transport.running
            assert transport.calls["DELETE", "/command"] == 1
        finally:
            body.release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_foreground_completion_does_not_hide_stream_close_failure() -> None:
    body = _CommandBody()
    body.close_failure = True
    body.release.set()
    async with _backend(body) as (backend, transport):
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await backend.aexecute("synthetic command")
        await backend.aclose()
        assert body.closed
        assert transport.calls["DELETE", "/command"] == 0


async def test_cancellation_during_confirmed_response_close_remains_cancellation() -> (
    None
):
    body = _CommandBody()
    body.close_release.clear()
    async with _backend(body) as (backend, transport):
        task = asyncio.create_task(backend.aexecute("synthetic command"))
        try:
            await body.close_entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await backend.aclose()
            assert body.closed
            assert transport.calls["DELETE", "/command"] == 0
        finally:
            body.close_release.set()
            await asyncio.gather(task, return_exceptions=True)
