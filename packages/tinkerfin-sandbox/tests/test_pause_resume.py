"""Exercise shared-State lifecycle coordination through public manager operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, TypeVar

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.transport import RetryPolicy
from sqlalchemy.ext.asyncio import AsyncEngine
from test_manager import _resource_key
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxAvailability,
    OpenSandboxAvailabilityPhase,
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxConfig,
    OpenSandboxDiagnosticContent,
    OpenSandboxHandle,
    OpenSandboxHolderUpdate,
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleEventType,
    OpenSandboxLifecycleUncertainError,
    OpenSandboxManager,
    OpenSandboxOwnerClaim,
    OpenSandboxPausedError,
    OpenSandboxRecoveryPolicy,
    OpenSandboxRuntimeInfo,
    OpenSandboxStateError,
    OpenSandboxStatusInfo,
    RootedOpenSandboxBackend,
    SQLAlchemyOpenSandboxState,
)

_ResultT = TypeVar("_ResultT")
_Backend = OpenSandboxHandle | RootedOpenSandboxBackend


class _HeldCommand(httpx.AsyncByteStream):
    """Keep remote work alive independently of a cancelled command consumer."""

    def __init__(self, remote: _Remote, *, unknown_id: bool) -> None:
        self.remote = remote
        self.unknown_id = unknown_id

    async def __aiter__(self) -> AsyncGenerator[bytes]:
        if not self.unknown_id:
            yield b'data: {"type":"init","text":"held-command","timestamp":1}\n\n'
        self.remote.command_started.set()
        await self.remote.command_release.wait()
        self.remote.active_commands -= 1
        yield b'data: {"type":"execution_complete","timestamp":2}\n\n'


class _Remote:
    """Control SDK-visible endpoints and lifecycle effects for shared clients."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.connection_generations: dict[str, int] = {}
        self.endpoints: dict[str, tuple[str, int]] = {}
        self.created = 0
        self.pause_calls: list[tuple[str, int, int]] = []
        self.resume_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.executions: list[tuple[str, int, str]] = []
        self.uploads: list[bytes] = []
        self.active_commands = 0
        self.active_uploads = 0
        self.command_started = asyncio.Event()
        self.command_release = asyncio.Event()
        self.upload_started = asyncio.Event()
        self.upload_release = asyncio.Event()
        self.pause_started = asyncio.Event()
        self.pause_release = asyncio.Event()
        self.pause_release.set()

    def info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        state = self.states.get(sandbox_id)
        if state is None:
            return OpenSandboxRuntimeInfo.unavailable(sandbox_id, "not_found")
        return OpenSandboxRuntimeInfo(
            sandbox_id=sandbox_id,
            available=True,
            healthy=state == "Running",
            status=OpenSandboxStatusInfo(state=state),
        )

    def require_running(self, sandbox_id: str) -> None:
        if self.states.get(sandbox_id) != "Running":
            raise OpenSandboxBackendUnavailableError(
                "Sandbox is not running", context={"reason": "unreachable"}
            )

    async def pause(self, sandbox_id: str) -> None:
        self.require_running(sandbox_id)
        self.pause_calls.append((sandbox_id, self.active_commands, self.active_uploads))
        self.pause_started.set()
        await self.pause_release.wait()
        self.states[sandbox_id] = "Paused"

    async def resume(self, sandbox_id: str) -> None:
        if self.states.get(sandbox_id) != "Paused":
            raise OpenSandboxBackendUnavailableError("Sandbox is not paused")
        self.resume_calls.append(sandbox_id)
        self.states[sandbox_id] = "Running"
        self.connection_generations[sandbox_id] += 1

    def destroy(self, sandbox_id: str) -> None:
        self.destroy_calls.append(sandbox_id)
        self.states.pop(sandbox_id, None)

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/sandboxes/"):
            sandbox_id = path.split("/")[3]
            if request.method == "DELETE":
                self.destroy(sandbox_id)
                return httpx.Response(204)
            if sandbox_id not in self.states:
                return httpx.Response(
                    404, json={"code": "NOT_FOUND", "message": "gone"}
                )
            if "/endpoints/" in path:
                generation = self.connection_generations[sandbox_id]
                host = f"{sandbox_id}-endpoint-{generation}.invalid"
                self.endpoints[host] = (sandbox_id, generation)
                return httpx.Response(200, json={"endpoint": host})
            return httpx.Response(
                200,
                json={
                    "id": sandbox_id,
                    "status": {"state": self.states[sandbox_id]},
                    "createdAt": "2026-09-07T00:00:00Z",
                    "entrypoint": ["/entrypoint.sh"],
                },
            )
        sandbox_id, generation = self.endpoints[request.url.host]
        if (
            self.states.get(sandbox_id) != "Running"
            or generation != self.connection_generations[sandbox_id]
        ):
            return httpx.Response(
                503, json={"code": "UNAVAILABLE", "message": "endpoint unavailable"}
            )
        if request.method == "POST" and path == "/command":
            payload = json.loads(await request.aread())
            command = payload["command"]
            assert isinstance(command, str)
            self.executions.append((sandbox_id, generation, command))
            if command in {"hold", "unknown"}:
                self.active_commands += 1
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=_HeldCommand(self, unknown_id=command == "unknown"),
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(
                    b'data: {"type":"stdout","text":"ok","timestamp":1}\n\n'
                    b'data: {"type":"execution_complete","timestamp":2}\n\n'
                ),
            )
        if path == "/files/upload":
            self.uploads.append(await request.aread())
            self.active_uploads += 1
            self.upload_started.set()
            try:
                await self.upload_release.wait()
            finally:
                self.active_uploads -= 1
            return httpx.Response(200)
        if path == "/command/status/held-command":
            return httpx.Response(
                200,
                json={
                    "id": "held-command",
                    "running": self.active_commands > 0,
                    "exit_code": None if self.active_commands > 0 else 0,
                },
            )
        if request.method == "DELETE" and path == "/command":
            self.command_release.set()
            self.active_commands = 0
            return httpx.Response(204)
        raise AssertionError(f"Unexpected SDK request: {request.method} {path}")


class _Client:
    """Implement the current replaceable client contract with real SDK backends."""

    def __init__(self, remote: _Remote, *, workspace_root: str | None) -> None:
        self.remote = remote
        self.config = OpenSandboxConfig(
            warm_pool_size=0, ttl=None, workspace_root=workspace_root
        )
        self.transport = httpx.MockTransport(remote.respond)
        self.connections: list[tuple[str, int]] = []
        self.diagnostics: list[tuple[str, str, str]] = []

    async def create(
        self, *, metadata: Mapping[str, str] | None = None
    ) -> OpenSandboxBackend:
        del metadata
        self.remote.created += 1
        sandbox_id = f"sandbox-{self.remote.created}"
        self.remote.states[sandbox_id] = "Running"
        self.remote.connection_generations[sandbox_id] = 0
        return await self.connect(sandbox_id)

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.remote.require_running(sandbox_id)
        sandbox = await Sandbox.connect(
            sandbox_id,
            connection_config=ConnectionConfig(
                domain="control.invalid",
                transport=self.transport,
                retry_policy=RetryPolicy.disabled(),
                disable_metrics=True,
                endpoint_cache_disabled=True,
            ),
            skip_health_check=True,
        )
        self.connections.append(
            (sandbox_id, self.remote.connection_generations[sandbox_id])
        )
        return OpenSandboxBackend(sandbox=sandbox)

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        return self.remote.info(sandbox_id)

    async def get_runtime_info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        return self.remote.info(sandbox_id)

    async def pause(self, sandbox_id: str) -> None:
        await self.remote.pause(sandbox_id)

    async def resume(self, sandbox_id: str) -> None:
        await self.remote.resume(sandbox_id)

    async def get_diagnostic_logs(
        self, sandbox_id: str, *, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent:
        self.diagnostics.append((sandbox_id, "logs", scope))
        return OpenSandboxDiagnosticContent(
            sandbox_id=sandbox_id,
            kind="logs",
            scope=scope,
            delivery="inline",
            content_type="text/plain",
            truncated=False,
            content="retained logs",
        )

    async def get_diagnostic_events(
        self, sandbox_id: str, *, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent:
        self.diagnostics.append((sandbox_id, "events", scope))
        return OpenSandboxDiagnosticContent(
            sandbox_id=sandbox_id,
            kind="events",
            scope=scope,
            delivery="inline",
            content_type="text/plain",
            truncated=False,
            content="current state summary",
            warnings=("Summary only",),
        )

    async def destroy(self, sandbox_id: str) -> None:
        self.remote.destroy(sandbox_id)

    async def aclose(self) -> None:
        await self.transport.aclose()


class _ControlledState(SQLAlchemyOpenSandboxState):
    """Delay public coordination responses while retaining real SQL transitions."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine=engine, namespace="pause-resume", poll_interval=0.01)
        self.outage = False
        self.delay_ack = False
        self.ack_started = asyncio.Event()
        self.ack_release = asyncio.Event()
        self.stale_ack_rejected = asyncio.Event()
        self.delay_phase: str | None = None
        self.phase_committed = asyncio.Event()
        self.phase_release = asyncio.Event()
        self.registration_failure: Literal["before", "after"] | None = None
        self.registration_failed = asyncio.Event()
        self.delay_registration = False
        self.registration_committed = asyncio.Event()
        self.registration_release = asyncio.Event()

    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        failure = self.registration_failure
        self.registration_failure = None
        if failure == "before":
            self.registration_failed.set()
            raise OpenSandboxStateError(
                "Holder registration is temporarily unavailable"
            )
        result = await super().register_holder(claim, holder_id)
        if failure == "after":
            self.registration_failed.set()
            raise OpenSandboxStateError("Holder registration response is unavailable")
        if self.delay_registration:
            self.registration_committed.set()
            await self.registration_release.wait()
        return result

    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        result = await super().change_availability(
            claim, expected, phase=phase, refresh_connection=refresh_connection
        )
        if phase == self.delay_phase:
            self.phase_committed.set()
            await self.phase_release.wait()
        return result

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        if self.outage:
            raise OpenSandboxStateError("State is temporarily unavailable")
        return await super().acquire_owner(owner_key)

    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        if self.outage:
            raise OpenSandboxStateError("State is temporarily unavailable")
        return await super().get_holder_updates(holder_id)

    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        if self.delay_ack:
            self.ack_started.set()
            await self.ack_release.wait()
        accepted = await super().acknowledge_idle(holder_id, availability)
        if not accepted:
            self.stale_ack_rejected.set()
        return accepted


class _Observer:
    def __init__(self) -> None:
        self.events: list[OpenSandboxLifecycleEvent] = []

    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        self.events.append(event)


class _World:
    """Own managers, independent SQL State instances, and test operation tasks."""

    def __init__(self, tmp_path: Path, workspace_root: str | None) -> None:
        self.url = f"sqlite+aiosqlite:///{tmp_path / 'pause-state.db'}"
        self.engines = SqlEngineFactory()
        self.workspace_root = workspace_root
        self.remote = _Remote()
        self.managers: list[OpenSandboxManager[str]] = []
        self.states: list[_ControlledState] = []
        self.clients: list[_Client] = []
        self.tasks: list[asyncio.Task[object]] = []

    async def add(self, observer: _Observer | None = None) -> OpenSandboxManager[str]:
        client = _Client(self.remote, workspace_root=self.workspace_root)
        state = _ControlledState(self.engines(self.url))
        manager = OpenSandboxManager[str](
            client=client,
            key_resolver=str,
            state=state,
            warm_pool_size=0,
            recovery_policy=OpenSandboxRecoveryPolicy(max_attempts=1, timeout=1),
            observers=() if observer is None else (observer,),
        )
        self.managers.append(manager)
        self.states.append(state)
        self.clients.append(client)
        await manager.start()
        return manager

    def spawn(self, operation: Awaitable[_ResultT]) -> asyncio.Task[_ResultT]:
        async def run() -> _ResultT:
            return await operation

        task = asyncio.create_task(run())
        self.tasks.append(task)
        return task

    async def close(self) -> None:
        self.remote.command_release.set()
        self.remote.upload_release.set()
        self.remote.pause_release.set()
        for state in self.states:
            state.outage = False
            state.ack_release.set()
            state.phase_release.set()
            state.registration_release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(
                asyncio.gather(*(manager.aclose() for manager in self.managers)),
                timeout=10,
            )
        finally:
            await self.engines.aclose()


@asynccontextmanager
async def _world(
    tmp_path: Path, *, workspace_root: str | None = None
) -> AsyncGenerator[_World]:
    world = _World(tmp_path, workspace_root)
    try:
        yield world
    finally:
        await world.close()


async def _phase(state: _ControlledState, phase: str) -> OpenSandboxAvailability:
    async with asyncio.timeout(3):
        while True:
            snapshot = await state.read_availability(_resource_key("owner"))
            if snapshot is not None and snapshot.phase == phase:
                return snapshot
            await asyncio.sleep(0.01)


async def _usable(handle: _Backend) -> None:
    async with asyncio.timeout(3):
        while True:
            try:
                response = await handle.aexecute("probe")
            except (
                OpenSandboxBusyError,
                OpenSandboxPausedError,
                OpenSandboxLifecycleUncertainError,
            ):
                await asyncio.sleep(0.02)
            else:
                assert response.exit_code == 0
                return


@pytest.mark.parametrize("failure", ["before", "after"])
async def test_registration_failure_recovers_through_get(
    tmp_path: Path, failure: Literal["before", "after"]
) -> None:
    async with _world(tmp_path) as world:
        manager = await world.add()
        world.states[0].registration_failure = failure
        with pytest.raises(OpenSandboxStateError):
            await manager.get("owner")
        backend = await manager.get("owner")
        assert (await backend.aexecute("probe")).exit_code == 0
        assert world.remote.created == 1
        await manager.pause("owner", timeout=2)
        assert world.remote.states[backend.id] == "Paused"


async def test_committed_registration_response_failure_is_released_on_close(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first = await world.add()
        world.states[0].registration_failure = "after"
        with pytest.raises(OpenSandboxStateError):
            await first.get("owner")
        await first.aclose()
        second = await world.add()
        backend = await second.get("owner")
        await second.pause("owner", timeout=2)
        assert world.remote.created == 1
        assert world.remote.states[backend.id] == "Paused"


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_registration_settles_ownership_before_manager_close(
    tmp_path: Path, cancellations: int
) -> None:
    async with _world(tmp_path) as world:
        first = await world.add()
        state = world.states[0]
        state.delay_registration = True
        operation = world.spawn(first.get("owner"))
        await asyncio.wait_for(state.registration_committed.wait(), timeout=2)
        for _ in range(cancellations):
            operation.cancel("cancelled registration caller")
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=2)
        state.registration_release.set()
        await first.aclose()
        second = await world.add()
        backend = await second.get("owner")
        await second.pause("owner", timeout=2)
        assert world.remote.created == 1
        assert world.remote.states[backend.id] == "Paused"


async def test_peer_resume_registration_failure_recovers_through_get(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first = await world.add()
        second = await world.add()
        await first.get("owner")
        backend = await second.get("owner")
        await first.pause("owner", timeout=2)
        world.states[1].registration_failure = "before"
        await first.resume("owner", timeout=2)
        await asyncio.wait_for(world.states[1].registration_failed.wait(), timeout=2)
        assert await second.get("owner") is backend
        assert (await backend.aexecute("probe")).exit_code == 0
        assert world.remote.created == 1


@pytest.mark.asyncio
async def test_pause_blocks_workspace_reset_without_executing_commands(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, workspace_root="/workspace") as world:
        manager = await world.add()
        await manager.get("owner")
        await manager.pause("owner", timeout=2)
        count = len(world.remote.executions)
        with pytest.raises(OpenSandboxPausedError):
            await manager.reset("owner")
        assert len(world.remote.executions) == count


@pytest.mark.asyncio
async def test_cancelled_undispatched_pause_restores_both_handles(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first, second = await world.add(), await world.add()
        first_handle, second_handle = (
            await first.get("owner"),
            await second.get("owner"),
        )
        working = world.spawn(second_handle.aexecute("hold"))
        await asyncio.wait_for(world.remote.command_started.wait(), timeout=1)
        pausing = world.spawn(first.pause("owner", timeout=3))
        await _phase(world.states[0], "draining")
        pausing.cancel("pause cancelled")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pausing, timeout=1)
        await _phase(world.states[0], "running")
        assert world.remote.pause_calls == []
        world.remote.command_release.set()
        await working
        await _usable(first_handle)
        await _usable(second_handle)


@pytest.mark.asyncio
async def test_resume_refreshes_both_stable_handles_for_the_original_instance(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first, second = await world.add(), await world.add()
        first_handle, second_handle = (
            await first.get("owner"),
            await second.get("owner"),
        )
        binding = await world.states[0].read_binding(_resource_key("owner"))
        await first.pause("owner", timeout=2)
        resumed = await first.resume("owner", timeout=2)
        assert resumed is first_handle
        await _usable(second_handle)
        assert await second.get("owner") is second_handle
        assert first_handle.id == second_handle.id == "sandbox-1"
        assert await world.states[1].read_binding(_resource_key("owner")) == binding
        assert world.clients[0].connections[-1] == ("sandbox-1", 1)
        assert world.clients[1].connections[-1] == ("sandbox-1", 1)
        assert world.remote.resume_calls == ["sandbox-1"]
        assert world.remote.created == 1


@pytest.mark.asyncio
async def test_paused_binding_survives_all_managers_closing_and_reopening(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first, second = await world.add(), await world.add()
        original = await first.get("owner")
        await second.get("owner")
        await first.pause("owner", timeout=2)
        await first.aclose()
        await second.aclose()
        reopened = await world.add()
        with pytest.raises(OpenSandboxPausedError):
            await reopened.get("owner")
        assert world.remote.created == 1
        assert world.remote.states[original.id] == "Paused"
        resumed = await reopened.resume("owner", timeout=2)
        assert resumed.id == original.id
        await _usable(resumed)
        assert world.remote.created == 1


@pytest.mark.asyncio
async def test_repeated_pause_and_resume_do_not_repeat_remote_effects(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        first, second = await world.add(), await world.add()
        await first.get("owner")
        await second.get("owner")
        await first.pause("owner", timeout=2)
        await second.pause("owner", timeout=2)
        await first.resume("owner", timeout=2)
        await second.resume("owner", timeout=2)
        assert len(world.remote.pause_calls) == 1
        assert world.remote.resume_calls == ["sandbox-1"]
        assert world.remote.created == 1


@pytest.mark.asyncio
async def test_missing_and_stopped_instances_never_create_during_pause_or_resume(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        manager = await world.add()
        for operation in (manager.pause, manager.resume):
            with pytest.raises(OpenSandboxBackendUnavailableError):
                await operation("missing", timeout=1)
        assert world.remote.created == 0
        handle = await manager.get("owner")
        original = await world.states[0].read_binding(_resource_key("owner"))
        world.remote.states[handle.id] = "Terminated"
        for operation in (manager.pause, manager.resume):
            with pytest.raises(OpenSandboxBackendError):
                await operation("owner", timeout=1)
        assert world.remote.created == 1
        assert await world.states[0].read_binding(_resource_key("owner")) == original


@pytest.mark.asyncio
async def test_paused_diagnostics_and_details_remain_readable_without_false_outage(
    tmp_path: Path,
) -> None:
    observer = _Observer()
    async with _world(tmp_path) as world:
        manager = await world.add(observer)
        await manager.get("owner")
        await manager.pause("owner", timeout=2)
        commands = len(world.remote.executions)
        connections = len(world.clients[0].connections)
        logs = await manager.get_diagnostic_logs("owner", scope="all")
        events = await manager.get_diagnostic_events("owner", scope="runtime")
        details = await manager.get_details("owner")
        assert details is not None and details.status is not None
        assert details.status.state == "Paused"
        assert not details.healthy
        assert not await manager.is_healthy("owner")
        assert logs.content == "retained logs" and logs.scope == "all"
        assert events.warnings == ("Summary only",)
        assert len(world.remote.executions) == commands
        assert len(world.clients[0].connections) == connections
        await manager.resume("owner", timeout=2)
        await manager.aclose()
        kinds = [event.type for event in observer.events]
        assert OpenSandboxLifecycleEventType.PAUSED in kinds
        assert OpenSandboxLifecycleEventType.RESUMED in kinds
        assert OpenSandboxLifecycleEventType.UNAVAILABLE not in kinds
        assert OpenSandboxLifecycleEventType.RECOVERY_FAILED not in kinds


async def test_expired_paused_sandbox_details_remain_an_unavailable_snapshot(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path) as world:
        manager = await world.add()
        handle = await manager.get("owner")
        await manager.pause("owner", timeout=2)
        world.remote.states.pop(handle.id)
        details = await manager.get_details("owner")
        assert details is not None
        assert details.available is False
        assert details.unavailable_reason == "not_found"
        assert details.access_state == "paused"
        assert world.remote.created == 1
        assert world.remote.resume_calls == []


@pytest.mark.parametrize(
    ("action", "delayed_phase", "expected_phase", "pause_count", "resume_count"),
    [
        ("pause", "draining", "running", 0, 0),
        ("pause", "pausing", "running", 0, 0),
        ("pause", "paused", "paused", 1, 0),
        ("resume", "resuming", "paused", 1, 0),
        ("resume", "running", "running", 1, 1),
    ],
)
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancel_after_state_commit_preserves_exact_request_ownership(
    tmp_path: Path,
    action: str,
    delayed_phase: str,
    expected_phase: str,
    pause_count: int,
    resume_count: int,
    cancellations: int,
) -> None:
    async with _world(tmp_path) as world:
        first, second = await world.add(), await world.add()
        first_handle, second_handle = (
            await first.get("owner"),
            await second.get("owner"),
        )
        if action == "resume":
            await first.pause("owner", timeout=2)
        delayed = world.states[0]
        delayed.delay_phase = delayed_phase
        operation = world.spawn(
            first.pause("owner", timeout=2)
            if action == "pause"
            else first.resume("owner", timeout=2)
        )
        await asyncio.wait_for(delayed.phase_committed.wait(), timeout=2)
        for _ in range(cancellations):
            operation.cancel("cancel after durable intent commit")
            await asyncio.sleep(0)
        delayed.phase_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(operation, timeout=2)
        assert type(cancelled.value) is asyncio.CancelledError
        await _phase(delayed, expected_phase)
        assert len(world.remote.pause_calls) == pause_count
        assert len(world.remote.resume_calls) == resume_count
        assert first_handle.id == second_handle.id == "sandbox-1"
        if expected_phase == "running":
            await _usable(first_handle)
            await _usable(second_handle)
        else:
            with pytest.raises(OpenSandboxPausedError):
                await second.get("owner")


@pytest.mark.parametrize("action", ["recreate", "destroy"])
async def test_cancel_pending_confirmation_does_not_expose_internal_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async with _world(tmp_path) as world:
        manager = await world.add()
        handle = await manager.get("owner")
        client = world.clients[0]
        pause = client.pause

        async def lost_response(sandbox_id: str) -> None:
            await pause(sandbox_id)
            raise OpenSandboxBackendUnavailableError("Pause response was lost")

        monkeypatch.setattr(client, "pause", lost_response)
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await manager.pause("owner", timeout=2)
        await _phase(world.states[0], "pausing")
        state = world.states[0]
        state.delay_phase = "paused"
        operation = world.spawn(
            manager.recreate("owner")
            if action == "recreate"
            else manager.destroy("owner")
        )
        await asyncio.wait_for(state.phase_committed.wait(), timeout=1)
        operation.cancel("cancel confirmation before resource replacement")
        await asyncio.sleep(0)
        state.phase_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(operation, timeout=1)
        assert type(cancelled.value) is asyncio.CancelledError
        assert world.remote.created == 1
        assert world.remote.states[handle.id] == "Paused"
        await _phase(state, "paused")
