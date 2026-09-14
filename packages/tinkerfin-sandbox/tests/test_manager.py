from __future__ import annotations

import asyncio
import json
import logging
import threading
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StoreBackend
from deepagents.backends.protocol import ExecuteResponse
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from tests.support.sql_engines import SqlEngineFactory

import tinkerfin_sandbox
from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxBackend,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBinding,
    OpenSandboxConfig,
    OpenSandboxDestroyError,
    OpenSandboxDetails,
    OpenSandboxDiagnosticContent,
    OpenSandboxHandle,
    OpenSandboxHandleOwnershipError,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxOwnerClaim,
    OpenSandboxPlatformInfo,
    OpenSandboxRecoveryPolicy,
    OpenSandboxResetError,
    OpenSandboxRuntimeInfo,
    OpenSandboxStateError,
    OpenSandboxStatusInfo,
    OpenSandboxUnavailableReason,
    OpenSandboxWarmClaim,
    RootedOpenSandboxBackend,
    SQLAlchemyOpenSandboxState,
    UnexpectedOpenSandboxBackendError,
    UnexpectedOpenSandboxStateError,
)
from tinkerfin_sandbox.backends import _rooted_protocol


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


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """Allow deterministic responses to invoke Deep Agents tools."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _ExecuteResponse:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


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
        self.close_gate: threading.Event | None = None
        self.runtime_info_calls = 0
        self.block_command: str | None = None
        self.execute_entered = threading.Event()
        self.execute_gate = threading.Event()

    def execute(self, command: str, *, timeout: int | None = None) -> _ExecuteResponse:
        self.execute_calls.append((command, timeout))
        if command == self.block_command:
            self.execute_entered.set()
            if not self.execute_gate.wait(timeout=1):
                raise TimeoutError("test execution gate was not released")
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
            released = await asyncio.to_thread(self.execute_gate.wait, 1)
            if not released:
                raise TimeoutError("test execution gate was not released")
        return _ExecuteResponse(0 if self.healthy else 1)

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.close_gate is not None and not self.close_gate.wait(timeout=1):
            raise TimeoutError("test close gate was not released")

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    async def akill(self) -> None:
        self.kill_calls += 1

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info


class _RenewFailingBackend(_FakeBackend):
    """Keep data-plane health while making remote expiry renewal unavailable."""

    async def arenew(self, timeout: timedelta) -> None:
        del timeout
        raise RuntimeError("renew unavailable")


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
        self.connect_calls: list[str] = []
        self.inspect_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.destroy_errors: dict[str, Exception] = {}
        self.destroy_entered = asyncio.Event()
        self.destroy_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.create_gate: asyncio.Event | None = None
        self.release_after_create_count: int | None = None
        self.close_calls = 0

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
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
        self.backends.append(backend)
        return cast(OpenSandboxBackend, backend)

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_calls.append(sandbox_id)
        try:
            backend = self.connected[sandbox_id]
        except KeyError as error:
            raise OpenSandboxBackendUnavailableError(
                "Sandbox not found",
                context={"reason": "not_found"},
                cause=error,
            ) from error
        return cast(OpenSandboxBackend, backend)

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        self.inspect_calls.append(sandbox_id)
        return self.inspection_results[sandbox_id]

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


class _FakeState(InMemoryOpenSandboxState):
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        super().__init__()
        self.bindings = dict(bindings or {})
        self.binding_generations = {owner_key: 0 for owner_key in self.bindings}
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
            binding = await super().bind_owner(claim, desired_id)
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
        )

    async def bind_owner(self, claim, sandbox_id: str) -> OpenSandboxBinding:
        self.save_calls.append((claim.owner_key, sandbox_id))
        if self.commit_before_save_error or self.cancel_after_save_commit:
            binding = await super().bind_owner(claim, sandbox_id)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
        if self.cancel_after_save_commit:
            raise asyncio.CancelledError("bind response was cancelled after commit")
        if self.save_error is not None:
            raise OpenSandboxStateError("fake state write failed") from self.save_error
        if not self.commit_before_save_error:
            binding = await super().bind_owner(claim, sandbox_id)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
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

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return ()


class _WarmStateWithoutReconciliation(InMemoryOpenSandboxState):
    """Represent a custom State that has not implemented ready-slot fencing."""

    def __getattribute__(self, name: str) -> object:
        if name in {
            "claim_ready_warm_slot",
            "discard_ready_warm_slot",
            "warm_pool_ready",
        }:
            raise AttributeError(name)
        return super().__getattribute__(name)


class _LeakingState(_FakeState):
    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        del owner_key
        raise RuntimeError("driver-specific state failure")


class _BlockingOwnerReleaseState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_cancelled = asyncio.Event()
        self.claim: OpenSandboxOwnerClaim | None = None

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        self.claim = claim
        self.release_started.set()
        try:
            await self.release_gate.wait()
        except asyncio.CancelledError:
            self.release_cancelled.set()
            raise
        await super().release_owner(claim)


class _BlockingWarmReleaseState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_cancelled = asyncio.Event()
        self.claim: OpenSandboxWarmClaim | None = None
        self.close_calls = 0

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        self.claim = claim
        self.release_started.set()
        try:
            await self.release_gate.wait()
        except asyncio.CancelledError:
            self.release_cancelled.set()
            raise
        await super().release_warm(claim)

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


class _ObservedWarmCreateClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.current_create_task: asyncio.Task[object] | None = None

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("warm creation requires an asyncio task")
        self.current_create_task = cast(asyncio.Task[object], current)
        return await super().create(metadata=metadata)


class _CancelledWarmupClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        raise asyncio.CancelledError("warm creation cancelled")


class _CloseObservedState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


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
        metadata={"region": "local"},
        unavailable_reason=unavailable_reason,
    )


async def _eventually(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class _NotificationLoop:
    def __init__(self, *, close_during_notification: bool) -> None:
        self._closed = False
        self.close_during_notification = close_during_notification

    def is_closed(self) -> bool:
        return self._closed

    def call_soon_threadsafe(
        self,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        del callback, args
        if self.close_during_notification:
            self._closed = True
        raise RuntimeError("test loop rejected notification")


def _append_idle_waiter(
    handle: OpenSandboxHandle,
    backend: _FakeBackend,
    loop: asyncio.AbstractEventLoop,
    waiter: asyncio.Future[None],
) -> None:
    with handle._condition:
        handle._idle_waiters.setdefault(id(backend), []).append((loop, waiter))


def test_closed_waiter_loop_does_not_replace_lease_body_failure() -> None:
    backend = _FakeBackend("sandbox-1")
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, backend))
    loop = asyncio.new_event_loop()
    waiter = loop.create_future()
    body_error = ValueError("lease body failed")

    try:
        with pytest.raises(ValueError) as captured:
            with handle._lease() as leased:
                assert leased is backend
                _append_idle_waiter(handle, backend, loop, waiter)
                loop.close()
                raise body_error
    finally:
        if not loop.is_closed():
            loop.close()

    assert captured.value is body_error


@pytest.mark.parametrize("close_during_notification", (False, True))
async def test_waiter_notification_suppresses_only_confirmed_loop_close(
    close_during_notification: bool,
) -> None:
    backend = _FakeBackend("sandbox-1")
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, backend))
    loop = _NotificationLoop(
        close_during_notification=close_during_notification,
    )
    waiter = asyncio.get_running_loop().create_future()

    if close_during_notification:
        with handle._lease():
            _append_idle_waiter(
                handle,
                backend,
                cast(asyncio.AbstractEventLoop, loop),
                waiter,
            )
    else:
        with pytest.raises(RuntimeError, match="rejected notification"):
            with handle._lease():
                _append_idle_waiter(
                    handle,
                    backend,
                    cast(asyncio.AbstractEventLoop, loop),
                    waiter,
                )


class OpenSandboxManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.managers: list[OpenSandboxManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.aclose()

    async def _manager(
        self,
        client: _FakeClient,
        store: _FakeState | None = None,
        *,
        warm_pool_size: int = 0,
        recovery_policy: OpenSandboxRecoveryPolicy | None = None,
    ) -> OpenSandboxManager:
        manager = _new_manager(
            client=client,
            state=store,
            warm_pool_size=warm_pool_size,
            recovery_policy=recovery_policy,
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

    async def test_different_owners_can_create_sandboxes_concurrently(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        client.release_after_create_count = 2
        manager = await self._manager(client, _FakeState())

        first_handle, second_handle = await asyncio.wait_for(
            asyncio.gather(manager.get(_key("user-1")), manager.get(_key("user-2"))),
            timeout=1,
        )

        self.assertEqual(client.create_calls, 2)
        self.assertNotEqual(first_handle.id, second_handle.id)

    async def test_key_resolver_can_share_one_sandbox_across_inputs(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = OpenSandboxManager(
            client=client,
            key_resolver=lambda _key: "global",
            state=store,
            warm_pool_size=0,
        )
        self.managers.append(manager)
        await manager.start()

        first, other = await asyncio.gather(
            manager.get("request-a"),
            manager.get("request-b"),
        )

        self.assertIs(first, other)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(store.save_calls, [(_resource_key("global"), "sandbox-1")])

    async def test_store_recovery_takes_priority_over_warm_sandbox(self) -> None:
        client = _FakeClient()
        restored = _FakeBackend("persisted-id")
        client.connected[restored.id] = restored
        store = _FakeState({_resource_key("user-1"): restored.id})
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, restored.id)
        self.assertEqual(client.connect_calls, [restored.id])
        self.assertEqual(client.create_calls, 1, "only the warm reserve is created")
        self.assertEqual(store.save_calls, [])

    async def test_get_claims_warm_sandbox_and_replenishes_pool(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(store.consume_calls, [(_resource_key("user-1"), "sandbox-1")])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {_resource_key("user-1"): "sandbox-1"})
        self.assertEqual(
            [backend.id for backend in client.backends],
            [
                "sandbox-1",
                "sandbox-2",
            ],
        )

    async def test_unhealthy_warm_sandbox_is_destroyed_before_binding(self) -> None:
        def backend_factory(sandbox_id: str) -> _FakeBackend:
            return _FakeBackend(sandbox_id, healthy=sandbox_id != "sandbox-1")

        client = _FakeClient(backend_factory)
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))

        self.assertNotEqual(handle.id, "sandbox-1")
        self.assertEqual(client.backends[0].execute_calls, [("echo ok", None)])
        self.assertIn("sandbox-1", client.destroy_calls)
        self.assertEqual(client.backends[0].close_calls, 1)
        self.assertNotIn((_resource_key("user-1"), "sandbox-1"), store.save_calls)

    async def test_cached_healthy_sandbox_is_reused_and_renewed(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, _FakeState())
        first_handle = await manager.get(_key("user-1"))
        backend = client.backends[0]
        health_count = len(backend.execute_calls)
        renew_count = len(backend.renew_calls)

        second_handle = await manager.get(_key("user-1"))

        self.assertIs(second_handle, first_handle)
        self.assertEqual(len(backend.execute_calls), health_count + 1)
        self.assertEqual(len(backend.renew_calls), renew_count + 1)
        self.assertEqual(client.create_calls, 1)

    async def test_is_healthy_never_creates_or_renews_sandbox(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, _FakeState())

        self.assertFalse(await manager.is_healthy(_key("user-1")))
        self.assertEqual(client.create_calls, 0)

        await manager.get(_key("user-1"))
        backend = client.backends[0]
        renew_count = len(backend.renew_calls)
        health_count = len(backend.execute_calls)

        self.assertTrue(await manager.is_healthy(_key("user-1")))
        self.assertEqual(len(backend.renew_calls), renew_count)
        self.assertEqual(len(backend.execute_calls), health_count + 1)
        self.assertEqual(client.create_calls, 1)

    async def test_recreate_hot_swaps_existing_open_handle(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))

        recreated = await manager.recreate(_key("user-1"))

        self.assertIs(recreated, handle)
        self.assertEqual(recreated.id, "sandbox-2")
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(store.bindings, {_resource_key("user-1"): "sandbox-2"})

    async def test_unhealthy_persisted_binding_is_replaced(self) -> None:
        restored = _FakeBackend("persisted-id", healthy=False)
        client = _FakeClient()
        client.connected[restored.id] = restored
        store = _FakeState({_resource_key("user-1"): restored.id})
        manager = await self._manager(
            client,
            store,
            recovery_policy=OpenSandboxRecoveryPolicy(
                max_attempts=1, on_failure="recreate"
            ),
        )

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(client.connect_calls, [restored.id])
        self.assertEqual(restored.close_calls, 1)
        self.assertEqual(client.destroy_calls, [restored.id])
        self.assertEqual(store.bindings, {_resource_key("user-1"): handle.id})

    async def test_unhealthy_sandbox_is_hot_replaced_on_stable_handle(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(
            client,
            store,
            recovery_policy=OpenSandboxRecoveryPolicy(
                max_attempts=1, on_failure="recreate"
            ),
        )
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.healthy = False

        replacement_handle = await manager.get(_key("user-1"))

        self.assertIs(replacement_handle, handle)
        self.assertEqual(handle.id, "sandbox-2")
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(
            store.save_calls,
            [
                (_resource_key("user-1"), "sandbox-1"),
                (_resource_key("user-1"), "sandbox-2"),
            ],
        )

    async def test_hot_replacement_waits_for_in_flight_backend_call(self) -> None:
        client = _FakeClient()
        manager = await self._manager(
            client,
            _FakeState(),
            recovery_policy=OpenSandboxRecoveryPolicy(
                max_attempts=1, on_failure="recreate"
            ),
        )
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"

        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        entered = await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        old_backend.healthy = False
        replace_task = asyncio.create_task(manager.get(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")

        self.assertFalse(replace_task.done())
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(old_backend.close_calls, 0)
        with self.assertRaises(OpenSandboxHandleOwnershipError):
            handle.close()
        self.assertEqual(client.backends[1].close_calls, 0)

        old_backend.execute_gate.set()
        await execute_task
        replaced = await replace_task

        self.assertIs(replaced, handle)
        self.assertFalse(handle.is_closed)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(client.backends[1].close_calls, 0)

    async def test_cancelled_hot_replacement_tracks_old_backend_cleanup(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(
            client,
            store,
            recovery_policy=OpenSandboxRecoveryPolicy(
                max_attempts=1, on_failure="recreate"
            ),
        )
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"

        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        entered = await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        old_backend.healthy = False
        replace_task = asyncio.create_task(manager.get(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")

        replace_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await replace_task
        old_backend.execute_gate.set()
        await execute_task

        # Model the window after task completion but before its callback removes it.
        stale_cleanup_task = asyncio.create_task(asyncio.sleep(0))
        await stale_cleanup_task
        manager._cleanup_tasks.add(stale_cleanup_task)
        await manager.aclose()

        self.assertEqual(store.bindings, {_resource_key("user-1"): "sandbox-2"})
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(client.backends[1].close_calls, 1)

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

    async def test_committed_warm_binding_skips_redundant_bind(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        store.commit_before_save_error = True
        store.save_error = RuntimeError("response lost after commit")

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(store.consume_calls, [(_resource_key("user-1"), "sandbox-1")])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {_resource_key("user-1"): "sandbox-1"})
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(client.backends[0].close_calls, 0)

    async def test_manager_owned_handle_rejects_public_close(
        self,
    ) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))

        with self.assertRaises(OpenSandboxHandleOwnershipError):
            handle.close()
        same_handle = await manager.get(_key("user-1"))

        self.assertIs(same_handle, handle)
        self.assertEqual(store.bindings, {_resource_key("user-1"): "sandbox-1"})
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(client.backends[0].close_calls, 0)

    async def test_delete_destroys_sandbox_and_removes_binding(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        await manager.get(_key("user-1"))
        backend = client.backends[0]

        await manager.delete(_key("user-1"))

        self.assertEqual(client.destroy_calls, [backend.id])
        self.assertEqual(backend.close_calls, 1)
        self.assertEqual(store.delete_calls, [_resource_key("user-1")])
        self.assertNotIn(_resource_key("user-1"), store.bindings)

    async def test_delete_uses_store_binding_without_connecting(self) -> None:
        client = _FakeClient()
        store = _FakeState({_resource_key("user-1"): "persisted-id"})
        manager = await self._manager(client, store)

        await manager.delete(_key("user-1"))

        self.assertEqual(client.destroy_calls, ["persisted-id"])
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(store.delete_calls, [_resource_key("user-1")])

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

    async def test_delete_failure_without_store_retains_retry_target(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client)
        handle = await manager.get(_key("user-1"))
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        with self.assertRaises(OpenSandboxDestroyError):
            await manager.delete(_key("user-1"))

        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_cancelled_delete_retains_retry_target_without_store(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client)
        handle = await manager.get(_key("user-1"))
        client.destroy_gate = asyncio.Event()
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        delete_task = asyncio.create_task(manager.delete(_key("user-1")))
        await client.destroy_entered.wait()
        delete_task.cancel()
        client.destroy_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await delete_task

        client.destroy_gate = None
        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_concurrent_start_and_close_share_startup_completion(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        manager = _new_manager(client=client, warm_pool_size=1)
        self.managers.append(manager)

        first_start = asyncio.create_task(manager.start())
        await client.create_entered.wait()
        second_start = asyncio.create_task(manager.start())
        close_task = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        second_finished_early = second_start.done()
        close_finished_early = close_task.done()

        client.create_gate.set()
        await asyncio.gather(first_start, second_start, close_task)

        self.assertFalse(second_finished_early)
        self.assertFalse(close_finished_early)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(client.backends[0].close_calls, 1)

    async def test_aclose_preserves_persistent_warm_and_bound_remote_sandboxes(
        self,
    ) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        handle = await manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)
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

    async def test_aclose_waits_for_in_flight_get_and_closes_its_backend(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        store = _FakeState()
        manager = await self._manager(client, store)

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        close_task = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)

        self.assertFalse(close_task.done())
        client.create_gate.set()
        handle = await get_task
        await close_task

        self.assertEqual(store.bindings, {_resource_key("user-1"): handle.id})
        self.assertEqual(client.backends[0].close_calls, 1)
        self.assertEqual(client.destroy_calls, [])

    async def test_concurrent_aclose_callers_wait_for_the_same_cleanup(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        manager = await self._manager(client, _FakeState())

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        first_close = asyncio.create_task(manager.aclose())
        second_close = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)

        self.assertFalse(first_close.done())
        self.assertFalse(second_close.done())
        client.create_gate.set()
        await get_task
        await asyncio.gather(first_close, second_close)
        self.assertEqual(client.backends[0].close_calls, 1)

    async def test_cancelled_aclose_caller_does_not_cancel_shared_cleanup(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, warm_pool_size=1)
        backend = client.backends[0]
        backend.close_gate = threading.Event()

        first_close = asyncio.create_task(manager.aclose())
        entered = await asyncio.to_thread(backend.close_entered.wait, 1)
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

    async def test_cancelled_committed_warm_health_check_closes_locally(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        backend = client.backends[0]
        backend.block_command = "echo ok"

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        entered = await asyncio.to_thread(backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        get_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await get_task
        backend.execute_gate.set()

        await manager.aclose()

        self.assertEqual(store.bindings, {_resource_key("user-1"): backend.id})
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(backend.close_calls, 1)

    async def test_cancelled_recovery_health_check_closes_candidate(self) -> None:
        restored = _FakeBackend("persisted-id")
        restored.block_command = "echo ok"
        client = _FakeClient()
        client.connected[restored.id] = restored
        manager = await self._manager(
            client,
            _FakeState({_resource_key("user-1"): restored.id}),
        )

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        entered = await asyncio.to_thread(restored.execute_entered.wait, 1)
        self.assertTrue(entered)
        get_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await get_task
        restored.execute_gate.set()

        await manager.aclose()

        self.assertEqual(restored.close_calls, 1)
        self.assertEqual(client.destroy_calls, [])


class OpenSandboxDetailsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.managers: list[OpenSandboxManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.aclose()

    async def _manager(
        self,
        client: _FakeClient,
        store: _FakeState,
    ) -> OpenSandboxManager:
        manager = _new_manager(
            client=client,
            state=store,
            warm_pool_size=0,
        )
        self.managers.append(manager)
        await manager.start()
        return manager

    async def test_no_binding_returns_none_without_sandbox_side_effects(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)

        details = await manager.get_details(_key("user-1"))

        self.assertIsNone(details)
        self.assertEqual(store.get_calls, [_resource_key("user-1")])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(store.save_calls, [])

    async def test_memory_details_use_backend_without_renewing_or_inspecting(
        self,
    ) -> None:
        info = _runtime_info("sandbox-1")
        client = _FakeClient(
            lambda sandbox_id: _FakeBackend(sandbox_id, runtime_info=info)
        )
        store = _FakeState()
        manager = await self._manager(client, store)
        await manager.get(_key("user-1"))
        backend = client.backends[0]
        renew_count = len(backend.renew_calls)
        create_count = client.create_calls
        save_count = len(store.save_calls)

        details = await manager.get_details(_key("user-1"))

        self.assertIsInstance(details, OpenSandboxDetails)
        assert details is not None
        self.assertEqual(details.owner_key, "user-1")
        self.assertEqual(details.sandbox_id, info.sandbox_id)
        self.assertTrue(details.cached)
        self.assertEqual(details.available, info.available)
        self.assertEqual(details.healthy, info.healthy)
        self.assertEqual(details.status, info.status)
        self.assertEqual(details.created_at, info.created_at)
        self.assertEqual(details.expires_at, info.expires_at)
        self.assertEqual(details.image, info.image)
        self.assertEqual(details.platform, info.platform)
        self.assertEqual(details.metadata, info.metadata)
        self.assertIsNone(details.unavailable_reason)
        self.assertEqual(backend.runtime_info_calls, 1)
        self.assertEqual(len(backend.renew_calls), renew_count)
        self.assertEqual(client.create_calls, create_count)
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(len(store.save_calls), save_count)

    async def test_store_details_inspect_without_connecting_caching_or_mutating(
        self,
    ) -> None:
        info = _runtime_info("persisted-id")
        client = _FakeClient()
        client.inspection_results[info.sandbox_id] = info
        store = _FakeState({_resource_key("user-1"): info.sandbox_id})
        manager = await self._manager(client, store)

        first = await manager.get_details(_key("user-1"))
        second = await manager.get_details(_key("user-1"))

        self.assertIsInstance(first, OpenSandboxDetails)
        assert first is not None
        self.assertEqual(first.owner_key, "user-1")
        self.assertEqual(first.sandbox_id, info.sandbox_id)
        self.assertFalse(first.cached)
        self.assertEqual(second, first)
        self.assertEqual(client.inspect_calls, [info.sandbox_id, info.sandbox_id])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {_resource_key("user-1"): info.sandbox_id})

    async def test_unavailable_store_details_remain_read_only(self) -> None:
        for reason in ("not_found", "unreachable"):
            with self.subTest(reason=reason):
                info = _runtime_info(
                    "persisted-id",
                    available=False,
                    healthy=False,
                    unavailable_reason=reason,
                )
                client = _FakeClient()
                client.inspection_results[info.sandbox_id] = info
                store = _FakeState({_resource_key("user-1"): info.sandbox_id})
                manager = await self._manager(client, store)

                first = await manager.get_details(_key("user-1"))
                second = await manager.get_details(_key("user-1"))

                assert first is not None
                self.assertFalse(first.available)
                self.assertFalse(first.healthy)
                self.assertEqual(first.unavailable_reason, reason)
                self.assertEqual(second, first)
                self.assertEqual(
                    client.inspect_calls, [info.sandbox_id, info.sandbox_id]
                )
                self.assertEqual(client.create_calls, 0)
                self.assertEqual(client.connect_calls, [])
                self.assertEqual(store.save_calls, [])

    async def test_store_failure_is_not_reported_as_missing_binding(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        store.get_error = RuntimeError("database unavailable")
        manager = await self._manager(client, store)

        with self.assertRaises(OpenSandboxStateError) as context:
            await manager.get_details(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])


class _ReconnectableFakeClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        backend = await super().create(metadata=metadata)
        self.connected[backend.id] = backend
        return backend


class _FailingCreateClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        raise RuntimeError("warmup failed")


class _AuthenticationFailingClient(_FailingCreateClient):
    """Reject every remote operation without claiming that a valid ID is missing."""

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_calls.append(sandbox_id)
        raise UnexpectedOpenSandboxBackendError("authentication rejected")


class _TimeoutThenInspectClient(_FakeClient):
    """Expose a reconnect timeout followed by one authoritative query result."""

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_calls.append(sandbox_id)
        raise OpenSandboxBackendTimeoutError("reconnect timed out")


class _BlockingCloseFailingCreateClient(_FailingCreateClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.release_close.wait()
        await super().aclose()


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


def _install_reset_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opened: Path,
    release: Path,
) -> None:
    original_script = _rooted_protocol._ROOTED_HELPER_SCRIPT
    function_start = original_script.index("def reset_workspace(")
    function_end = original_script.find("\ndef ", function_start + 1)
    function_source = original_script[function_start:function_end]
    statement = "        opened = os.fstat(root_descriptor)"
    barrier = (
        statement
        + "\n"
        + f"        open({str(opened)!r}, 'x').close()\n"
        + f"        while not os.path.exists({str(release)!r}):\n"
        + "            time.sleep(0.001)"
    )
    assert function_source.count(statement) == 1
    monkeypatch.setattr(
        _rooted_protocol,
        "_ROOTED_HELPER_SCRIPT",
        (
            original_script[:function_start]
            + function_source.replace(statement, barrier)
            + original_script[function_end:]
        ),
    )


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
async def test_manager_reconnect_requires_an_existing_binding() -> None:
    client = _FakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await manager.reconnect(_key("user-1"))
        assert client.create_calls == 0
        assert store.bindings == {}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", (timedelta(hours=2), None))
async def test_default_recovery_preserves_an_unhealthy_user_instance(
    ttl: timedelta | None,
) -> None:
    """An unsuccessful health check cannot authorize deleting the user's files."""

    client = _FakeClient()
    client.config = client.config.model_copy(update={"ttl": ttl})
    state = _FakeState()
    manager = _new_manager(client=client, state=state)
    await manager.start()
    try:
        handle = await manager.get("owner")
        backend = cast(_FakeBackend, client.backends[0])
        backend.healthy = False
        client.connected[backend.id] = backend
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await manager.get("owner")
        assert handle.id == backend.id
        assert state.bindings == {_resource_key("owner"): backend.id}
        assert client.create_calls == 1
        assert client.destroy_calls == []
        assert backend.kill_calls == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manager_lifecycle_uses_backend_coroutines_without_thread_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    thread_bridge_calls = 0

    async def reject_thread_bridge(*_args: object, **_kwargs: object) -> None:
        nonlocal thread_bridge_calls
        thread_bridge_calls += 1
        raise AssertionError("manager lifecycle must not use asyncio.to_thread")

    try:
        with monkeypatch.context() as context:
            context.setattr(asyncio, "to_thread", reject_thread_bridge)
            await manager.get(_key("user-1"))
        assert thread_bridge_calls == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_standalone_rooted_middleware_enforces_route_permissions(
    tmp_path: Path,
) -> None:
    """Use the standalone Sandbox integration with Deep Agents directly."""

    from tinkerfin_sandbox import build_rooted_filesystem_middleware

    store = InMemoryStore()
    backend = CompositeBackend(
        default=LocalShellBackend(
            root_dir=tmp_path,
            virtual_mode=False,
            inherit_env=True,
        ),
        routes={
            "/policies/": StoreBackend(
                namespace=lambda _runtime: ("tests", "manager-policies")
            )
        },
    )
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/private/**"],
            mode="deny",
        )
    ]
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/private/rule.md",
                            "content": "blocked\n",
                        },
                        "id": "call-manager-private-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="write attempted"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=[
            build_rooted_filesystem_middleware(backend, permissions=permissions)
        ],
        permissions=permissions,
        store=store,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "写入私有策略"}]}
    )

    write_result = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "write_file"
    )
    assert write_result.status == "error"
    assert "permission denied" in str(write_result.content).lower()
    assert (
        await store.aget(
            ("tests", "manager-policies"),
            "/private/rule.md",
        )
        is None
    )


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
async def test_reset_rejects_workspace_root_replaced_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    opened = tmp_path / "reset-opened"
    release = tmp_path / "reset-release"
    _install_reset_barrier(
        monkeypatch,
        opened=opened,
        release=release,
    )
    client = _FakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("user-1"))
        reset_task = asyncio.create_task(manager.reset(_key("user-1")))
        deadline = asyncio.get_running_loop().time() + 1
        while not opened.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        if not opened.exists():
            await reset_task
        assert opened.exists(), "manager reset did not use the rooted helper"
        workspace.rename(tmp_path / "detached-workspace")
        workspace.symlink_to(outside, target_is_directory=True)
        release.touch()

        with pytest.raises(OpenSandboxResetError):
            await reset_task

        assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
        assert list((tmp_path / "detached-workspace").iterdir()) == []
    finally:
        release.touch(exist_ok=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_response_loss_is_reported_without_replay(tmp_path: Path) -> None:
    class ResponseLossBackend(_ResettableLocalBackend):
        def __init__(self, sandbox_id: str, workspace: Path) -> None:
            super().__init__(sandbox_id, workspace)
            self.reset_calls = 0

        async def aexecute(
            self,
            command: str,
            *,
            timeout: int | None = None,
        ) -> ExecuteResponse:
            if command == "echo ok":
                return await super().aexecute(command, timeout=timeout)
            self.reset_calls += 1
            await super().aexecute(command, timeout=timeout)
            raise ConnectionError("reset response lost")

    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    created: list[ResponseLossBackend] = []

    def backend_factory(sandbox_id: str) -> ResponseLossBackend:
        backend = ResponseLossBackend(sandbox_id, workspace)
        created.append(backend)
        return backend

    client = _FakeClient(backend_factory)
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("user-1"))

        with pytest.raises(OpenSandboxResetError):
            await manager.reset(_key("user-1"))

        assert created[0].reset_calls == 1
        assert list(workspace.iterdir()) == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_requires_a_managed_key() -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": None})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        with pytest.raises(OpenSandboxResetError, match="workspace_root"):
            await manager.reset(_key("user-1"))
        assert client.create_calls == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_strict_startup_warmup_propagates_without_package_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Require strict warmup failure to prevent the manager becoming available."""

    manager = _new_manager(
        client=_FailingCreateClient(),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with caplog.at_level(logging.DEBUG, logger="tinkerfin.sandbox"):
        with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
            await manager.start()
        await manager.aclose()
    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "warmup failed"
    assert caplog.records == []


@pytest.mark.asyncio
async def test_strict_startup_replaces_a_published_but_missing_warm_sandbox() -> None:
    """A non-empty durable slot must not bypass remote startup verification."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    claim = await state.claim_warm_slot()
    assert claim is not None
    await state.publish_warm(claim, "missing-warm")
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    try:
        await manager.start()
        await manager.check_ready()
        assert client.connect_calls == ["missing-warm"]
        assert client.create_calls == 1
        await _eventually(lambda: client.destroy_calls == ["missing-warm"])
        assert client.destroy_calls == ["missing-warm"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", (timedelta(hours=2), None))
async def test_sql_state_restart_reuses_verified_warm_capacity(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    ttl: timedelta | None,
) -> None:
    """A later manager must reconnect the same healthy durable warm Sandbox."""

    url = f"sqlite+aiosqlite:///{tmp_path / 'warm-restart.db'}"
    first_client = _FakeClient()
    first_client.config = first_client.config.model_copy(update={"ttl": ttl})
    first = _new_manager(
        client=first_client,
        state=SQLAlchemyOpenSandboxState(
            engine=sql_engine(url), namespace="warm-restart"
        ),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    await first.start()
    warm_id = first_client.backends[0].id
    await first.aclose()
    assert first_client.destroy_calls == []

    second_client = _FakeClient()
    second_client.config = second_client.config.model_copy(update={"ttl": ttl})
    second_backend = _FakeBackend(warm_id)
    second_client.connected[warm_id] = second_backend
    second = _new_manager(
        client=second_client,
        state=SQLAlchemyOpenSandboxState(
            engine=sql_engine(url), namespace="warm-restart"
        ),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    try:
        await second.start()
        await second.check_ready()
        assert second_client.connect_calls == [warm_id]
        assert second_client.create_calls == 0
        assert second_backend.renew_calls == ([] if ttl is None else [ttl])
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_strict_startup_rejects_stale_capacity_when_replacement_fails() -> None:
    """A stale published ID and failed create must prevent manager startup."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    claim = await state.claim_warm_slot()
    assert claim is not None
    await state.publish_warm(claim, "missing-warm")
    manager = _new_manager(
        client=_FailingCreateClient(),
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError):
        await manager.start()
    await manager.aclose()


@pytest.mark.asyncio
async def test_authentication_failure_preserves_a_published_warm_binding() -> None:
    """An unverifiable reconnect must fail startup without discarding valid State."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    claim = await state.claim_warm_slot()
    assert claim is not None
    await state.publish_warm(claim, "warm-existing")
    client = _AuthenticationFailingClient()
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError):
        await manager.start()
    assert await state.warm_pool_ready()
    assert client.create_calls == 0
    assert client.destroy_calls == []
    await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["Failed", "Stopping", "Terminated"])
async def test_terminal_status_after_timeout_replaces_published_warm(
    terminal_state: str,
) -> None:
    """A control-plane terminal state confirms that timed-out data is unusable."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    claim = await state.claim_warm_slot()
    assert claim is not None
    await state.publish_warm(claim, "failed-warm")
    client = _TimeoutThenInspectClient()
    client.inspection_results["failed-warm"] = _runtime_info(
        "failed-warm",
        healthy=False,
        state=terminal_state,
    )
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    try:
        await manager.start()
        await manager.check_ready()
        assert client.connect_calls == ["failed-warm"]
        assert client.inspect_calls == ["failed-warm"]
        assert client.create_calls == 1
        await _eventually(lambda: client.destroy_calls == ["failed-warm"])
        assert client.destroy_calls == ["failed-warm"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unreachable_inspection_after_timeout_preserves_published_warm() -> None:
    """A secondary query outage must not turn a timeout into destructive certainty."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    claim = await state.claim_warm_slot()
    assert claim is not None
    await state.publish_warm(claim, "unverified-warm")
    client = _TimeoutThenInspectClient()
    client.inspection_results["unverified-warm"] = _runtime_info(
        "unverified-warm",
        available=False,
        healthy=False,
        unavailable_reason="unreachable",
    )
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with pytest.raises(OpenSandboxBackendTimeoutError):
        await manager.start()
    assert await state.warm_pool_ready()
    assert client.inspect_calls == ["unverified-warm"]
    assert client.create_calls == 0
    assert client.destroy_calls == []
    await manager.aclose()


@pytest.mark.asyncio
async def test_failed_stale_replacement_cannot_be_consumed_as_ready_capacity() -> None:
    """Invalidated warm IDs must not move into an owner binding during an outage."""

    state = _FakeState()
    await state.start(warm_pool_size=1)
    warm_claim = await state.claim_warm_slot()
    assert warm_claim is not None
    await state.publish_warm(warm_claim, "missing-warm")
    manager = _new_manager(
        client=_FailingCreateClient(),
        state=state,
        warm_pool_size=1,
        fail_on_startup_warmup_error=False,
    )
    await manager.start()
    try:
        with pytest.raises(tinkerfin_sandbox.OpenSandboxWarmPoolUnavailableError):
            await manager.check_ready()
        owner_claim = await state.acquire_owner("probe-owner")
        try:
            assert await state.consume_warm(owner_claim) is None
        finally:
            await state.release_owner(owner_claim)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_strict_warm_pool_requires_ready_slot_reconciliation_state() -> None:
    """A custom State cannot claim truthful readiness without the current fence API."""

    manager = _new_manager(
        client=_FakeClient(),
        state=_WarmStateWithoutReconciliation(),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with pytest.raises(tinkerfin_sandbox.OpenSandboxWarmPoolUnavailableError):
        await manager.start()
    await manager.aclose()


async def test_manual_cleanup_owner_reuse_keeps_health_checks_without_renewal() -> None:
    """Manual cleanup preserves health, inspection, identity, and close ownership."""

    client = _ReconnectableFakeClient()
    client.config = client.config.model_copy(update={"ttl": None})
    manager = _new_manager(client=client, warm_pool_size=0)
    async with manager:
        first = await manager.get("project")
        backend = cast(_FakeBackend, client.backends[0])
        backend.runtime_info = backend.runtime_info.model_copy(
            update={"expires_at": None}
        )
        assert await manager.get("project") is first
        assert await manager.reconnect("project") is first
        details = await manager.get_details("project")
        assert details is not None
        assert details.available and details.healthy
        assert details.expires_at is None
        assert details.model_dump(mode="json")["expires_at"] is None
        assert backend.execute_calls
        assert backend.renew_calls == []
    assert client.destroy_calls == [first.id]
    assert backend.close_calls >= 1


async def test_manual_cleanup_warm_health_maintenance_reclaims_failed_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-expiring warm instance still receives bounded periodic health checks."""

    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle._manager_resources._WARM_MAINTENANCE_MAX_SECONDS",
        0.05,
    )
    checked = asyncio.Event()

    class CheckedBackend(_FakeBackend):
        async def aexecute(
            self, command: str, *, timeout: int | None = None
        ) -> _ExecuteResponse:
            response = await super().aexecute(command, timeout=timeout)
            checked.set()
            return response

    client = _ReconnectableFakeClient(backend_factory=CheckedBackend)
    client.config = client.config.model_copy(update={"ttl": None})
    manager = _new_manager(
        client=client, warm_pool_size=1, fail_on_startup_warmup_error=True
    )
    async with manager:
        backend = cast(CheckedBackend, client.backends[0])
        await manager.check_ready()
        checked.clear()
        backend.healthy = False
        await asyncio.wait_for(checked.wait(), timeout=2)
        await _eventually(lambda: client.create_calls == 2)
        await _eventually(lambda: backend.id in client.destroy_calls)
        await manager.check_ready()
        assert all(not item.renew_calls for item in client.backends)
    assert set(client.destroy_calls) == {item.id for item in client.backends}


@pytest.mark.asyncio
async def test_owner_reuse_remains_available_when_best_effort_renewal_fails() -> None:
    """Warm readiness changes must not weaken the established owner reuse path."""

    client = _FakeClient(backend_factory=_RenewFailingBackend)
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        first = await manager.get(_key("user-1"))
        second = await manager.get(_key("user-1"))
        assert second is first
        assert client.create_calls == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_shared_readiness_distinguishes_verification_from_consumption(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    """A worker must observe shared capacity without treating claims as failures."""

    url = f"sqlite+aiosqlite:///{tmp_path / 'readiness.db'}"
    state = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="readiness")
    peer = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="readiness")
    manager = _new_manager(client=_FakeClient(), state=state, warm_pool_size=1)
    await manager.start()
    await peer.start(warm_pool_size=1)
    try:
        claim = await peer.claim_ready_warm_slot(exclude_slots=())
        assert claim is not None
        assert await peer.warm_pool_ready()
        await manager.check_ready()
        await peer.discard_ready_warm_slot(claim)
        with pytest.raises(tinkerfin_sandbox.OpenSandboxWarmPoolUnavailableError):
            await manager.check_ready()
        replacement = await peer.claim_warm_slot()
        assert replacement is not None
        await peer.publish_warm(replacement, "peer-verified")
        await manager.check_ready()
    finally:
        await manager.aclose()
        await peer.aclose()


@pytest.mark.asyncio
async def test_best_effort_warmup_logs_once_after_releasing_the_owner_lock() -> None:
    manager = _new_manager(
        client=_FailingCreateClient(),
        warm_pool_size=1,
        fail_on_startup_warmup_error=False,
    )
    records: list[logging.LogRecord] = []

    class _LockCheckingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert not manager._warm_fill_lock.locked()
            records.append(record)

    logger = logging.getLogger("tinkerfin.sandbox.lifecycle")
    handler = _LockCheckingHandler()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        await manager.start()
        await manager.aclose()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    failures = [
        record
        for record in records
        if record.getMessage() == "Sandbox warm-pool creation failed"
    ]
    assert len(failures) == 1
    assert failures[0].__dict__["tinkerfin_error_type"] == (
        "UnexpectedOpenSandboxBackendError"
    )
    assert failures[0].exc_info is None


@pytest.mark.parametrize(
    ("value", "error_type"),
    (
        pytest.param(True, TypeError, id="boolean"),
        pytest.param("1", TypeError, id="string"),
        pytest.param(-0.01, ValueError, id="negative"),
        pytest.param(float("inf"), ValueError, id="infinity"),
        pytest.param(float("nan"), ValueError, id="nan"),
    ),
)
def test_manager_rejects_invalid_settlement_timeout(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        _new_manager(
            client=_FakeClient(),
            warm_pool_size=0,
            settlement_timeout=value,
        )


async def test_manager_close_timeout_retains_shared_cleanup_for_second_close() -> None:
    settlement_timeout = 0
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        settlement_timeout=settlement_timeout,
    )
    await manager.start()
    backend = cast(_FakeBackend, client.backends[0])
    backend.close_gate = threading.Event()
    timeout_type = getattr(
        tinkerfin_sandbox,
        "OpenSandboxSettlementTimeoutError",
        None,
    )
    assert timeout_type is not None

    try:
        with pytest.raises(timeout_type) as captured:
            await manager.aclose()

        assert captured.value.timeout == settlement_timeout
        close_entered = await asyncio.to_thread(backend.close_entered.wait, 1)
        assert close_entered
        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
        with pytest.raises(OpenSandboxManagerClosedError):
            await manager.get("owner-1")

        backend.close_gate.set()
        # Each close caller has its own wait budget. Completion of the released
        # worker thread and dependent resources is observed explicitly.
        await _eventually(lambda: client.close_calls == 1)
        await manager.aclose()

        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
        assert client.close_calls == 1
    finally:
        if backend.close_gate is not None:
            backend.close_gate.set()
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_context_caller_cancellation_retains_body_failure() -> None:
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
    )
    body_error = ValueError("body failed")

    async def use_manager() -> None:
        async with manager:
            backend = cast(_FakeBackend, client.backends[0])
            backend.close_gate = threading.Event()
            raise body_error

    operation = asyncio.create_task(use_manager())
    backend: _FakeBackend | None = None
    try:
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        backend = cast(_FakeBackend, client.backends[0])
        entered = await asyncio.to_thread(backend.close_entered.wait, 1)
        assert entered

        operation.cancel("context cleanup cancelled")
        with pytest.raises(asyncio.CancelledError) as captured:
            await operation

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert "OpenSandbox context body also failed: ValueError: body failed" in notes

        assert backend.close_gate is not None
        backend.close_gate.set()
        await manager.aclose()
        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
    finally:
        if backend is not None and backend.close_gate is not None:
            backend.close_gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_start_cleanup_caller_cancellation_retains_start_error() -> None:
    client = _BlockingCloseFailingCreateClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    opening = asyncio.create_task(manager.__aenter__())
    try:
        await asyncio.wait_for(client.close_entered.wait(), timeout=1)
        opening.cancel("startup cleanup cancelled")

        with pytest.raises(asyncio.CancelledError) as captured:
            await opening

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert "UnexpectedOpenSandboxBackendError" in notes

        client.release_close.set()
        await manager.aclose()
        assert client.close_calls == 1
    finally:
        client.release_close.set()
        await asyncio.gather(opening, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_owner_claim_release() -> None:
    client = _FakeClient()
    client.create_gate = asyncio.Event()
    state = _BlockingOwnerReleaseState()
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    await manager.start()
    operation = asyncio.create_task(manager.get("owner-1"))
    successor: OpenSandboxOwnerClaim | None = None
    try:
        await client.create_entered.wait()
        operation.cancel("owner operation cancelled")
        await state.release_started.wait()
        operation.cancel("owner operation cancelled again")
        state.release_gate.set()

        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not state.release_cancelled.is_set()
        successor = await asyncio.wait_for(
            state.acquire_owner(_resource_key("owner-1")),
            timeout=0.2,
        )
    finally:
        state.release_gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        if successor is not None:
            await InMemoryOpenSandboxState.release_owner(state, successor)
        if state.claim is not None:
            await InMemoryOpenSandboxState.release_owner(state, state.claim)
        await manager.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_warm_claim_release() -> None:
    client = _ObservedWarmCreateClient()
    client.create_gate = asyncio.Event()
    state = _BlockingWarmReleaseState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)
    startup = asyncio.create_task(manager.start())
    successor: OpenSandboxWarmClaim | None = None
    try:
        await client.create_entered.wait()
        create_task = client.current_create_task
        assert create_task is not None
        create_task.cancel("warm creation cancelled")
        await state.release_started.wait()
        create_task.cancel("warm creation cancelled again")
        state.release_gate.set()

        with pytest.raises(asyncio.CancelledError):
            await startup
        assert not state.release_cancelled.is_set()
        successor = await state.claim_warm_slot()
        assert successor is not None
    finally:
        state.release_gate.set()
        await asyncio.gather(startup, return_exceptions=True)
        if successor is not None:
            await InMemoryOpenSandboxState.release_warm(state, successor)
        if state.claim is not None:
            await InMemoryOpenSandboxState.release_warm(state, state.claim)
        close_result = (await asyncio.gather(manager.aclose(), return_exceptions=True))[
            0
        ]
        if isinstance(close_result, BaseException):
            await state.aclose()
            await client.aclose()


@pytest.mark.asyncio
async def test_close_after_cancelled_startup_is_silent_and_closes_resources(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _CancelledWarmupClient()
    state = _CloseObservedState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)

    with caplog.at_level(logging.DEBUG, logger="tinkerfin.sandbox"):
        with pytest.raises(asyncio.CancelledError):
            await manager.start()

        close_result = (await asyncio.gather(manager.aclose(), return_exceptions=True))[
            0
        ]
    state_close_calls = state.close_calls
    client_close_calls = client.close_calls
    if isinstance(close_result, BaseException):
        await state.aclose()
        await client.aclose()

    assert close_result is None
    assert state_close_calls == 1
    assert client_close_calls == 1
    assert caplog.records == []


@pytest.mark.asyncio
async def test_shared_state_prevents_cross_manager_duplicate_create() -> None:
    """Allow one creator when two process-level managers contend for an owner."""

    client = _ReconnectableFakeClient()
    store = _FakeState()
    first_manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    second_manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await first_manager.start()
    await second_manager.start()
    try:
        first, second = await asyncio.gather(
            first_manager.get(_key("user-1")),
            second_manager.get(_key("user-1")),
        )
    finally:
        await first_manager.aclose()
        await second_manager.aclose()

    assert first.id == second.id == "sandbox-1"
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_manager_tags_on_demand_create_with_hashed_owner() -> None:
    client = _ReconnectableFakeClient()
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("e2e-c1e84f194a-shared/agent-b"))
    finally:
        await manager.aclose()

    owner_label = client.create_metadata[0]["tinkerfin.ai/owner"]
    assert len(owner_label) <= 63
    assert owner_label[0].isalnum()
    assert owner_label[-1].isalnum()
    assert all(character.isalnum() or character in "-_." for character in owner_label)
    assert "e2e-c1e84f194a-shared" not in owner_label
    assert "agent-b" not in owner_label


async def test_sql_states_share_one_global_warm_pool(
    sql_engine: SqlEngineFactory, tmp_path: Path
) -> None:
    client = _ReconnectableFakeClient()
    url = f"sqlite+aiosqlite:///{tmp_path / 'manager-state.db'}"
    first_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="test"),
        warm_pool_size=1,
    )
    second_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="test"),
        warm_pool_size=1,
    )
    await asyncio.gather(first_manager.start(), second_manager.start())
    try:
        assert client.create_calls == 1

        handle = await second_manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)

        assert handle.id == "sandbox-1"
        assert client.create_calls == 2
    finally:
        await asyncio.gather(first_manager.aclose(), second_manager.aclose())


@pytest.mark.asyncio
async def test_manager_drains_durable_cleanup_queue_on_start(
    sql_engine: SqlEngineFactory, tmp_path: Path
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-start.db'}"
    seeded_state = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="test")
    await seeded_state.start(warm_pool_size=0)
    await seeded_state.enqueue_cleanup("orphan-sandbox")
    await seeded_state.aclose()

    client = _FakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="test"),
        warm_pool_size=0,
    )
    await manager.start()
    await manager.aclose()

    assert client.destroy_calls == ["orphan-sandbox"]
    checking_state = SQLAlchemyOpenSandboxState(
        engine=sql_engine(url), namespace="test"
    )
    await checking_state.start(warm_pool_size=0)
    try:
        assert await checking_state.claim_cleanup() is None
    finally:
        await checking_state.aclose()


@pytest.mark.asyncio
async def test_manager_persists_failed_replacement_cleanup(
    sql_engine: SqlEngineFactory, tmp_path: Path
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-replacement.db'}"
    client = _ReconnectableFakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="test"),
        warm_pool_size=0,
        recovery_policy=OpenSandboxRecoveryPolicy(
            max_attempts=1, on_failure="recreate"
        ),
    )
    await manager.start()
    await manager.get(_key("user-1"))
    old_backend = client.backends[0]
    old_backend.healthy = False
    client.destroy_errors[old_backend.id] = RuntimeError("destroy unavailable")

    replacement = await manager.get(_key("user-1"))
    await manager.aclose()

    assert replacement.id == "sandbox-2"
    checking_state = SQLAlchemyOpenSandboxState(
        engine=sql_engine(url), namespace="test"
    )
    await checking_state.start(warm_pool_size=0)
    try:
        cleanup = await checking_state.claim_cleanup()
        assert cleanup is not None
        assert cleanup.sandbox_id == "sandbox-1"
        await checking_state.release_cleanup(cleanup)
    finally:
        await checking_state.aclose()


@pytest.mark.asyncio
async def test_state_get_adopts_authoritative_binding_change() -> None:
    """Use the State binding instead of returning a healthy but stale local cache."""

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
        old_backend = client.backends[0]
        authoritative = _FakeBackend("sandbox-external")
        client.connected[authoritative.id] = authoritative
        store.bindings[_resource_key("user-1")] = authoritative.id

        refreshed = await manager.get(_key("user-1"))

        assert refreshed is handle
        assert refreshed.id == authoritative.id
        assert old_backend.close_calls == 1
        assert client.destroy_calls == []
    finally:
        await manager.aclose()


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


@pytest.mark.asyncio
async def test_cancelled_recreate_tracks_distinct_authoritative_old_id() -> None:
    client = _ReconnectableFakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    execute_task: asyncio.Task[ExecuteResponse] | None = None
    recreate_task: asyncio.Task[OpenSandboxHandle | RootedOpenSandboxBackend] | None = (
        None
    )
    try:
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"
        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        assert await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        store.bindings[_resource_key("user-1")] = "sandbox-external"

        recreate_task = asyncio.create_task(manager.recreate(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")
        recreate_task.cancel("caller stopped waiting for replacement cleanup")
        with pytest.raises(asyncio.CancelledError):
            await recreate_task

        old_backend.execute_gate.set()
        assert execute_task is not None
        await execute_task
    finally:
        for backend in client.backends:
            backend.execute_gate.set()
        if execute_task is not None:
            await asyncio.gather(execute_task, return_exceptions=True)
        if recreate_task is not None:
            await asyncio.gather(recreate_task, return_exceptions=True)
        await manager.aclose()

    assert store.bindings == {_resource_key("user-1"): "sandbox-2"}
    assert client.destroy_calls == ["sandbox-external"]
    assert "sandbox-1" not in client.destroy_calls
    assert old_backend.close_calls == 1


async def test_manager_uses_state_as_its_allocation_boundary() -> None:
    client = _FakeClient()
    state = InMemoryOpenSandboxState()
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=0,
    )
    try:
        await manager.start()
        handle = await manager.get(_key("user-1"))

        binding = await state.read_binding(_resource_key("user-1"))
        assert binding is not None
        assert binding.sandbox_id == handle.id
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


async def test_cancelled_committed_bind_closes_locally_and_reconnects() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.cancel_after_save_commit = True
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    await manager.start()
    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="bind response was cancelled after commit",
        ):
            await manager.get(_key("user-1"))

        created = client.backends[0]
        state.cancel_after_save_commit = False
        reconnected = _FakeBackend("sandbox-1")
        client.connected["sandbox-1"] = reconnected
        handle = await manager.get(_key("user-1"))

        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.connect_calls == ["sandbox-1"]
        assert client.destroy_calls == []
        assert created.close_calls == 1
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


async def test_replaced_binding_allows_only_the_candidate_to_be_destroyed() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.save_error = RuntimeError("owner claim expired")
    state.read_override_enabled = True
    state.read_override = OpenSandboxBinding(
        sandbox_id="sandbox-authoritative",
        generation=99,
    )
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        with pytest.raises(OpenSandboxStateError, match="fake state write failed"):
            await manager.get(_key("user-1"))

        assert len(state.get_calls) == 2
        assert client.destroy_calls == ["sandbox-1"]
        assert "sandbox-authoritative" not in client.destroy_calls
        assert client.backends[0].close_calls == 1
    finally:
        await manager.aclose()


async def test_failed_replacement_preserves_the_committed_warm_sandbox() -> None:
    def backend_factory(sandbox_id: str) -> _FakeBackend:
        if sandbox_id == "sandbox-2":
            raise RuntimeError("replacement creation failed")
        return _FakeBackend(sandbox_id, healthy=False)

    client = _FakeClient(backend_factory)
    state = _FakeState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)
    try:
        await manager.start()
        warm_backend = client.backends[0]

        with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
            await manager.get(_key("user-1"))
        assert isinstance(captured.value.cause, RuntimeError)
        assert str(captured.value.cause) == "replacement creation failed"

        assert state.bindings == {_resource_key("user-1"): "sandbox-1"}
        assert client.destroy_calls == []
        assert warm_backend.close_calls == 1
    finally:
        await manager.aclose()


async def test_manager_wraps_an_undeclared_custom_state_failure() -> None:
    manager = _new_manager(client=_FakeClient(), state=_LeakingState())
    await manager.start()
    try:
        with pytest.raises(UnexpectedOpenSandboxStateError) as captured:
            await manager.get(_key("user-1"))
    finally:
        await manager.aclose()

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "driver-specific state failure"
    assert dict(captured.value.context) == {}
    assert captured.value.diagnostic_context["operation"] == "acquire_owner"


class _RegistrationInterruptedState(InMemoryOpenSandboxState):
    """Fail or suspend the public holder registration after a connection changes."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def persistent(self) -> bool:
        return True

    async def register_holder(self, claim: OpenSandboxOwnerClaim, holder_id: str):
        if self.failure is not None:
            self.entered.set()
            if self.failure == "cancel":
                await self.release.wait()
            raise OpenSandboxStateError("Holder registration is unavailable")
        return await super().register_holder(claim, holder_id)


@pytest.mark.parametrize("operation", ["reconnect", "recreate"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_registration_failure_retains_old_resource_cleanup(
    operation: str, failure: str
) -> None:
    client = _FakeClient()
    state = _RegistrationInterruptedState()
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    await manager.start()
    task: asyncio.Task[object] | None = None
    try:
        handle = await manager.get("owner")
        original = client.backends[0]
        replacement = _FakeBackend(original.id)
        client.connected[original.id] = replacement
        state.failure = failure
        action = manager.reconnect if operation == "reconnect" else manager.recreate
        task = asyncio.create_task(action("owner"))
        await asyncio.wait_for(state.entered.wait(), 1)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(OpenSandboxStateError):
                await task
        await _eventually(lambda: original.close_calls == 1)
        binding = await state.read_binding(_resource_key("owner"))
        assert binding is not None
        assert binding.sandbox_id == handle.id
        if operation == "recreate":
            assert original.id in client.destroy_calls
            assert binding.sandbox_id != original.id
        else:
            assert client.destroy_calls == []
            assert binding.sandbox_id == original.id
        assert original.close_calls == 1
    finally:
        state.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()
