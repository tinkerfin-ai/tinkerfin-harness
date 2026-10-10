"""Verify lifecycle ownership through the installed native OpenSandbox SDK."""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx


class _ServiceTransport(httpx.AsyncBaseTransport):
    """Serve the SDK's actual HTTP contract without contacting a Sandbox."""

    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.closed = False
        self.mutation_status = 202
        self.mutation_started = asyncio.Event()
        self.mutation_release = asyncio.Event()
        self.mutation_release.set()
        self.mutation_cancelled = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[request.method, path] += 1
        if path.endswith(("/pause", "/resume")):
            self.mutation_started.set()
            try:
                await self.mutation_release.wait()
            except asyncio.CancelledError:
                self.mutation_cancelled = True
                raise
            return httpx.Response(
                self.mutation_status,
                json={"code": "SYNTHETIC", "message": "Synthetic response"},
            )
        if "/endpoints/" in path:
            return httpx.Response(200, json={"endpoint": "audit.invalid:9999"})
        if request.method == "DELETE":
            return httpx.Response(404, json={"code": "MISSING", "message": "Missing"})
        if path == "/v1/sandboxes" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "pagination": {
                        "page": 1,
                        "pageSize": 2,
                        "totalItems": 0,
                        "totalPages": 0,
                        "hasNextPage": False,
                    },
                },
            )
        if path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(202, json=_sandbox_info("bad"))
        if "/diagnostics/" in path:
            kind = "logs" if path.endswith("/logs") else "events"
            return httpx.Response(
                200,
                json={
                    "sandboxId": "existing",
                    "kind": kind,
                    "scope": request.url.params.get("scope"),
                    "delivery": "inline",
                    "contentType": "text/plain",
                    "truncated": False,
                    "content": "Synthetic diagnostic content",
                    "warnings": None,
                },
            )
        if path.startswith("/v1/sandboxes/") and path.count("/") == 3:
            return httpx.Response(200, json=_sandbox_info(path.rsplit("/", 1)[1]))
        if path == "/command":
            return httpx.Response(503, json={"code": "BUSY", "message": "Busy"})
        return httpx.Response(200, json={})

    async def aclose(self) -> None:
        self.closed = True


def _sandbox_info(sandbox_id: str) -> dict[str, object]:
    return {
        "id": sandbox_id,
        "status": {"state": "Running"},
        "createdAt": "2026-09-07T00:00:00Z",
        "entrypoint": ["sleep", "infinity"],
        "metadata": {"tinkerfin.ai/purpose": "commands"},
    }
