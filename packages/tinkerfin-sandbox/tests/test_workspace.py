"""Public project selection, isolated borrowing and physical resource lifetimes."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend
from test_lifecycle_notifications import _Recorder
from test_manager import _FakeClient, _resource_key
from test_pause_resume import _Client, _Remote
from test_workspace_lifecycle import _projects

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxResetError,
    RootedOpenSandboxBackend,
    SandboxWorkspace,
)


def _identity(namespace: str = "company-a") -> RunIdentity:
    return RunIdentity(namespace=namespace, thread_id="thread", run_id="run")


@pytest.mark.parametrize("key", ["users/7", "sessions/8", "projects/3", "汉字\0/:"])
async def test_namespace_and_standalone_keys_are_independent(key: str) -> None:
    remote = _Remote()
    client = _Client(remote, workspace_root="/workspace")
    state = InMemoryOpenSandboxState()
    async with OpenSandboxManager(client=client, state=state) as manager:
        first, second, standalone = await asyncio.gather(
            manager.get(key, namespace="one"),
            manager.get(key, namespace="two"),
            manager.get(key),
        )
        assert len({first.id, second.id, standalone.id}) == 3
        assert await manager.get(key, namespace="one") is first
        for namespace, backend in [("one", first), ("two", second), (None, standalone)]:
            binding = await state.read_binding(_resource_key(key, namespace))
            assert binding is not None and binding.sandbox_id == backend.id
            details = await manager.get_details(key, namespace=namespace)
            assert details is not None
            assert (details.namespace, details.owner_key) == (namespace, key)


async def test_all_management_operations_target_the_selected_namespace() -> None:
    remote = _Remote()
    client = _Client(remote, workspace_root="/workspace")
    recorder = _Recorder()
    async with OpenSandboxManager(client=client, observers=[recorder]) as manager:
        first = await manager.get("owner", namespace="one")
        second = await manager.get("owner", namespace="two")
        standalone = await manager.get("owner")
        first_id, second_id, standalone_id = first.id, second.id, standalone.id
        assert await manager.is_healthy("owner", namespace="one")
        assert not await manager.is_healthy("owner", namespace="absent")
        assert await manager.reconnect("owner", namespace="one") is first
        # This remote fixture returns an invalid reset response. The failed command
        # must still reach only the selected instance and retain its binding.
        before_reset = len(remote.executions)
        with pytest.raises(OpenSandboxResetError):
            await manager.reset("owner", namespace="one")
        assert {entry[0] for entry in remote.executions[before_reset:]} == {first_id}
        assert (
            await manager.get_diagnostic_logs("owner", namespace="one")
        ).sandbox_id == first_id
        assert (
            await manager.get_diagnostic_events("owner", namespace="one")
        ).sandbox_id == first_id
        await manager.pause("owner", namespace="one")
        assert remote.states == {
            first_id: "Paused",
            second_id: "Running",
            standalone_id: "Running",
        }
        resumed = await manager.resume("owner", namespace="one")
        assert resumed is None and remote.states[first_id] == "Running"
        assert await manager.get("owner", namespace="one") is first
        replaced = await manager.recreate("owner", namespace="one")
        assert replaced is first and first.id != first_id
        await manager.destroy("owner", namespace="one")
        assert await manager.get_details("owner", namespace="one") is None
        assert second.id == second_id and standalone.id == standalone_id
        assert remote.states == {second_id: "Running", standalone_id: "Running"}
        await manager.delete("owner", namespace="two")
        assert remote.states == {standalone_id: "Running"}
    assert recorder.events
    assert {(event.namespace, event.owner_key) for event in recorder.events} == {
        ("one", "owner"),
        ("two", "owner"),
    }


async def test_workspace_declaration_binds_project_independently_of_runtime_namespace(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        route = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        routes = {"/reference/": route}
        declaration = manager.workspace(
            "users/7", workspace_key="project-a", routes=routes
        )
        assert isinstance(declaration, SandboxWorkspace)
        routes.clear()
        assert remote.created == 0
        async with (
            declaration.prepare(_identity("one")) as first,
            declaration.prepare(_identity("two")) as second,
        ):
            assert isinstance(first.workspace, RootedOpenSandboxBackend)
            assert isinstance(first.backend, CompositeBackend)
            assert first.backend.default is first.workspace
            assert first.backend.routes == {"/reference/": route}
            assert first.workspace.to_shell_path("/report.txt") == "report.txt"
            assert first.filesystem_instructions
            assert set(first.tool_descriptions) == {"execute"}
            assert first.workspace is not second.workspace
            assert first.workspace.id == second.workspace.id
            assert remote.created == 1 and len(remote.records) == 1
        assert first.workspace.is_closed and second.workspace.is_closed
        assert not remote.live and not remote.network_live
        assert remote.destroy_calls == []
        async with declaration.open() as files:
            assert files.id == first.workspace.id
            assert not isinstance(files, CompositeBackend)
        await declaration.delete()
        assert next(iter(remote.records.values())).phase == "deleted"


async def test_public_open_delete_select_only_the_named_project(tmp_path: Path) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        first = manager.workspace("users/7", workspace_key="project-a")
        second = manager.workspace("users/7", workspace_key="project-b")
        async with first.open() as first_files, second.open() as second_files:
            assert first_files.id == second_files.id
            await first.delete()
            assert len(remote.live) == len(remote.network_live) == 1
            assert (await second_files.aexecute("second")).output == "second"
        assert remote.destroy_calls == []
        await second.delete()


async def test_manager_close_waits_for_public_workspace_context(tmp_path: Path) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        declaration = manager.workspace("owner", workspace_key="project-a")
        close_started = asyncio.Event()

        async def close() -> None:
            close_started.set()
            await manager.aclose()

        async with declaration.open() as files:
            closing = world.spawn(close())
            await close_started.wait()
            assert not closing.done() and not files.is_closed
            with pytest.raises(OpenSandboxManagerClosedError):
                async with declaration.open():
                    pytest.fail("Closing managers cannot admit another Run")
            assert remote.live
        await closing
        assert files.is_closed and not remote.live and not remote.network_live


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_workspace_body_failure_stops_only_its_run(
    tmp_path: Path, failure: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        declaration = manager.workspace("owner", workspace_key="project-a")
        entered = asyncio.Event()

        async def run() -> None:
            async with declaration.prepare(_identity()):
                entered.set()
                if failure == "error":
                    raise ValueError("workspace use failed")
                await asyncio.Event().wait()

        task = world.spawn(run())
        await entered.wait()
        if failure == "cancel":
            task.cancel()
        with pytest.raises(
            ValueError if failure == "error" else asyncio.CancelledError
        ):
            await task
        assert not remote.live and not remote.network_live
        assert remote.destroy_calls == []
        assert next(iter(remote.records.values())).phase == "active"


async def test_workspace_identity_validation_precedes_io(tmp_path: Path) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        with pytest.raises(ValueError, match="workspace_key"):
            manager.workspace("owner", workspace_key="")
        declaration = manager.workspace("owner", workspace_key="project-a")
        with pytest.raises(TypeError, match="RunIdentity"):
            async with declaration.prepare(cast(RunIdentity, None)):
                pytest.fail("Invalid execution identities cannot prepare resources")
        assert remote.created == 0

    client = _FakeClient()
    manager = OpenSandboxManager(client=client)
    for namespace in ["", " leading", "trailing ", "\ud800"]:
        with pytest.raises(ValueError):
            await manager.get("owner", namespace=namespace)
        with pytest.raises(ValueError):
            await manager.reconnect("owner", namespace=namespace)
    with pytest.raises(TypeError, match="key_resolver"):
        OpenSandboxManager[int](client=client).workspace(3, workspace_key="project-a")
    assert client.create_calls == client.close_calls == 0


def test_preparation_values_are_immutable_and_keep_workspace_distinct() -> None:
    workspace = Path("/workspace")
    descriptions = {"execute": "Run a command inside the workspace."}
    prepared = PreparedWorkspace(workspace, "backend", tool_descriptions=descriptions)
    descriptions.clear()
    assert prepared.tool_descriptions["execute"]
    with pytest.raises(FrozenInstanceError):
        setattr(prepared, "workspace", Path("/other"))
    assert prepared.workspace is workspace
