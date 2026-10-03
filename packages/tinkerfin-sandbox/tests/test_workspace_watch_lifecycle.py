"""Observation shutdown never retains a workspace Run or follows a new instance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest
from test_workspace_watch import _manager, _prepare, _watch_world

from tinkerfin_notifications import (
    MemoryBackend,
    Notifications,
    NotificationUnavailable,
    ResyncRequired,
)
from tinkerfin_sandbox import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxPausedError,
    WorkspaceChange,
)
from tinkerfin_sandbox.backends import _workspace_changes


@pytest.mark.parametrize("operation", ["pause", "destroy"])
async def test_parent_shutdown_ends_watch_without_consumer_participation(
    tmp_path: Path, operation: Literal["pause", "destroy"]
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.watch() as changes:
            stream = await remote.watch_opened.get()
            if operation == "pause":
                await manager.pause("owner")
            else:
                await manager.destroy("owner")
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)
            await stream.closed.wait()
            with pytest.raises(
                OpenSandboxPausedError
                if operation == "pause"
                else OpenSandboxBackendUnavailableError
            ):
                async with project.watch():
                    pytest.fail("Stopped parents cannot admit another watch")
            if operation == "pause":
                await manager.resume("owner")
                async with project.watch() as current:
                    replacement = await remote.watch_opened.get()
                    assert replacement is not stream
                    await replacement.pending.put({"type": "changed"})
                    assert await anext(current) is WorkspaceChange.FILES_CHANGED


async def test_project_delete_ends_only_its_watch_and_reentry_selects_new_incarnation(
    tmp_path: Path,
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        await _prepare(manager, "project-b")
        first = manager.workspace("owner", workspace_key="project-a")
        second = manager.workspace("owner", workspace_key="project-b")
        async with first.watch() as first_changes:
            first_stream = await remote.watch_opened.get()
            async with second.watch() as second_changes:
                second_stream = await remote.watch_opened.get()
                await first.delete()
                assert await anext(first_changes) == ResyncRequired("disconnected")
                with pytest.raises(StopAsyncIteration):
                    await anext(first_changes)
                with pytest.raises(OpenSandboxBackendUnavailableError):
                    async with first.watch():
                        pytest.fail("Watching cannot recreate deleted projects")
                await second_stream.pending.put({"type": "changed"})
                assert await anext(second_changes) is WorkspaceChange.FILES_CHANGED
                await _prepare(manager)
                async with first.watch() as replacement:
                    current_stream = await remote.watch_opened.get()
                    assert current_stream.incarnation != first_stream.incarnation
                    await current_stream.pending.put({"type": "changed"})
                    assert await anext(replacement) is WorkspaceChange.FILES_CHANGED
            assert not remote.destroy_calls


async def test_manager_close_wakes_watch_before_waiting_for_active_file_context(
    tmp_path: Path,
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as files:
            async with project.watch() as changes:
                stream = await remote.watch_opened.get()
                closing = world.spawn(manager.aclose())
                assert await anext(changes) == ResyncRequired("disconnected")
                await stream.closed.wait()
                assert not closing.done() and not files.is_closed
        await closing
        await manager.aclose()
        assert not remote.live and not remote.network_live


@pytest.mark.parametrize("ending", ["closed", "source_closed", "eof", "invalid"])
async def test_remote_stream_end_invalidates_once_and_does_not_rebind(
    tmp_path: Path, ending: str
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            if ending == "eof":
                await stream.pending.put(None)
            elif ending == "source_closed":
                await stream.pending.put({"type": "resync", "reason": "source_closed"})
            else:
                await stream.pending.put({"type": ending})
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)
            await stream.closed.wait()
            assert len(remote.change_streams) == 1 and remote.created == 1


async def test_remote_pause_deadline_is_signal_controlled_and_never_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_started = asyncio.Event()
    expire = asyncio.Event()

    async def expire_read(lines: AsyncIterator[bytes], *, timeout: float) -> bytes:
        assert timeout == 45
        read_started.set()
        await expire.wait()
        raise TimeoutError("Controlled heartbeat deadline")

    monkeypatch.setattr(_workspace_changes, "_next_line", expire_read)
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        peer, _peer_state = await _manager(world)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await read_started.wait()
            await peer.pause("owner")
            expire.set()
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)
            await stream.closed.wait()
            assert not remote.resume_calls and len(remote.change_streams) == 1


async def test_heartbeat_rejects_a_binding_removed_by_another_worker(
    tmp_path: Path,
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        peer, _peer_state = await _manager(world)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await peer.destroy("owner")
            await stream.pending.put({"type": "heartbeat"})
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)


@pytest.mark.parametrize("action", ["peer_resume", "local_pause_again"])
async def test_pause_cannot_be_hidden_by_resume_or_an_idempotent_pause(
    tmp_path: Path, action: str
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        peer, _peer_state = await _manager(world)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await peer.pause("owner")
            if action == "peer_resume":
                await peer.resume("owner")
                await stream.pending.put({"type": "heartbeat"})
            else:
                await manager.pause("owner")
            await stream.pending.put({"type": "changed"})
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)


async def test_borrowed_notification_close_ends_source_but_not_the_project(
    tmp_path: Path,
) -> None:
    async with (
        Notifications() as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.watch() as changes:
            stream = await remote.watch_opened.get()
            await notifications.aclose()
            assert await anext(changes) == ResyncRequired("disconnected")
            with pytest.raises(StopAsyncIteration):
                await anext(changes)
            await stream.closed.wait()
        async with project.open():
            pass
        assert remote.created == 1


async def test_actual_notification_send_failure_resyncs_the_matching_watch(
    tmp_path: Path,
) -> None:
    class FailingMemory(MemoryBackend):
        fail = True

        async def publish(self, payload: bytes) -> None:
            if self.fail:
                self.fail = False
                raise NotificationUnavailable("Controlled publication failure")
            await super().publish(payload)

    async with (
        Notifications(backend=FailingMemory()) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        async with manager.workspace(
            "owner", workspace_key="project-a"
        ).watch() as changes:
            stream = await remote.watch_opened.get()
            await stream.pending.put({"type": "changed"})
            assert await anext(changes) == ResyncRequired("disconnected")
            await stream.pending.put({"type": "changed"})
            assert await anext(changes) is WorkspaceChange.FILES_CHANGED
        assert remote.created == 1 and not remote.destroy_calls
