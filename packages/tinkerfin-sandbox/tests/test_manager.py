from __future__ import annotations

import asyncio
import json
import threading
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from deepagents.backends import LocalShellBackend

from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxBackend,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBinding,
    OpenSandboxConfig,
    OpenSandboxDestroyError,
    OpenSandboxDiagnosticContent,
    OpenSandboxLifecycleObserver,
    OpenSandboxManager,
    OpenSandboxOwnerClaim,
    OpenSandboxPlatformInfo,
    OpenSandboxPurpose,
    OpenSandboxRecoveryPolicy,
    OpenSandboxRuntimeInfo,
    OpenSandboxStateError,
    OpenSandboxStatusInfo,
    OpenSandboxUnavailableReason,
    RootedOpenSandboxBackend,
    SQLAlchemyOpenSandboxState,
)


def _resource_key(value: str, namespace: str | None = None) -> str:
    """Encode the current persisted manager identity for fixture setup and checks."""
    return json.dumps(
        {"namespace": namespace, "key": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _key(value: str) -> str:
    return value


def _new_manager(**kwargs: Any) -> OpenSandboxManager[str]:
    return OpenSandboxManager(key_resolver=_key, **kwargs)


class _ExecuteResponse:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _OperationGate:
    """Release sync and async fake operations explicitly from the test loop."""

    def __init__(self) -> None:
        self._thread = threading.Event()
        self._async = asyncio.Event()

    def set(self) -> None:
        self._thread.set()
        self._async.set()

    def wait(self) -> bool:
        return self._thread.wait()

    async def wait_async(self) -> None:
        await self._async.wait()


class _FakeBackend:
    def __init__(
        self,
        sandbox_id: str,
        *,
        healthy: bool = True,
        runtime_info: OpenSandboxRuntimeInfo | None = None,
    ) -> None:
        self.id = sandbox_id
        self.healthy = healthy
        self.runtime_info = runtime_info or _runtime_info(sandbox_id)
        self.execute_calls: list[tuple[str, int | None]] = []
        self.renew_calls: list[timedelta] = []
        self.close_calls = 0
        self.kill_calls = 0
        self.close_entered = threading.Event()
        self.close_gate: _OperationGate | None = None
        self.runtime_info_calls = 0
        self.block_command: str | None = None
        self.execute_entered = threading.Event()
        self.execute_gate = _OperationGate()

    def execute(self, command: str, *, timeout: int | None = None) -> _ExecuteResponse:
        self.execute_calls.append((command, timeout))
        if command == self.block_command:
            self.execute_entered.set()
            self.execute_gate.wait()
        return _ExecuteResponse(0 if self.healthy else 1)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> _ExecuteResponse:
        self.execute_calls.append((command, timeout))
        if command == self.block_command:
            self.execute_entered.set()
            await self.execute_gate.wait_async()
        return _ExecuteResponse(0 if self.healthy else 1)

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.close_gate is not None:
            self.close_gate.wait()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait_async()

    async def akill(self) -> None:
        self.kill_calls += 1

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info


class _FakeClient:
    def __init__(
        self,
        backend_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = OpenSandboxConfig(
            health_command="echo ok",
            command_timeout=30,
            ttl=timedelta(hours=2),
            warm_pool_size=0,
            workspace_root=None,
        )
        self._backend_factory = backend_factory or _FakeBackend
        self.backends: list[Any] = []
        self.connected: dict[str, Any] = {}
        self.inspection_results: dict[str, OpenSandboxRuntimeInfo] = {}
        self.create_calls = 0
        self.create_metadata: list[dict[str, str]] = []
        self.create_purposes: list[OpenSandboxPurpose] = []
        self.connect_calls: list[str] = []
        self.connect_purposes: list[OpenSandboxPurpose] = []
        self.inspect_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.destroy_errors: dict[str, Exception] = {}
        self.destroy_entered = asyncio.Event()
        self.destroy_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.create_gate: asyncio.Event | None = None
        self.release_after_create_count: int | None = None
        self.close_calls = 0
        self.closed = asyncio.Event()

    async def wait_for_creates(self, count: int) -> None:
        while self.create_calls < count:
            self.create_entered.clear()
            await self.create_entered.wait()

    async def create(
        self,
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        self.create_purposes.append(purpose)
        create_number = self.create_calls
        self.create_entered.set()
        if (
            self.release_after_create_count is not None
            and self.create_calls >= self.release_after_create_count
            and self.create_gate is not None
        ):
            self.create_gate.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        backend = self._backend_factory(f"sandbox-{create_number}")
        if isinstance(backend, _FakeBackend):
            backend.runtime_info = backend.runtime_info.model_copy(
                update={
                    "metadata": {
                        **backend.runtime_info.metadata,
                        "tinkerfin.ai/purpose": purpose,
                    }
                }
            )
        self.backends.append(backend)
        return cast(OpenSandboxBackend, backend)

    async def connect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxBackend:
        self.connect_calls.append(sandbox_id)
        self.connect_purposes.append(purpose)
        try:
            backend = self.connected[sandbox_id]
        except KeyError as error:
            raise OpenSandboxBackendUnavailableError(
                "Sandbox not found",
                context={"reason": "not_found"},
                cause=error,
            ) from error
        return cast(OpenSandboxBackend, backend)

    async def inspect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxRuntimeInfo:
        self.inspect_calls.append(sandbox_id)
        return self.inspection_results[sandbox_id]

    async def _connect_observer(self, sandbox_id: str) -> OpenSandboxBackend:
        return await self.connect(sandbox_id, purpose="workspaces")

    async def get_runtime_info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        return await self.inspect(sandbox_id)

    async def pause(self, sandbox_id: str) -> None:
        raise AssertionError(f"unexpected pause for {sandbox_id}")

    async def resume(self, sandbox_id: str) -> None:
        raise AssertionError(f"unexpected resume for {sandbox_id}")

    async def get_diagnostic_logs(
        self, sandbox_id: str, *, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent:
        raise AssertionError(f"unexpected log diagnostics for {sandbox_id}: {scope}")

    async def get_diagnostic_events(
        self, sandbox_id: str, *, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent:
        raise AssertionError(f"unexpected event diagnostics for {sandbox_id}: {scope}")

    async def destroy(self, sandbox_id: str) -> None:
        self.destroy_calls.append(sandbox_id)
        self.destroy_entered.set()
        if self.destroy_gate is not None:
            await self.destroy_gate.wait()
        error = self.destroy_errors.get(sandbox_id)
        if error is not None:
            raise error

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


class _FakeState(InMemoryOpenSandboxState):
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        super().__init__()
        self.bindings = dict(bindings or {})
        self.binding_generations = {owner_key: 0 for owner_key in self.bindings}
        self.binding_purposes: dict[str, OpenSandboxPurpose] = {
            owner_key: "commands" for owner_key in self.bindings
        }
        self.get_calls: list[str] = []
        self.save_calls: list[tuple[str, str]] = []
        self.consume_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.get_error: Exception | None = None
        self.read_error: Exception | None = None
        self.read_override_enabled = False
        self.read_override: OpenSandboxBinding | None = None
        self.save_error: Exception | None = None
        self.commit_before_save_error = False
        self.cancel_after_save_commit = False
        self.delete_error: Exception | None = None

    @property
    def persistent(self) -> bool:
        return True

    async def acquire_owner(self, owner_key: str):
        self.get_calls.append(owner_key)
        if self.get_error is not None:
            raise OpenSandboxStateError("fake state read failed") from self.get_error
        claim = await super().acquire_owner(owner_key)
        desired_id = self.bindings.get(owner_key)
        current_id = claim.binding.sandbox_id if claim.binding is not None else None
        if desired_id == current_id:
            return claim
        if desired_id is None:
            await super().unbind_owner(claim)
            self.binding_generations.pop(owner_key, None)
            binding = None
        else:
            binding = await super().bind_owner(
                claim, desired_id, purpose=self.binding_purposes[owner_key]
            )
            self.binding_generations[owner_key] = binding.generation
        return replace(claim, binding=binding)

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        self.get_calls.append(owner_key)
        read_error = self.read_error or self.get_error
        if read_error is not None:
            raise OpenSandboxStateError("fake binding reconciliation failed") from (
                read_error
            )
        if self.read_override_enabled:
            return self.read_override
        sandbox_id = self.bindings.get(owner_key)
        if sandbox_id is None:
            return None
        return OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=self.binding_generations.get(owner_key, 0),
            purpose=self.binding_purposes[owner_key],
        )

    async def bind_owner(
        self, claim, sandbox_id: str, *, purpose: OpenSandboxPurpose
    ) -> OpenSandboxBinding:
        self.save_calls.append((claim.owner_key, sandbox_id))
        if self.commit_before_save_error or self.cancel_after_save_commit:
            binding = await super().bind_owner(claim, sandbox_id, purpose=purpose)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
            self.binding_purposes[claim.owner_key] = binding.purpose
        if self.cancel_after_save_commit:
            raise asyncio.CancelledError("bind response was cancelled after commit")
        if self.save_error is not None:
            raise OpenSandboxStateError("fake state write failed") from self.save_error
        if not self.commit_before_save_error:
            binding = await super().bind_owner(claim, sandbox_id, purpose=purpose)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
            self.binding_purposes[claim.owner_key] = binding.purpose
        return binding

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        binding = await super().consume_warm(claim)
        if binding is not None:
            self.consume_calls.append((claim.owner_key, binding.sandbox_id))
            self.bindings[claim.owner_key] = binding.sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
            self.binding_purposes[claim.owner_key] = binding.purpose
        return binding

    async def unbind_owner(self, claim) -> None:
        self.delete_calls.append(claim.owner_key)
        if self.delete_error is not None:
            raise OpenSandboxStateError(
                "fake state delete failed"
            ) from self.delete_error
        await super().unbind_owner(claim)
        self.bindings.pop(claim.owner_key, None)
        self.binding_generations.pop(claim.owner_key, None)
        self.binding_purposes.pop(claim.owner_key, None)

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return ()


def _runtime_info(
    sandbox_id: str,
    *,
    available: bool = True,
    healthy: bool = True,
    state: str = "RUNNING",
    unavailable_reason: OpenSandboxUnavailableReason | None = None,
) -> OpenSandboxRuntimeInfo:
    return OpenSandboxRuntimeInfo(
        sandbox_id=sandbox_id,
        available=available,
        healthy=healthy,
        status=(OpenSandboxStatusInfo(state=state) if available else None),
        created_at=datetime(2026, 8, 8, 1, 2, tzinfo=UTC),
        expires_at=datetime(2026, 8, 8, 3, 2, tzinfo=UTC),
        image="registry.example/sandbox:1",
        platform=OpenSandboxPlatformInfo(os="linux", arch="amd64"),
        metadata={"region": "local", "tinkerfin.ai/purpose": "commands"},
        unavailable_reason=unavailable_reason,
    )


class OpenSandboxManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.managers: list[OpenSandboxManager] = []
        self.clients: list[_FakeClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            for backend in (*client.backends, *client.connected.values()):
                if isinstance(backend, _FakeBackend):
                    backend.execute_gate.set()
                    if backend.close_gate is not None:
                        backend.close_gate.set()
        for manager in self.managers:
            await manager.aclose()

    async def _manager(
        self,
        client: _FakeClient,
        store: _FakeState | None = None,
        *,
        warm_pool_size: int = 0,
        recovery_policy: OpenSandboxRecoveryPolicy | None = None,
        observers: Sequence[OpenSandboxLifecycleObserver] = (),
    ) -> OpenSandboxManager:
        self.clients.append(client)
        manager = _new_manager(
            client=client,
            state=store,
            warm_pool_size=warm_pool_size,
            recovery_policy=recovery_policy,
            observers=observers,
        )
        self.managers.append(manager)
        await manager.start()
        return manager

    async def test_same_owner_concurrent_get_creates_only_one_sandbox(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        store = _FakeState()
        manager = await self._manager(client, store)

        first = asyncio.create_task(manager.get(_key("user-1")))
        second = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        await asyncio.sleep(0)

        self.assertEqual(client.create_calls, 1)
        client.create_gate.set()
        first_handle, second_handle = await asyncio.gather(first, second)

        self.assertIs(first_handle, second_handle)
        self.assertEqual(store.save_calls, [(_resource_key("user-1"), "sandbox-1")])

    async def test_binding_failure_rolls_back_new_sandbox(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        store.save_error = RuntimeError("database unavailable")
        manager = await self._manager(client, store)

        with self.assertRaises(OpenSandboxStateError) as context:
            await manager.get(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(client.backends[0].close_calls, 1)
        store.save_error = None
        handle = await manager.get(_key("user-1"))
        self.assertEqual(handle.id, "sandbox-2")

    async def test_delete_failure_keeps_binding_and_can_be_retried(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        with self.assertRaises(OpenSandboxDestroyError) as context:
            await manager.delete(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(store.bindings, {_resource_key("user-1"): handle.id})
        self.assertEqual(store.delete_calls, [])
        self.assertEqual(client.backends[0].close_calls, 1)

        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(store.bindings, {})
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_aclose_preserves_persistent_warm_and_bound_remote_sandboxes(
        self,
    ) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        handle = await manager.get(_key("user-1"))
        await client.wait_for_creates(2)
        bound_backend, warm_backend = client.backends

        await manager.aclose()
        await manager.aclose()

        self.assertEqual(handle.id, bound_backend.id)
        self.assertEqual(bound_backend.close_calls, 1)
        self.assertNotIn(bound_backend.id, client.destroy_calls)
        self.assertEqual(warm_backend.close_calls, 1)
        self.assertNotIn(warm_backend.id, client.destroy_calls)
        self.assertEqual(store.bindings, {_resource_key("user-1"): bound_backend.id})
        self.assertEqual(store.delete_calls, [])

    async def test_cancelled_aclose_caller_does_not_cancel_shared_cleanup(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, warm_pool_size=1)
        backend = client.backends[0]
        backend.close_gate = _OperationGate()

        first_close = asyncio.create_task(manager.aclose())
        entered = await asyncio.to_thread(backend.close_entered.wait)
        self.assertTrue(entered)
        first_close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_close

        second_close = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        second_finished_early = second_close.done()
        backend.close_gate.set()
        await second_close

        self.assertFalse(second_finished_early)
        self.assertEqual(client.destroy_calls, [backend.id])
        self.assertEqual(backend.close_calls, 1)


class _ReconnectableFakeClient(_FakeClient):
    async def create(
        self,
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        backend = await super().create(purpose=purpose, metadata=metadata)
        self.connected[backend.id] = backend
        return backend


class _ResettableLocalBackend(LocalShellBackend):
    """Run real cleanup commands while emulating the remote lifecycle extension."""

    enable_capture_offload = False

    def __init__(self, sandbox_id: str, workspace: Path) -> None:
        super().__init__(root_dir=workspace, virtual_mode=False, inherit_env=True)
        self._test_id = sandbox_id
        self.renew_calls: list[timedelta] = []
        self.close_calls = 0

    @property
    def id(self) -> str:
        return self._test_id

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew(timeout)

    def close(self) -> None:
        self.close_calls += 1

    async def aclose(self) -> None:
        self.close()

    @contextmanager
    def _rooted_file_operation(self) -> Iterator[None]:
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", (timedelta(hours=2), None))
async def test_manager_reconnect_preserves_identity_and_destroy_removes_binding(
    ttl: timedelta | None,
) -> None:
    client = _ReconnectableFakeClient()
    client.config = client.config.model_copy(update={"ttl": ttl})
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        first = await manager.get(_key("user-1"))

        assert await manager.reconnect(_key("user-1")) is first

        await manager.destroy(_key("user-1"))
        assert client.destroy_calls == [first.id]
        assert store.bindings == {}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_clears_only_workspace_and_keeps_stable_backend(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".hidden").write_text("hidden", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "data.txt").write_text("data", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)

    client = _ReconnectableFakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        backend = await manager.get(_key("user-1"))
        assert isinstance(backend, RootedOpenSandboxBackend)
        sandbox_id = backend.id

        await manager.reset(_key("user-1"))

        assert list(workspace.iterdir()) == []
        assert outside_file.read_text(encoding="utf-8") == "keep"
        assert await manager.reconnect(_key("user-1")) is backend
        assert backend.id == sandbox_id
        assert client.create_calls == 1
        assert client.destroy_calls == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_shared_state_prevents_cross_manager_duplicate_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Share stored claims without advancing unrelated background clocks."""
    from test_pause_resume import _world

    from tinkerfin_sandbox.lifecycle import (
        _manager_availability,
        _sql_state_ops,
        _sql_transactions,
    )

    async with _world(tmp_path) as world:
        with monkeypatch.context() as patch:
            background_tick = asyncio.Event()

            async def parked_renewal(_state: SQLAlchemyOpenSandboxState) -> None:
                await background_tick.wait()

            async def parked_poll(
                _availability: _manager_availability._SandboxAvailability[str],
            ) -> None:
                await background_tick.wait()

            patch.setattr(_sql_transactions, "_renew_worker_loop", parked_renewal)
            patch.setattr(
                _manager_availability._SandboxAvailability, "_poll", parked_poll
            )
            first_manager = await world.add()
            second_manager = await world.add()
            create_entered = asyncio.Event()
            create_release = asyncio.Event()
            claim_waiting = asyncio.Event()
            claim_retry = asyncio.Event()
            claim_caller: asyncio.Task[object] | None = None
            create = world.clients[0].create
            acquire = world.states[1].acquire_owner

            async def blocked_create(
                *,
                purpose: OpenSandboxPurpose = "commands",
                metadata: Mapping[str, str] | None = None,
            ) -> OpenSandboxBackend:
                create_entered.set()
                await create_release.wait()
                return await create(purpose=purpose, metadata=metadata)

            async def observed_acquire(owner_key: str) -> OpenSandboxOwnerClaim:
                nonlocal claim_caller
                claim_caller = asyncio.current_task()
                return await acquire(owner_key)

            async def observe_wait(_seconds: float) -> None:
                assert asyncio.current_task() is claim_caller
                claim_waiting.set()
                await claim_retry.wait()

            controlled = ModuleType("controlled_manager_owner_poll")
            controlled.__dict__.update(vars(asyncio))
            setattr(controlled, "sleep", observe_wait)
            patch.setattr(_sql_state_ops, "asyncio", controlled)
            patch.setattr(world.clients[0], "create", blocked_create)
            patch.setattr(world.states[1], "acquire_owner", observed_acquire)
            first_task = world.spawn(first_manager.get("user-1"))
            try:
                await create_entered.wait()
                second_task = world.spawn(second_manager.get("user-1"))
                await claim_waiting.wait()
                assert world.remote.created == 0
                create_release.set()
                first = await first_task
                assert not second_task.done()
                claim_retry.set()
                second = await second_task
                assert first.id == second.id == "sandbox-1"
                assert world.remote.created == 1
                await first_manager.aclose()
                await second_manager.aclose()
                assert world.remote.destroy_calls == []
            finally:
                create_release.set()
                claim_retry.set()


@pytest.mark.asyncio
async def test_state_recreate_preserves_remote_without_authoritative_ownership() -> (
    None
):
    """Retire only the authoritative instance after cross-process rebinding."""

    client = _ReconnectableFakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        handle = await manager.get(_key("user-1"))
        store.bindings[_resource_key("user-1")] = "sandbox-external"

        recreated = await manager.recreate(_key("user-1"))

        assert recreated is handle
        assert recreated.id == "sandbox-2"
        assert client.destroy_calls == ["sandbox-external"]
        assert "sandbox-1" not in client.destroy_calls
    finally:
        await manager.aclose()


async def test_committed_on_demand_binding_survives_lost_commit_response() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.commit_before_save_error = True
    state.save_error = RuntimeError("database response was lost")
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        handle = await manager.get(_key("user-1"))

        assert handle.id == "sandbox-1"
        assert state.bindings == {_resource_key("user-1"): "sandbox-1"}
        assert state.save_calls == [(_resource_key("user-1"), "sandbox-1")]
        assert len(state.get_calls) == 2
        assert client.destroy_calls == []
        assert client.backends[0].close_calls == 0
    finally:
        await manager.aclose()


async def test_unknown_bind_outcome_closes_without_destroying_candidate() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.save_error = RuntimeError("database write failed")
    state.read_error = RuntimeError("database read failed")
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        with pytest.raises(
            OpenSandboxStateError, match="fake state write failed"
        ) as caught:
            await manager.get(_key("user-1"))

        assert any("reconciliation" in note for note in caught.value.__notes__)
        assert len(state.get_calls) == 2
        assert client.destroy_calls == []
        assert client.backends[0].close_calls == 1
    finally:
        await manager.aclose()
