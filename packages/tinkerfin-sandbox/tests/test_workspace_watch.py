"""Project-bound notification admission, ownership, and independent readers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import JsonValue
from test_pause_resume import _Client, _ControlledState, _World, _world
from test_workspace_lifecycle import _WorkspaceRemote

from tinkerfin_notifications import (
    Notifications,
)
from tinkerfin_sandbox import (
    OpenSandboxManager,
    OpenSandboxOwnerClaim,
)


class _Changes(httpx.AsyncByteStream):
    def __init__(self, source: str, incarnation: str, ready: asyncio.Event) -> None:
        self.source = source
        self.incarnation = incarnation
        self.ready = ready
        self.pending: asyncio.Queue[dict[str, JsonValue] | None] = asyncio.Queue(16)
        self.closed = asyncio.Event()
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.ready.wait()
        yield (
            json.dumps(
                {
                    "type": "ready",
                    "source": self.source,
                    "incarnation": self.incarnation,
                }
            ).encode()
            + b"\n"
        )
        while True:
            self.waiting.set()
            event = await self.pending.get()
            if event is None:
                return
            yield json.dumps(event).encode() + b"\n"

    async def aclose(self) -> None:
        self.closed.set()


class _WatchRemote(_WorkspaceRemote):
    def __init__(self) -> None:
        super().__init__()
        self.source = str(uuid4())
        self.watch_ready = asyncio.Event()
        self.watch_ready.set()
        self.watch_opened: asyncio.Queue[_Changes] = asyncio.Queue(16)
        self.change_streams: list[_Changes] = []

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/proxy/44773/projects/"):
            return await super().respond(request)
        assert request.method == "GET"
        assert request.headers["X-EXECD-ACCESS-TOKEN"] == "private-execd"
        assert request.headers["Accept-Encoding"] == "identity"
        sandbox_id, _generation = self.endpoints[request.url.host]
        project = path.split("/")[4]
        record = self.records.get((sandbox_id, project))
        if record is None or record.phase == "deleted":
            return httpx.Response(404)
        if record.phase == "deleting":
            return httpx.Response(409)
        stream = _Changes(self.source, record.incarnation, self.watch_ready)
        self.change_streams.append(stream)
        self.watch_opened.put_nowait(stream)
        return httpx.Response(
            200, headers={"Content-Type": "application/x-ndjson"}, stream=stream
        )


class _WatchState(_ControlledState):
    record_releases = False
    releases: asyncio.Queue[None]

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        await super().release_owner(claim)
        if self.record_releases:
            self.releases.put_nowait(None)


@asynccontextmanager
async def _watch_world(tmp_path: Path) -> AsyncIterator[tuple[_World, _WatchRemote]]:
    async with _world(tmp_path) as world:
        remote = _WatchRemote()
        remote.changes = world.changes
        world.remote = remote
        try:
            yield world, remote
        finally:
            remote.watch_ready.set()
            for gate in remote.gates.values():
                gate.release.set()


async def _manager(
    world: _World, notifications: Notifications | None = None
) -> tuple[OpenSandboxManager[str], _WatchState]:
    client = _Client(world.remote, workspace_root=None)
    state = _WatchState(world.engines(world.url), world.changes)
    state.releases = asyncio.Queue[None](16)
    manager = OpenSandboxManager(
        client=client, state=state, notifications=notifications
    )
    world.managers.append(manager)
    world.states.append(state)
    world.clients.append(client)
    await manager.start()
    return manager, state


async def _prepare(
    manager: OpenSandboxManager[str], project: str = "project-a"
) -> None:
    async with manager.workspace("owner", workspace_key=project).open():
        pass
