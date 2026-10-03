"""Project-bound notification admission, ownership, and independent readers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue
from test_pause_resume import _Client, _ControlledState, _World, _world
from test_workspace_lifecycle import _WorkspaceRemote

from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    NotificationLimits,
    Notifications,
    NotificationScope,
    ResyncRequired,
)
from tinkerfin_sandbox import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxManager,
    OpenSandboxOwnerClaim,
    OpenSandboxPurposeError,
    WorkspaceChange,
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


class _RecordingMemory(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.messages: asyncio.Queue[Notification] = asyncio.Queue(16)
        self.closed = False

    async def publish(self, payload: bytes) -> None:
        await super().publish(payload)
        self.messages.put_nowait(Notification.model_validate_json(payload))

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


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


async def test_watch_shares_one_connection_and_each_context_releases_its_reader(
    tmp_path: Path,
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        commands = list(remote.executions)
        registry = list(remote.registry_requests)
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.watch() as first:
            stream = await remote.watch_opened.get()
            async with project.watch() as second:
                await stream.pending.put({"type": "changed"})
                assert await anext(first) is WorkspaceChange.FILES_CHANGED
                assert await anext(second) is WorkspaceChange.FILES_CHANGED
                assert len(remote.change_streams) == 1
                assert not remote.live and not remote.network_live
            assert not stream.closed.is_set()
            await stream.pending.put({"type": "changed"})
            assert await anext(first) is WorkspaceChange.FILES_CHANGED
        assert stream.closed.is_set()
        assert remote.created == 1
        assert remote.executions == commands and remote.registry_requests == registry


async def test_cancelling_one_pending_entry_preserves_the_other(tmp_path: Path) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, state = await _manager(world)
        await _prepare(manager)
        remote.watch_ready.clear()
        state.record_releases = True
        second_entered = asyncio.Event()
        release = asyncio.Event()

        async def enter(signal: asyncio.Event) -> None:
            async with manager.workspace("owner", workspace_key="project-a").watch():
                signal.set()
                await release.wait()

        first = world.spawn(enter(asyncio.Event()))
        await state.releases.get()
        stream = await remote.watch_opened.get()
        second = world.spawn(enter(second_entered))
        await state.releases.get()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not stream.closed.is_set()
        remote.watch_ready.set()
        await second_entered.wait()
        assert len(remote.change_streams) == 1
        release.set()
        await second
        assert stream.closed.is_set()


async def test_notifications_arriving_before_ready_are_retained_for_the_fixed_source(
    tmp_path: Path,
) -> None:
    async with (
        Notifications() as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        first_manager, _first_state = await _manager(world, notifications)
        second_manager, _second_state = await _manager(world, notifications)
        await _prepare(first_manager)
        async with first_manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as first:
            first_stream = await remote.watch_opened.get()
            remote.watch_ready.clear()

            async def consume_second() -> WorkspaceChange | ResyncRequired:
                async with second_manager.workspace(
                    "owner", workspace_key="project-a"
                ).watch() as changes:
                    return await anext(changes)

            second = world.spawn(consume_second())
            await remote.watch_opened.get()
            await first_stream.pending.put({"type": "changed"})
            assert await anext(first) is WorkspaceChange.FILES_CHANGED
            remote.watch_ready.set()
            assert await second is WorkspaceChange.FILES_CHANGED


@pytest.mark.parametrize("close_manager", [False, True])
async def test_pending_entry_is_owned_until_cancel_or_manager_close(
    tmp_path: Path, close_manager: bool
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        remote.watch_ready.clear()

        async def enter() -> None:
            async with manager.workspace("owner", workspace_key="project-a").watch():
                pytest.fail("Unready sources cannot be exposed")

        entering = world.spawn(enter())
        stream = await remote.watch_opened.get()
        if close_manager:
            await manager.aclose()
        else:
            entering.cancel()
        with pytest.raises(
            OpenSandboxBackendUnavailableError
            if close_manager
            else asyncio.CancelledError
        ):
            await entering
        assert stream.closed.is_set()


async def test_observer_failure_and_unstarted_service_leave_existing_files_unchanged(
    tmp_path: Path,
) -> None:
    notifications = Notifications()
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        records = {key: record.payload() for key, record in remote.records.items()}
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with manager.workspace("owner", workspace_key="project-a").watch():
                pytest.fail("Borrowed Notifications must already be started")
        assert {
            key: record.payload() for key, record in remote.records.items()
        } == records
        assert not remote.change_streams
    await notifications.aclose()


@pytest.mark.parametrize("state", ["unbound", "missing", "commands"])
async def test_watch_never_creates_or_selects_another_capability(
    tmp_path: Path, state: str
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        if state == "missing":
            await _prepare(manager, "other")
        elif state == "commands":
            await manager.get("owner")
        created = remote.created
        with pytest.raises(
            OpenSandboxPurposeError
            if state == "commands"
            else OpenSandboxBackendUnavailableError
        ):
            async with manager.workspace("owner", workspace_key="project-a").watch():
                pytest.fail("Watching cannot create a project")
        assert remote.created == created
        assert len(remote.records) == (1 if state == "missing" else 0)


async def test_borrowed_service_survives_manager_close_and_two_workers_receive_hints(
    tmp_path: Path,
) -> None:
    backend = _RecordingMemory()
    async with Notifications(backend=backend) as notifications:
        async with _watch_world(tmp_path) as (world, remote):
            first_manager, _first_state = await _manager(world, notifications)
            second_manager, _second_state = await _manager(world, notifications)
            await _prepare(first_manager)
            async with first_manager.workspace(
                "owner", workspace_key="project-a"
            ).watch() as first:
                first_stream = await remote.watch_opened.get()
                async with second_manager.workspace(
                    "owner", workspace_key="project-a"
                ).watch() as second:
                    second_stream = await remote.watch_opened.get()
                    assert first_stream is not second_stream
                    await first_stream.pending.put({"type": "changed"})
                    assert await anext(first) is WorkspaceChange.FILES_CHANGED
                    assert await anext(second) is WorkspaceChange.FILES_CHANGED
                    await second_manager.aclose()
                    assert await anext(second) == ResyncRequired("disconnected")
                    with pytest.raises(StopAsyncIteration):
                        await anext(second)
                    assert not backend.closed
                    await first_stream.pending.put({"type": "changed"})
                    assert await anext(first) is WorkspaceChange.FILES_CHANGED
            assert all(stream.closed.is_set() for stream in remote.change_streams)
        assert not backend.closed
        await notifications.publish(
            Notification(scope=NotificationScope("borrowed"), topic="usable", key="one")
        )
    assert backend.closed


@pytest.mark.parametrize("field", ["binding", "source", "incarnation"])
async def test_delayed_foreign_identity_cannot_invalidate_current_watch(
    tmp_path: Path, field: str
) -> None:
    backend = _RecordingMemory()
    async with (
        Notifications(backend=backend) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await stream.pending.put({"type": "resync", "reason": "overflow"})
            observed = await backend.messages.get()
            assert await anext(changes) == ResyncRequired("overflow")
            details = {**observed.details, field: "obsolete", "reason": "disconnected"}
            await notifications.publish(
                observed.model_copy(update={"details": details, "key": "obsolete"})
            )
            await stream.pending.put({"type": "changed"})
            result = await anext(changes)
            if result == ResyncRequired("overflow"):
                result = await anext(changes)
            assert result is WorkspaceChange.FILES_CHANGED


async def test_notification_admission_failure_immediately_invalidates_local_readers(
    tmp_path: Path,
) -> None:
    async with (
        Notifications(
            limits=NotificationLimits(max_notification_bytes=1)
        ) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await stream.pending.put({"type": "changed"})
            assert await anext(changes) == ResyncRequired("overflow")


@pytest.mark.parametrize("reason", ["topology", "overflow"])
async def test_source_resync_uses_the_existing_public_type(
    tmp_path: Path, reason: Literal["topology", "overflow"]
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await stream.pending.put({"type": "resync", "reason": reason})
            assert await anext(changes) == ResyncRequired(
                "reconnected" if reason == "topology" else "overflow"
            )


async def test_capacity_is_bounded_without_affecting_existing_subscription(
    tmp_path: Path,
) -> None:
    async with (
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.watch() as changes:
            with pytest.raises(OpenSandboxBusyError):
                async with project.watch():
                    pytest.fail("A second consumer exceeds the configured capacity")
            stream = await remote.watch_opened.get()
            await stream.pending.put({"type": "changed"})
            assert await anext(changes) is WorkspaceChange.FILES_CHANGED
