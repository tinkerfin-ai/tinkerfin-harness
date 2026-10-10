"""Bound workspace readiness through the installed SDK without external resources."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import timedelta
from types import ModuleType, TracebackType
from typing import Literal

import httpx
from opensandbox.config import ConnectionConfig
from test_sdk_upgrade import _sandbox_info, _ServiceTransport

from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
)


class _Deadline:
    """Expire only after the test explicitly signals this exact deadline."""

    def __init__(self, clock: _Clock, when: float) -> None:
        self.clock = clock
        self.when = when
        self.owner: asyncio.Task[object] | None = None
        self.cancel_count = 0
        self.triggered = False

    async def __aenter__(self) -> _Deadline:
        self.owner = asyncio.current_task()
        assert self.owner is not None
        self.cancel_count = self.owner.cancelling()
        self.clock.deadlines.append(self)
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del error_type, traceback
        if self.triggered and isinstance(error, asyncio.CancelledError):
            assert self.owner is not None
            if self.owner.uncancel() <= self.cancel_count:
                raise TimeoutError("Explicit readiness deadline") from error
        return False

    def expired(self) -> bool:
        return self.triggered

    def expire(self) -> None:
        assert self.owner is not None
        self.clock.now = self.when
        self.triggered = True
        self.owner.cancel()


class _Clock:
    """Drive deadlines and readiness intervals without a wall-clock timer."""

    def __init__(self) -> None:
        self.now = 100.0
        self.deadlines: list[_Deadline] = []
        self.delays: list[float] = []
        self.poll_started = asyncio.Event()
        self.poll_release = asyncio.Event()
        self.poll_release.set()

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.poll_started.set()
        await self.poll_release.wait()
        self.now += seconds

    def module(self) -> ModuleType:
        controlled = ModuleType("controlled_readiness_asyncio")
        controlled.__dict__.update(vars(asyncio))
        setattr(controlled, "get_running_loop", lambda: self)
        setattr(controlled, "timeout_at", lambda when: _Deadline(self, when))
        setattr(controlled, "sleep", self.sleep)
        return controlled


class _ReadinessBody(httpx.AsyncByteStream):
    """Expose body cancellation and close independently of response headers."""

    def __init__(self, service: _WorkspaceService, payload: str) -> None:
        self.service = service
        self.payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.service.body_started.set()
        if self.service.block == "body":
            await self.service.release.wait()
        if self.service.command_error:
            yield (
                b'data: {"type":"error","timestamp":1,"error":{"ename":"RuntimeError",'
                b'"evalue":"Synthetic command failure"}}\n\n'
            )
        for payload in (
            {"type": "init", "text": "readiness-command", "timestamp": 1},
            {"type": "stdout", "text": self.payload, "timestamp": 2},
            {"type": "execution_complete", "timestamp": 3},
        ):
            yield ("data: " + json.dumps(payload) + "\n\n").encode()

    async def aclose(self) -> None:
        self.service.body_closed.set()


class _WorkspaceService(_ServiceTransport):
    """Serve exact SDK lifecycle endpoints and a controllable readiness command."""

    def __init__(self, block: Literal["headers", "body", "none"] = "none") -> None:
        super().__init__()
        self.block = block
        self.payload = '{"ready":true}'
        self.command_started = asyncio.Event()
        self.body_started = asyncio.Event()
        self.body_closed = asyncio.Event()
        self.headers_cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.metadata = {"tinkerfin.ai/purpose": "workspaces"}
        self.statuses: deque[int] = deque()
        self.failures: deque[Exception] = deque()
        self.payloads: deque[str] = deque()
        self.responses: list[httpx.Response] = []
        self.create_started = asyncio.Event()
        self.create_release = asyncio.Event()
        self.create_release.set()
        self.create_cancelled = False
        self.create_failure: Exception | None = None
        self.clock: _Clock | None = None
        self.endpoint_cost = 0.0
        self.info_cost = 0.0
        self.delete_status = 204
        self.command_error = False
        self.delete_started = asyncio.Event()
        self.delete_release = asyncio.Event()
        self.delete_release.set()
        self.delete_cancelled = False
        self.discovered_ids: tuple[str, ...] = ("owned-sandbox",)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/sandboxes":
            self.calls[request.method, path] += 1
            body = json.loads(request.content)
            self.metadata = body["metadata"]
            self.create_started.set()
            try:
                await self.create_release.wait()
            except asyncio.CancelledError:
                self.create_cancelled = True
                raise
            if self.create_failure is not None:
                raise self.create_failure
            return httpx.Response(
                202,
                json={**_sandbox_info("owned-sandbox"), "metadata": self.metadata},
            )
        if request.method == "DELETE":
            self.calls[request.method, path] += 1
            self.delete_started.set()
            try:
                await self.delete_release.wait()
            except asyncio.CancelledError:
                self.delete_cancelled = True
                raise
            return httpx.Response(
                self.delete_status,
                json={"code": "SYNTHETIC", "message": "Synthetic delete response"},
            )
        if path == "/v1/sandboxes" and request.method == "GET":
            self.calls[request.method, path] += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {**_sandbox_info(sandbox_id), "metadata": self.metadata}
                        for sandbox_id in self.discovered_ids
                    ],
                    "pagination": {
                        "page": 1,
                        "pageSize": 2,
                        "totalItems": len(self.discovered_ids),
                        "totalPages": 1,
                        "hasNextPage": False,
                    },
                },
            )
        if "/endpoints/" in path and path.endswith("/44772") and self.clock is not None:
            self.clock.now += self.endpoint_cost
        if path.startswith("/v1/sandboxes/") and path.count("/") == 3:
            self.calls[request.method, path] += 1
            if self.clock is not None:
                self.clock.now += self.info_cost
            return httpx.Response(
                200,
                json={
                    **_sandbox_info(path.rsplit("/", 1)[1]),
                    "metadata": self.metadata,
                },
            )
        if path == "/command":
            self.calls[request.method, path] += 1
            self.command_started.set()
            if self.failures:
                raise self.failures.popleft()
            status = self.statuses.popleft() if self.statuses else 200
            if status != 200:
                response = httpx.Response(
                    status,
                    json={"code": "REJECTED", "message": "synthetic-private-detail"},
                )
                self.responses.append(response)
                return response
            if self.block == "headers":
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.headers_cancelled.set()
                    raise
            response = httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=_ReadinessBody(
                    self, self.payloads.popleft() if self.payloads else self.payload
                ),
            )
            self.responses.append(response)
            return response
        return await super().handle_async_request(request)


def _client(
    service: _WorkspaceService | None,
    *,
    ready_seconds: float = 4,
    connect_seconds: float = 5,
) -> OpenSandboxClient:
    return OpenSandboxClient(
        connection_config=ConnectionConfig(domain="audit.invalid", transport=service),
        config=OpenSandboxConfig(
            workspace_root=None,
            ready_timeout=timedelta(seconds=ready_seconds),
            connect_timeout=timedelta(seconds=connect_seconds),
            warm_pool_size=0,
        ),
    )


def _causes(error: BaseException) -> list[BaseException]:
    pending = [error]
    found: list[BaseException] = []
    while pending:
        current = pending.pop()
        if any(current is known for known in found):
            continue
        found.append(current)
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return found
