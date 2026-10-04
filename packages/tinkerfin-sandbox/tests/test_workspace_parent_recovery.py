"""Logical workspace admission after another manager deletes its physical parent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest
from test_manager import _resource_key
from test_workspace_lifecycle import _project, _projects
from test_workspace_readiness import _Clock, _Deadline

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxHandleClosedError,
    OpenSandboxRuntimeInfo,
    OpenSandboxStateOwnershipError,
)
from tinkerfin_sandbox.lifecycle import _manager_bindings


async def test_workspace_reopens_after_peer_deletes_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        peer = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as original:
            old_id = original.id
        await peer.delete("owner")
        probes: list[str] = []

        async def missing_parent(sandbox_id: str) -> OpenSandboxRuntimeInfo:
            probes.append(sandbox_id)
            assert sandbox_id == old_id
            raise OpenSandboxBackendUnavailableError(
                "The parent no longer exists", context={"reason": "not_found"}
            )

        monkeypatch.setattr(world.clients[0], "get_runtime_info", missing_parent)
        async with project.open() as replacement:
            assert replacement.id != old_id
            assert (await replacement.aexecute("replacement")).output == "replacement"
        assert probes == [old_id]
        assert remote.created == 2
        assert remote.destroy_calls == [old_id]


@pytest.mark.parametrize(
    "failure",
    [
        "available",
        "unreachable",
        "authentication",
        "permission",
        "protocol",
        "timeout",
    ],
)
async def test_unconfirmed_parent_deletion_cannot_create_another_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as original:
            old_id = original.id
        state = world.states[1]
        claim = await state.acquire_owner(_resource_key("owner"))
        try:
            await state.unbind_owner(claim)
        finally:
            await state.release_owner(claim)

        async def unconfirmed_parent(sandbox_id: str) -> OpenSandboxRuntimeInfo:
            assert sandbox_id == old_id
            if failure == "available":
                return OpenSandboxRuntimeInfo(
                    sandbox_id=sandbox_id, available=True, healthy=False
                )
            if failure == "protocol":
                raise OpenSandboxBackendProtocolError("Invalid response")
            if failure == "timeout":
                raise OpenSandboxBackendTimeoutError("Provider request timed out")
            raise OpenSandboxBackendUnavailableError(
                "Parent deletion is not confirmed", context={"reason": failure}
            )

        monkeypatch.setattr(world.clients[0], "get_runtime_info", unconfirmed_parent)
        expected = {
            "available": OpenSandboxStateOwnershipError,
            "protocol": OpenSandboxBackendProtocolError,
            "timeout": OpenSandboxBackendTimeoutError,
        }.get(failure, OpenSandboxBackendUnavailableError)
        with pytest.raises(expected):
            async with project.open():
                raise AssertionError("Unconfirmed deletion must reject admission")
        assert remote.created == 1
        assert remote.destroy_calls == []
        assert await manager.is_healthy("owner")


@pytest.mark.parametrize("termination", ["deadline", "cancelled"])
async def test_parent_probe_has_a_cancellable_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination: Literal["deadline", "cancelled"],
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        peer = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as original:
            old_id = original.id
        await peer.delete("owner")
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        clock = _Clock()

        async def pending_probe(sandbox_id: str) -> OpenSandboxRuntimeInfo:
            assert sandbox_id == old_id
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("The probe must be interrupted")

        def controlled_timeout(seconds: float) -> _Deadline:
            return _Deadline(clock, clock.now + seconds)

        async def open_pending() -> None:
            async with project.open():
                raise AssertionError("An unfinished probe cannot admit a workspace")

        controlled = ModuleType("controlled_parent_probe_asyncio")
        controlled.__dict__.update(vars(asyncio))
        setattr(controlled, "timeout", controlled_timeout)
        with monkeypatch.context() as patch:
            patch.setattr(_manager_bindings, "asyncio", controlled)
            patch.setattr(world.clients[0], "get_runtime_info", pending_probe)
            opening = world.spawn(open_pending())
            await started.wait()
            assert len(clock.deadlines) == 1
            assert clock.deadlines[0].when == (
                clock.now
                + world.clients[0].config.lifecycle_request_timeout.total_seconds()
            )
            if termination == "deadline":
                clock.deadlines[0].expire()
            else:
                opening.cancel()
            expected = (
                OpenSandboxBackendTimeoutError
                if termination == "deadline"
                else asyncio.CancelledError
            )
            with pytest.raises(expected):
                await opening
            assert cancelled.is_set()
        assert remote.created == 1
        assert remote.destroy_calls == [old_id]


async def test_confirmed_deletion_does_not_wait_for_or_revive_an_old_parent_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        peer = await world.add()
        async with _project(manager)._parent(allow_create=True) as borrowed:
            assert borrowed is not None
            old_handle, old_parent = borrowed
            await peer.delete("owner")

            async def missing_parent(sandbox_id: str) -> OpenSandboxRuntimeInfo:
                assert sandbox_id == old_parent.id
                raise OpenSandboxBackendUnavailableError(
                    "The parent no longer exists", context={"reason": "not_found"}
                )

            monkeypatch.setattr(world.clients[0], "get_runtime_info", missing_parent)
            async with manager.workspace(
                "owner", workspace_key="project-a"
            ).open() as replacement:
                assert replacement.id != old_parent.id
                with pytest.raises(OpenSandboxHandleClosedError):
                    await old_handle.aexecute("old reference")
            assert remote.created == 2
            assert remote.destroy_calls == [old_parent.id]


@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_failed_new_admission_keeps_retired_connection_cleanup_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["error", "cancelled"],
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        peer = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as original:
            old_id = original.id
        await peer.delete("owner")
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        close_finished = asyncio.Event()
        cleanup_waiting = asyncio.Event()
        close_backend = OpenSandboxBackend.aclose
        wait_for_cleanup = manager._wait_for_cleanup_tasks

        async def missing_parent(sandbox_id: str) -> OpenSandboxRuntimeInfo:
            assert sandbox_id == old_id
            raise OpenSandboxBackendUnavailableError(
                "The parent no longer exists", context={"reason": "not_found"}
            )

        async def held_close(backend: OpenSandboxBackend) -> None:
            if backend.id == old_id:
                close_started.set()
                await close_release.wait()
            await close_backend(backend)
            if backend.id == old_id:
                close_finished.set()

        async def fail_create(**_arguments: object) -> OpenSandboxBackend:
            await close_started.wait()
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise OpenSandboxBackendProtocolError("Creation rejected")

        async def observed_cleanup_wait() -> None:
            cleanup_waiting.set()
            await wait_for_cleanup()

        monkeypatch.setattr(world.clients[0], "get_runtime_info", missing_parent)
        monkeypatch.setattr(world.clients[0], "create", fail_create)
        monkeypatch.setattr(OpenSandboxBackend, "aclose", held_close)
        monkeypatch.setattr(manager, "_wait_for_cleanup_tasks", observed_cleanup_wait)
        expected = (
            asyncio.CancelledError
            if failure == "cancelled"
            else OpenSandboxBackendProtocolError
        )
        try:
            with pytest.raises(expected):
                async with project.open():
                    raise AssertionError("Failed creation cannot yield a workspace")
            closing = world.spawn(manager.aclose())
            await cleanup_waiting.wait()
            assert not closing.done()
            assert not close_finished.is_set()
            close_release.set()
            await closing
            assert close_finished.is_set()
            assert remote.created == 1
            assert remote.destroy_calls == [old_id]
        finally:
            close_release.set()
