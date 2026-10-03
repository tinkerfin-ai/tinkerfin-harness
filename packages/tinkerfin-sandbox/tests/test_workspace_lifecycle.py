"""Project admission, fixed parent borrowing, and cancellation settlement contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_manager import _resource_key
from test_pause_resume import _Remote, _World, _world
from test_workspace_backend import _Stream

from tinkerfin_sandbox import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxInitializationError,
    OpenSandboxLifecycleUncertainError,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxPurposeError,
    RootedOpenSandboxBackend,
    UnexpectedOpenSandboxBackendError,
)
from tinkerfin_sandbox.backends._isolated import _WorkspaceConnection
from tinkerfin_sandbox.lifecycle import _workspace_access
from tinkerfin_sandbox.lifecycle._workspace_access import _ProjectCoordinator

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class _Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


@dataclass
class _Record:
    incarnation: str = field(default_factory=lambda: str(uuid4()))
    phase: Literal["active", "deleting", "deleted"] = "active"
    sessions: list[tuple[str, str]] = field(default_factory=list)
    deletion_id: str | None = None

    def payload(self) -> dict[str, JsonValue]:
        return {
            "incarnation": self.incarnation,
            "phase": self.phase,
            "sessions": [
                {"session_id": session_id, "session_namespace": namespace}
                for session_id, namespace in self.sessions
            ],
            "deletion_id": self.deletion_id,
        }


class _WorkspaceRemote(_Remote):
    """Model external registry and native contracts behind real SDK HTTP adapters."""

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[tuple[str, str], _Record] = {}
        self.cancelled: set[tuple[str, str, str]] = set()
        self.stopped: set[tuple[str, str, str]] = set()
        self.live: set[tuple[str, str, str]] = set()
        self.namespaces: dict[str, str] = {}
        self.registry_requests: list[tuple[str, str, dict[str, JsonValue]]] = []
        self.native_requests: list[httpx.Request] = []
        self.streams: list[_Stream] = []
        self.gates: dict[str, _Gate] = {}
        self.late_registry: list[tuple[str, dict[str, JsonValue]]] = []
        self.late_posts: list[tuple[str, dict[str, JsonValue]]] = []
        self.lose_response: str | None = None
        self.stop_status = 200
        self.registry_output: dict[str, str] = {}
        self.initialization_output: str | None = None
        self.initialization_exit = 0
        self.initialization_timeout = False
        self.session_request_mismatch = False
        self.environments: list[dict[str, JsonValue]] = []
        self.network_live: set[tuple[str, str, str]] = set()
        self.network_revoked: set[tuple[str, str, str]] = set()
        self.restart_stopped: set[tuple[str, str, str]] = set()
        self.network_status = 0
        self.network_output: dict[str, str] = {}
        self.late_network: list[tuple[str, str, str, str]] = []

    def restart_parent(self, sandbox_id: str) -> None:
        """End all parent processes and retain receipts for its recorded owners."""
        self.restart_stopped = {
            identity for identity in self.restart_stopped if identity[0] != sandbox_id
        }
        self.restart_stopped.update(
            (sandbox_id, namespace, session_id)
            for (record_parent, _project), record in self.records.items()
            if record_parent == sandbox_id
            for session_id, namespace in record.sessions
        )
        self.cancelled.update(self.restart_stopped)
        self.live = {identity for identity in self.live if identity[0] != sandbox_id}
        self.network_live = {
            identity for identity in self.network_live if identity[0] != sandbox_id
        }
        self.network_revoked = {
            identity for identity in self.network_revoked if identity[0] != sandbox_id
        }
        self.namespaces[sandbox_id] = str(uuid4())

    @staticmethod
    def rejection(reason: str) -> tuple[int, JsonValue]:
        return 1, {"error": {"reason": reason, "message": "Safe registry rejection"}}

    def registry(
        self, sandbox_id: str, payload: dict[str, JsonValue]
    ) -> tuple[int, JsonValue]:
        project, operation, arguments = (
            payload["project"],
            payload["operation"],
            payload["arguments"],
        )
        assert isinstance(project, str) and isinstance(operation, str)
        assert isinstance(arguments, dict)
        self.registry_requests.append((sandbox_id, operation, payload))
        key = sandbox_id, project
        record = self.records.get(key)
        if operation == "prepare":
            if record is not None and record.phase == "deleting":
                return self.rejection("busy")
            if record is None or record.phase == "deleted":
                record = self.records[key] = _Record()
            return 0, record.payload()
        if operation == "begin_delete":
            if record is None:
                return 0, None
            if record.phase == "active":
                record.phase, record.deletion_id = "deleting", str(uuid4())
            return 0, record.payload()
        if operation == "finish_delete":
            assert record is not None and record.phase == "deleting"
            assert arguments["incarnation"] == record.incarnation
            assert arguments["deletion_id"] == record.deletion_id
            expected = record.payload()["sessions"]
            assert arguments["confirmed"] == expected
            assert all(
                (sandbox_id, namespace, session_id) not in self.live
                for session_id, namespace in record.sessions
            )
            assert all(
                (sandbox_id, namespace, session_id) in self.network_revoked
                for session_id, namespace in record.sessions
            )
            record.phase = "deleted"
            record.sessions.clear()
            return 0, None
        session_id, namespace = arguments["session_id"], arguments["session_namespace"]
        assert isinstance(session_id, str) and isinstance(namespace, str)
        owner = session_id, namespace
        identity = sandbox_id, namespace, session_id
        if operation == "cancel":
            self.cancelled.add(identity)
            return 0, (
                record.payload()
                if record is not None and owner in record.sessions
                else None
            )
        assert record is not None
        if operation == "release":
            self.cancelled.add(identity)
            assert identity in self.stopped or identity in self.restart_stopped
            assert identity in self.network_revoked
            if (
                record.incarnation == arguments["incarnation"]
                and record.phase == "active"
            ):
                record.sessions = [entry for entry in record.sessions if entry != owner]
            return 0, None
        if (
            record.incarnation != arguments["incarnation"]
            or record.phase != "active"
            or identity in self.cancelled
            or namespace != self.namespaces[sandbox_id]
        ):
            return self.rejection("stale")
        if operation == "reserve":
            if owner not in record.sessions:
                record.sessions.append(owner)
            return 0, record.payload()
        assert operation == "session_request" and owner in record.sessions
        return 0, {
            "session_id": str(uuid4()) if self.session_request_mismatch else session_id,
            "session_namespace": namespace,
            "workspace": {
                "path": f"/trusted/{project}/{record.incarnation}",
                "mode": "rw",
            },
            "profile": "strict",
            "share_net": False,
            "uid_mode": "setpriv",
            "uid": 1000,
            "gid": 1000,
            "env_passthrough": {"mode": "allow", "keys": []},
        }

    def command_response(self, output: str, *, exit_code: int = 0) -> httpx.Response:
        terminal: dict[str, JsonValue] = (
            {"type": "execution_complete", "timestamp": 1}
            if exit_code == 0
            else {
                "type": "error",
                "timestamp": 1,
                "error": {"ename": "ExitError", "evalue": str(exit_code)},
            }
        )
        content = b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n"
            for event in (
                {"type": "stdout", "timestamp": 1, "text": output},
                terminal,
            )
        )
        stream = _Stream(content)
        self.streams.append(stream)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=stream
        )

    def start(self, sandbox_id: str, payload: dict[str, JsonValue]) -> httpx.Response:
        session_id, namespace = payload["session_id"], payload["session_namespace"]
        assert isinstance(session_id, str) and isinstance(namespace, str)
        identity = sandbox_id, namespace, session_id
        if namespace != self.namespaces[sandbox_id] or identity in self.stopped:
            return httpx.Response(409)
        self.live.add(identity)
        return httpx.Response(201, json={"session_id": session_id})

    def network(
        self, sandbox_id: str, operation: str, session_id: str, namespace: str
    ) -> httpx.Response:
        identity = sandbox_id, namespace, session_id
        if self.network_status:
            return self.command_response('{"error":"unavailable"}', exit_code=1)
        if operation == "revoke":
            self.network_revoked.add(identity)
            self.network_live.discard(identity)
            result = json.dumps({"stopped": identity in self.restart_stopped})
        else:
            assert operation == "grant"
            if (
                namespace != self.namespaces[sandbox_id]
                or identity in self.network_revoked
            ):
                return self.command_response('{"error":"denied"}', exit_code=1)
            self.network_live.add(identity)
            result = json.dumps({"token": "a" * 64})
        if self.lose_response == f"network_{operation}":
            self.lose_response = None
            raise httpx.ReadError("Network control response was lost")
        return self.command_response(self.network_output.get(operation, result))

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/sandboxes/"):
            response = await super().respond(request)
            if "/endpoints/" in path and response.status_code == 200:
                endpoint = _JSON_OBJECT.validate_json(response.content)
                endpoint["headers"] = {"X-EXECD-ACCESS-TOKEN": "private-execd"}
                return httpx.Response(200, json=endpoint)
            return response
        sandbox_id, _generation = self.endpoints[request.url.host]
        if path == "/command":
            body = _JSON_OBJECT.validate_json(await request.aread())
            command = body["command"]
            assert isinstance(command, str)
            if "/opt/sandbox-runtime/workspaces/network.py" in command:
                arguments = shlex.split(command)
                operation, session_id, namespace = arguments[-3:]
                if gate := self.gates.get(f"network_{operation}"):
                    try:
                        await gate.wait()
                    except asyncio.CancelledError:
                        self.late_network.append(
                            (sandbox_id, operation, session_id, namespace)
                        )
                        raise
                return self.network(sandbox_id, operation, session_id, namespace)
            if "/opt/sandbox-runtime/workspaces/registry.py" not in command:
                return await super().respond(request)
            arguments = shlex.split(command)
            assert arguments[:2] == ["printf", "%s"]
            assert arguments[3:] == [
                "|",
                "/opt/sandbox-runtime/venv/bin/python",
                "-I",
                "-S",
                "/opt/sandbox-runtime/workspaces/registry.py",
            ]
            payload = _JSON_OBJECT.validate_json(arguments[2])
            operation = payload["operation"]
            assert isinstance(operation, str)
            if gate := self.gates.get(operation):
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.late_registry.append((sandbox_id, payload))
                    raise
            exit_code, result = self.registry(sandbox_id, payload)
            if self.lose_response == operation:
                self.lose_response = None
                raise httpx.ReadError("Registry response was lost")
            return self.command_response(
                self.registry_output.get(operation, json.dumps(result)),
                exit_code=exit_code,
            )
        self.native_requests.append(request)
        if path == "/v1/isolated/capabilities":
            namespace = self.namespaces.setdefault(sandbox_id, str(uuid4()))
            return httpx.Response(
                200,
                json={
                    "available": True,
                    "client_session_ownership": True,
                    "namespace_exit_confirmation": True,
                    "rooted_filesystem": True,
                    "session_namespace": namespace,
                },
            )
        if path == "/v1/isolated/session":
            payload = _JSON_OBJECT.validate_json(await request.aread())
            if gate := self.gates.get("start"):
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.late_posts.append((sandbox_id, payload))
                    raise
            response = self.start(sandbox_id, payload)
            if self.lose_response == "start":
                self.lose_response = None
                raise httpx.ReadError("Native creation response was lost")
            return response
        session_id = path.split("/")[4]
        namespace = self.namespaces[sandbox_id]
        identity = sandbox_id, namespace, session_id
        if request.method == "DELETE":
            if gate := self.gates.get("stop"):
                await gate.wait()
            if request.url.params["session_namespace"] != namespace:
                return httpx.Response(409)
            if self.stop_status != 200:
                return httpx.Response(self.stop_status)
            self.live.discard(identity)
            self.stopped.add(identity)
            self.changes.publish()
            return httpx.Response(200)
        assert path.endswith("/run")
        if identity not in self.live:
            return httpx.Response(409)
        body = _JSON_OBJECT.validate_json(await request.aread())
        environment, command = body["envs"], body["code"]
        assert isinstance(environment, dict) and isinstance(command, str)
        self.environments.append(environment)
        if "/opt/sandbox-runtime/workspaces/initialize.py" in command:
            assert shlex.split(command) == [
                "/opt/sandbox-runtime/venv/bin/python",
                "-I",
                "-S",
                "/opt/sandbox-runtime/workspaces/initialize.py",
                session_id,
                "a" * 64,
            ]
            assert environment == {}
            if gate := self.gates.get("initialize"):
                await gate.wait()
            if self.initialization_timeout:
                raise httpx.ReadTimeout("Controlled initialization timeout")
            return self.command_response(
                self.initialization_output
                if self.initialization_output is not None
                else json.dumps({"HOME": "/home/workspace", "PATH": "/project/bin"}),
                exit_code=self.initialization_exit,
            )
        assert command.startswith("cd /workspace && ")
        return self.command_response(command.removeprefix("cd /workspace && "))


@asynccontextmanager
async def _projects(tmp_path: Path) -> AsyncIterator[tuple[_World, _WorkspaceRemote]]:
    async with _world(tmp_path) as world:
        remote = _WorkspaceRemote()
        remote.changes = world.changes
        world.remote = remote
        try:
            yield world, remote
        finally:
            for gate in remote.gates.values():
                gate.release.set()


def _project(
    manager: OpenSandboxManager[str], key: str = "project-a"
) -> _ProjectCoordinator[str]:
    return _ProjectCoordinator(manager, "owner", key)


async def _borrow(project: _ProjectCoordinator[str]) -> None:
    async with project.open():
        raise AssertionError("The controlled startup must not yield")


async def test_declaration_is_lazy_and_runs_share_only_the_project(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        world.clients[0].config = world.clients[0].config.model_copy(
            update={
                "env": {"MODEL_SECRET": "parent-only"},
                "command_env": {"EXPLICIT_SHELL_SETTING": "chosen"},
                "command_timeout": 17,
                "enable_capture_offload": True,
            }
        )
        manager._client.config = world.clients[0].config
        key = "project/汉字\x00'$(not-a-command)"
        project = _project(manager, key)
        assert remote.created == 0 and remote.records == {}
        async with project.open() as first, project.open() as second:
            assert isinstance(first, RootedOpenSandboxBackend)
            assert first is not second and first.id == second.id
            assert len(remote.live) == 2
            record = remote.records[
                (first.id, hashlib.sha256(key.encode()).hexdigest())
            ]
            assert len(record.sessions) == 2
            result = await first.aexecute("chosen command")
            assert result.output == "chosen command"
            assert first.enable_capture_offload
            assert remote.environments[-1] == {
                "HOME": "/home/workspace",
                "PATH": "/project/bin",
                "EXPLICIT_SHELL_SETTING": "chosen",
            }
            request = remote.native_requests[-1]
            assert _JSON_OBJECT.validate_json(request.content)["timeout_seconds"] == 17
            binding = await world.states[0].read_binding(_resource_key("owner"))
            assert binding is not None and binding.purpose == "workspaces"
            assert (
                await world.states[0].read_binding(_resource_key("owner", "project-a"))
                is None
            )
        assert record.phase == "active" and record.sessions == []
        assert remote.live == set() and remote.destroy_calls == []
        assert len(remote.stopped) == 2
        assert all(stream.closed for stream in remote.streams)
        assert first.is_closed and second.is_closed


@pytest.mark.parametrize("content", ['""', "null", "3"])
async def test_workspace_key_is_a_nonempty_opaque_string(
    tmp_path: Path, content: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        key = json.loads(content)
        with pytest.raises(ValueError if key == "" else TypeError):
            _ProjectCoordinator(manager, "owner", key)
        assert remote.created == 0


async def test_delete_without_binding_performs_no_remote_io(tmp_path: Path) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        await _project(manager).delete()
        assert remote.created == 0
        assert remote.records == {} and remote.executions == []
        assert remote.native_requests == []


@pytest.mark.parametrize("operation", ["open", "delete"])
async def test_wrong_purpose_rejects_before_workspace_requests(
    tmp_path: Path, operation: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        await manager.get("owner")
        with pytest.raises(OpenSandboxPurposeError):
            if operation == "open":
                async with _project(manager).open():
                    raise AssertionError("Command bindings must not admit workspaces")
            else:
                await _project(manager).delete()
        assert remote.created == 1 and remote.native_requests == []
        assert remote.records == {}


async def test_delete_does_not_recreate_an_unavailable_parent(tmp_path: Path) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        async with project.open() as backend:
            sandbox_id = backend.id
        remote.states.pop(sandbox_id)
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await project.delete()
        assert remote.created == 1
        assert next(iter(remote.records.values())).phase == "active"


async def test_cross_worker_delete_seals_all_runs_and_preserves_other_project(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        first_manager, second_manager = await world.add(), await world.add()
        first_project = _project(first_manager)
        second_project = _project(second_manager)
        other_project = _project(first_manager, "project-b")
        async with (
            first_project.open() as first,
            second_project.open(),
            other_project.open() as other,
        ):
            gate = remote.gates["stop"] = _Gate()
            deletion = world.spawn(second_project.delete())
            await gate.entered.wait()
            with pytest.raises(OpenSandboxBusyError):
                async with first_project.open():
                    raise AssertionError("Sealed projects must reject admission")
            record = remote.records[
                (first.id, hashlib.sha256(b"project-a").hexdigest())
            ]
            original_incarnation = record.incarnation
            assert record.phase == "deleting" and len(record.sessions) == 2
            gate.release.set()
            await deletion
            assert record.phase == "deleted" and len(remote.live) == 1
            assert (
                await other.aexecute("other remains usable")
            ).output == "other remains usable"
            with pytest.raises(OpenSandboxBackendProtocolError):
                await first.aexecute("stopped session")
        async with first_project.open():
            replacement = remote.records[
                (first.id, hashlib.sha256(b"project-a").hexdigest())
            ]
            assert replacement.incarnation != original_incarnation
        assert remote.created == 1 and remote.destroy_calls == []


@pytest.mark.parametrize("stage", ["reserve", "network_grant", "start", "initialize"])
async def test_startup_cancellation_fences_late_requests(
    tmp_path: Path, stage: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        gate = remote.gates[stage] = _Gate()
        task = world.spawn(_borrow(_project(manager)))
        await gate.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert remote.live == set()
        assert all(record.sessions == [] for record in remote.records.values())
        for sandbox_id, payload in remote.late_registry:
            status, result = remote.registry(sandbox_id, payload)
            assert status == 1 and result == remote.rejection("stale")[1]
        for sandbox_id, payload in remote.late_posts:
            assert remote.start(sandbox_id, payload).status_code == 409
        for sandbox_id, operation, session_id, namespace in remote.late_network:
            identity = sandbox_id, namespace, session_id
            assert identity in remote.network_revoked
            remote.network(sandbox_id, operation, session_id, namespace)
            assert identity not in remote.network_live
        assert remote.live == set()


@pytest.mark.parametrize("stage", ["reserve", "network_grant", "start"])
async def test_lost_creation_response_keeps_known_cleanup_identity(
    tmp_path: Path, stage: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        remote.lose_response = stage
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await _borrow(_project(manager))
        assert remote.live == set() and len(remote.stopped) == 1
        assert all(record.sessions == [] for record in remote.records.values())
        assert all(stream.closed for stream in remote.streams)


@pytest.mark.parametrize(
    ("stop_status", "error"),
    [
        (503, OpenSandboxBackendUnavailableError),
        (404, OpenSandboxBackendUnavailableError),
        (409, OpenSandboxBackendProtocolError),
    ],
)
async def test_failed_cleanup_retains_record_until_retry_confirms_delete(
    tmp_path: Path,
    stop_status: int,
    error: type[Exception],
) -> None:
    async with _projects(tmp_path) as (world, remote):
        first_manager = await world.add()
        first_project = _project(first_manager)
        with pytest.raises(error):
            async with first_project.open():
                remote.stop_status = stop_status
        record = next(iter(remote.records.values()))
        assert (
            record.phase == "active" and len(record.sessions) == len(remote.live) == 1
        )
        handle = first_manager._handles[_resource_key("owner")]
        assert not handle._is_idle()
        remote.stop_status = 200
        await first_project.delete()
        assert record.phase == "deleted" and remote.live == set()
        assert handle._is_idle()


async def test_network_cleanup_failure_preserves_ownership_after_native_stop(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with project.open():
                remote.network_status = 1
        record = next(iter(remote.records.values()))
        assert record.sessions and remote.live == set()
        assert remote.network_live
        handle = manager._handles[_resource_key("owner")]
        assert not handle._is_idle()
        remote.network_status = 0
        await project.delete()
        assert record.phase == "deleted"
        assert remote.network_live == set()
        assert handle._is_idle()


async def test_parent_restart_receipt_releases_open_run_ownership(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        async with project.open() as backend:
            remote.restart_parent(backend.id)
        record = next(iter(remote.records.values()))
        assert record.phase == "active" and record.sessions == []
        assert remote.live == remote.network_live == remote.stopped == set()
        assert manager._handles[_resource_key("owner")]._is_idle()


@pytest.mark.parametrize("cancel_delete", [False, True])
async def test_parent_restart_receipt_settles_retained_owner_and_allows_pause(
    tmp_path: Path,
    cancel_delete: bool,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with project.open() as backend:
                remote.stop_status = 503
        record = next(iter(remote.records.values()))
        retained = tuple(record.sessions)
        handle = manager._handles[_resource_key("owner")]
        assert retained and not handle._is_idle()
        deletions = [
            request for request in remote.native_requests if request.method == "DELETE"
        ]
        remote.restart_parent(backend.id)
        assert tuple(record.sessions) == retained
        if cancel_delete:
            gate = remote.gates["network_revoke"] = _Gate()
            deletion = world.spawn(project.delete())
            await gate.entered.wait()
            deletion.cancel()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await deletion
        else:
            await project.delete()
        assert record.phase == "deleted" and record.sessions == []
        assert remote.live == remote.network_live == set()
        assert [
            request for request in remote.native_requests if request.method == "DELETE"
        ] == deletions
        assert manager._handles[_resource_key("owner")] is handle
        assert handle._is_idle()
        await manager.pause("owner")
        assert remote.pause_calls == [(backend.id, 0, 0)]
        assert all(stream.closed for stream in remote.streams)


async def test_lost_restart_receipt_preserves_failures_and_unknown_command(
    tmp_path: Path,
) -> None:
    """A Run receipt cannot settle an unacknowledged command in its current parent."""
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with project.open() as backend:
                remote.stop_status = 503
        record = next(iter(remote.records.values()))
        retained = tuple(record.sessions)
        handle = manager._handles[_resource_key("owner")]
        remote.restart_parent(backend.id)
        remote.lose_response = "network_revoke"
        with pytest.raises(OpenSandboxLifecycleUncertainError) as captured:
            await project.delete()
        cause = captured.value.cause
        assert isinstance(cause, ExceptionGroup)
        assert {type(error) for error in cause.exceptions} == {
            OpenSandboxBackendUnavailableError,
            OpenSandboxBackendProtocolError,
        }
        assert record.phase == "deleting" and tuple(record.sessions) == retained
        assert not handle._is_idle()
        await project.delete()
        assert record.phase == "deleted" and record.sessions == []
        assert not handle._is_idle()


@pytest.mark.parametrize(
    "output",
    [
        "{}",
        '{"stopped":1}',
        '{"stopped":"true"}',
        '{"stopped":null}',
        '{"stopped":true,"extra":0}',
        "not json",
    ],
)
async def test_invalid_revoke_result_stops_native_but_retains_ownership(
    tmp_path: Path,
    output: str,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = manager.workspace("owner", workspace_key="project-a")
        with pytest.raises(OpenSandboxBackendProtocolError):
            async with project.open():
                remote.network_output["revoke"] = output
        record = next(iter(remote.records.values()))
        handle = manager._handles[_resource_key("owner")]
        assert record.sessions and not handle._is_idle()
        assert remote.live == set() and len(remote.stopped) == 1
        remote.network_output.clear()
        await project.delete()
        assert record.phase == "deleted" and handle._is_idle()


async def test_command_cancellation_settles_network_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        async with _project(manager).open() as backend:
            entered = asyncio.Event()

            async def hold(*arguments: object, **keywords: object) -> None:
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(_WorkspaceConnection, "run", hold)
            command = world.spawn(backend.aexecute("controlled command"))
            await entered.wait()
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
            assert not remote.network_live and not remote.live


async def test_repeated_cancellation_waits_for_cleanup_and_preserves_failure_cause(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        initialized = remote.gates["initialize"] = _Gate()
        stopping = remote.gates["stop"] = _Gate()
        task = world.spawn(_borrow(_project(manager)))
        await initialized.entered.wait()
        task.cancel()
        await stopping.entered.wait()
        task.cancel()
        remote.stop_status = 503
        stopping.release.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert captured.value.__cause__ is not None
        assert len(remote.live) == 1
        remote.stop_status = 200
        await _project(manager).delete()


async def test_body_failure_and_cleanup_failure_remain_observable(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        failure = ValueError("application failure")
        with pytest.raises(ValueError) as captured:
            async with _project(manager).open():
                remote.stop_status = 503
                raise failure
        assert captured.value is failure
        assert isinstance(failure.__cause__, OpenSandboxBackendUnavailableError)
        remote.stop_status = 200
        await _project(manager).delete()


@pytest.mark.parametrize("stage", ["cancel", "release"])
async def test_registry_cleanup_failure_still_stops_and_fences_the_known_session(
    tmp_path: Path, stage: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        remote.registry_output[stage] = "{}"
        with pytest.raises(OpenSandboxBackendProtocolError):
            async with _project(manager).open():
                pass
        handle = manager._handles[_resource_key("owner")]
        assert remote.live == set() and len(remote.stopped) == 1
        assert next(iter(remote.records.values())).sessions == []
        assert handle._is_idle()


async def test_lost_finish_confirmation_does_not_retain_already_stopped_sessions(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with project.open():
                remote.stop_status = 503
        remote.stop_status = 200
        remote.registry_output["finish_delete"] = "{}"
        with pytest.raises(OpenSandboxBackendProtocolError):
            await project.delete()
        assert next(iter(remote.records.values())).phase == "deleted"
        assert manager._handles[_resource_key("owner")]._is_idle()
        await project.delete()


async def test_local_connection_close_failure_uses_the_package_error_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        original_close = _WorkspaceConnection.aclose
        failure = RuntimeError("Internal transport detail")

        async def fail_after_close(connection: _WorkspaceConnection) -> None:
            await original_close(connection)
            raise failure

        monkeypatch.setattr(_WorkspaceConnection, "aclose", fail_after_close)
        with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
            async with _project(manager).open():
                pass
        assert captured.value.cause is failure
        assert "Internal transport detail" not in str(captured.value)
        assert remote.live == set()


@pytest.mark.parametrize(
    "failure", ["exit", "invalid_environment", "timeout", "wrong_owner"]
)
async def test_initialization_failure_never_yields_or_abandons_a_session(
    tmp_path: Path, failure: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        expected: type[Exception] = OpenSandboxBackendProtocolError
        if failure == "exit":
            remote.initialization_exit = 1
            expected = OpenSandboxInitializationError
        elif failure == "invalid_environment":
            remote.initialization_output = '{"HOME":1}'
        elif failure == "timeout":
            remote.initialization_timeout = True
            expected = OpenSandboxBackendTimeoutError
        else:
            remote.session_request_mismatch = True
        with pytest.raises(expected):
            await _borrow(_project(manager))
        assert remote.live == set()
        assert all(record.sessions == [] for record in remote.records.values())


@pytest.mark.parametrize("output", ["null", "not json", "x" * 257])
async def test_invalid_or_oversized_preparation_never_reserves_a_native_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        monkeypatch.setattr(_workspace_access, "_REGISTRY_LIMIT", 256)
        remote.registry_output["prepare"] = output
        with pytest.raises(OpenSandboxBackendProtocolError):
            await _borrow(_project(manager))
        assert remote.live == remote.stopped == set()
        assert all(record.sessions == [] for record in remote.records.values())


async def test_old_native_namespace_prevents_project_data_deletion(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        with pytest.raises(OpenSandboxBackendUnavailableError):
            async with project.open() as backend:
                remote.stop_status = 503
        record = next(iter(remote.records.values()))
        namespace = remote.namespaces[backend.id]
        remote.stop_status = 200
        remote.namespaces[backend.id] = str(uuid4())
        with pytest.raises(OpenSandboxBackendProtocolError):
            await project.delete()
        assert record.phase == "deleting" and len(record.sessions) == 1
        assert not manager._handles[_resource_key("owner")]._is_idle()
        remote.namespaces[backend.id] = namespace
        await project.delete()
        assert record.phase == "deleted"


async def test_manager_close_retains_parent_until_workspace_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        body = _Gate()
        stopping = remote.gates["stop"] = _Gate()
        connection_closed = asyncio.Event()
        original_close = _WorkspaceConnection.aclose

        async def observe_close(connection: _WorkspaceConnection) -> None:
            await original_close(connection)
            connection_closed.set()

        monkeypatch.setattr(_WorkspaceConnection, "aclose", observe_close)

        async def use_workspace() -> None:
            async with _project(manager).open():
                await body.wait()

        borrow = world.spawn(use_workspace())
        await body.entered.wait()
        close_started = asyncio.Event()

        async def close_manager() -> None:
            close_started.set()
            await manager.aclose()

        close = world.spawn(close_manager())
        await close_started.wait()
        with pytest.raises(OpenSandboxManagerClosedError):
            await _project(manager).delete()
        body.release.set()
        await stopping.entered.wait()
        assert not close.done() and not connection_closed.is_set()
        stopping.release.set()
        await borrow
        await close
        assert connection_closed.is_set() and remote.live == set()


async def test_pause_holds_owner_claim_without_blocking_run_cleanup(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        body = _Gate()

        async def use_workspace() -> None:
            async with _project(manager).open():
                await body.wait()

        borrow = world.spawn(use_workspace())
        await body.entered.wait()
        pause = world.spawn(manager.pause("owner"))
        while True:
            snapshot = await world.states[0].read_availability(_resource_key("owner"))
            if snapshot is not None and snapshot.phase == "draining":
                break
            await remote.changes.wait(0)
        assert remote.pause_calls == []
        body.release.set()
        await borrow
        remote.changes.publish()
        await pause
        assert remote.live == set()
        assert remote.pause_calls == [("sandbox-1", 0, 0)]


async def test_lost_seal_response_can_be_retried_without_reopening_admission(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        async with project.open():
            remote.lose_response = "begin_delete"
            with pytest.raises(OpenSandboxBackendUnavailableError):
                await project.delete()
            record = next(iter(remote.records.values()))
            assert record.phase == "deleting" and len(remote.live) == 1
            with pytest.raises(OpenSandboxBusyError):
                await _borrow(project)
            await project.delete()
            assert record.phase == "deleted" and remote.live == set()


async def test_fixed_run_never_follows_a_parent_handle_replacement(
    tmp_path: Path,
) -> None:
    async with _projects(tmp_path) as (world, remote):
        manager = await world.add()
        project = _project(manager)
        replacement = await world.clients[0].create(purpose="workspaces")
        try:
            async with project.open() as workspace:
                original_id = workspace.id
                handle = manager._handles[_resource_key("owner")]
                previous = handle._replace_backend(replacement)
                assert workspace.id == original_id and handle.id == replacement.id
                assert (await workspace.aexecute("fixed run")).output == "fixed run"
            handle._replace_backend(previous)
        finally:
            await replacement.aclose()
        assert {sandbox_id for sandbox_id, _namespace, _session in remote.stopped} == {
            original_id
        }
