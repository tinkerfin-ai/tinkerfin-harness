"""Namespace-bound workspace access through the public resource lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend
from test_lifecycle_notifications import _Recorder
from test_manager import _FakeClient, _FakeState, _resource_key
from test_pause_resume import _Client, _Remote

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxResetError,
    OpenSandboxSettlementTimeoutError,
    RootedOpenSandboxBackend,
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
        assert resumed is first and remote.states[first_id] == "Running"
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


async def test_workspace_declaration_is_lazy_and_borrow_preserves_shared_resources(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    state = _FakeState()
    manager = OpenSandboxManager(client=client, state=state)
    route = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    routes = {"/reference/": route}
    declaration = manager.workspace("users/7", routes=routes)
    routes.clear()
    assert client.create_calls == client.close_calls == 0
    assert state.get_calls == []
    async with manager:
        async with declaration.prepare(_identity()) as prepared:
            assert isinstance(prepared.workspace, RootedOpenSandboxBackend)
            assert isinstance(prepared.backend, CompositeBackend)
            assert prepared.backend.default is prepared.workspace
            assert prepared.backend.routes == {"/reference/": route}
            assert prepared.workspace.to_shell_path("/report.txt") == "report.txt"
            assert prepared.filesystem_instructions
            assert set(prepared.tool_descriptions) == {"execute"}
            other = await manager.get("users/7", namespace="company-b")
            assert other.id != prepared.workspace.id
            async with declaration.prepare(_identity()) as repeated:
                assert repeated.workspace is prepared.workspace
            assert not prepared.workspace.is_closed
        assert client.close_calls == 0 and client.destroy_calls == []
        assert await manager.get("users/7", namespace="company-a") is prepared.workspace
    assert client.close_calls == 1
    assert client.destroy_calls == []


async def test_manager_close_waits_for_open_workspace_borrow() -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    manager = OpenSandboxManager(
        client=client, state=_FakeState(), settlement_timeout=0
    )
    await manager.start()
    async with manager.workspace("owner").prepare(_identity()) as prepared:
        with pytest.raises(OpenSandboxSettlementTimeoutError):
            await manager.aclose()
        assert client.close_calls == 0 and not prepared.workspace.is_closed
        with pytest.raises(OpenSandboxManagerClosedError):
            await manager.get("owner", namespace="company-a")
    # A close caller may cancel its wait; the manager still owns final cleanup.
    closed = asyncio.Event()
    original_close = client.aclose

    async def record_close() -> None:
        await original_close()
        closed.set()

    client.aclose = record_close
    await closed.wait()
    await manager.aclose()
    assert prepared.workspace.is_closed


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_workspace_body_failure_releases_borrow_without_destroying_sandbox(
    failure: str,
) -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    manager = OpenSandboxManager(client=client, state=_FakeState())
    entered = asyncio.Event()

    async def run() -> None:
        async with manager.workspace("owner").prepare(_identity()):
            entered.set()
            if failure == "error":
                raise ValueError("workspace use failed")
            await asyncio.Event().wait()

    async with manager:
        task = asyncio.create_task(run())
        await entered.wait()
        if failure == "cancel":
            task.cancel()
        with pytest.raises(
            ValueError if failure == "error" else asyncio.CancelledError
        ):
            await task
        assert client.destroy_calls == [] and client.close_calls == 0
        assert (await manager.get("owner", namespace="company-a")).id == "sandbox-1"
    assert client.close_calls == 1 and client.destroy_calls == []


async def test_cancelled_preparation_releases_manager_operation() -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    client.create_gate = asyncio.Event()
    manager = OpenSandboxManager(client=client, state=_FakeState())

    async def run() -> None:
        async with manager.workspace("owner").prepare(_identity()):
            pytest.fail("Cancelled preparation cannot enter the body")

    async with manager:
        task = asyncio.create_task(run())
        await client.create_entered.wait()
        task.cancel()
        client.create_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.close_calls == 1


async def test_workspace_and_resource_identity_validation_precede_io() -> None:
    client = _FakeClient()
    manager = OpenSandboxManager(client=client)
    with pytest.raises(ValueError, match="workspace_root"):
        manager.workspace("owner")
    for namespace in ["", " leading", "trailing ", "\ud800"]:
        with pytest.raises(ValueError):
            await manager.get("owner", namespace=namespace)
        with pytest.raises(ValueError):
            await manager.reconnect("owner", namespace=namespace)
    with pytest.raises(TypeError, match="key_resolver"):
        await OpenSandboxManager[int](client=client).get(3)
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
