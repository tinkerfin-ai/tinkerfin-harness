"""Own project admission and isolated Runs while retaining their physical parent."""

from __future__ import annotations

__all__ = ["_ProjectCoordinator"]

import asyncio
import hashlib
import json
import math
import re
import shlex
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar, overload
from uuid import uuid4

from deepagents.backends.protocol import ExecuteResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from tinkerfin_notifications import ResyncRequired

from ..backends._isolated import _uuid_text, _workspace_call, _WorkspaceConnection
from ..backends._operations import RemoteOperations, current_remote_operations
from ..backends._workspace_backend import _WorkspaceBackend
from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxError,
    OpenSandboxFileChangedError,
    OpenSandboxInitializationError,
    OpenSandboxLifecycleUncertainError,
)
from ._purpose import require_binding_purpose
from ._transport import _join_owned_task
from ._workspace_files import QueryResult, query_files
from ._workspace_watch import WorkspaceChange

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

_KeyT = TypeVar("_KeyT")
_REGISTRY_LIMIT = 16 * 1024 * 1024
_ENVIRONMENT_LIMIT = 64 * 1024
_RUNTIME_PYTHON = "/opt/sandbox-runtime/venv/bin/python -I -S"
_REGISTRY_COMMAND = f"{_RUNTIME_PYTHON} /opt/sandbox-runtime/workspaces/registry.py"
_INITIALIZE_COMMAND = f"{_RUNTIME_PYTHON} /opt/sandbox-runtime/workspaces/initialize.py"
_NETWORK_COMMAND = f"{_RUNTIME_PYTHON} /opt/sandbox-runtime/workspaces/network.py"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_ENVIRONMENT: TypeAdapter[dict[str, str]] = TypeAdapter(
    dict[str, str], config=ConfigDict(strict=True)
)


@dataclass(frozen=True, slots=True)
class _SessionOwner:
    """Keep a client-known native identity before dispatching any reservation."""

    session_id: str
    session_namespace: str

    @property
    def identity(self) -> str:
        return f"workspace:{self.session_namespace}:{self.session_id}"

    def payload(self) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "session_namespace": self.session_namespace,
        }


@dataclass(frozen=True, slots=True)
class _ProjectRecord:
    """Retain the exact admission and deletion identities returned by the registry."""

    incarnation: str
    phase: Literal["active", "deleting", "deleted"]
    sessions: tuple[_SessionOwner, ...]
    deletion_id: str | None


class _OwnerPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str
    session_namespace: str

    _canonical_identity = field_validator("session_id", "session_namespace")(_uuid_text)


class _RecordPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    incarnation: str
    phase: Literal["active", "deleting", "deleted"]
    sessions: list[_OwnerPayload] = Field(max_length=65536)
    deletion_id: str | None

    _canonical_identity = field_validator("incarnation")(_uuid_text)

    @model_validator(mode="after")
    def validate_admission(self) -> _RecordPayload:
        if self.deletion_id is not None:
            _uuid_text(self.deletion_id)
        if (self.phase == "active") != (self.deletion_id is None):
            raise ValueError("registry deletion ownership is inconsistent")
        if self.phase == "deleted" and self.sessions:
            raise ValueError("deleted projects cannot retain sessions")
        if len({owner.session_id for owner in self.sessions}) != len(self.sessions):
            raise ValueError("registry session identities must be unique")
        return self


class _RegistryFailure(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: Literal["busy", "stale", "unavailable", "changed"]
    message: str


class _RegistryRejection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    error: _RegistryFailure


def _record(payload: JsonValue) -> _ProjectRecord | None:
    if payload is None:
        return None
    try:
        parsed = _RecordPayload.model_validate(payload)
    except ValidationError as error:
        raise OpenSandboxBackendProtocolError(
            "Workspace registry returned an invalid ownership record", cause=error
        ) from error
    return _ProjectRecord(
        parsed.incarnation,
        parsed.phase,
        tuple(
            _SessionOwner(owner.session_id, owner.session_namespace)
            for owner in parsed.sessions
        ),
        parsed.deletion_id,
    )


def _response_content(response: ExecuteResponse, *, limit: int) -> str:
    content = response.output
    if (
        response.truncated
        or len(content) > limit
        or len(content.encode("utf-8")) > limit
    ):
        raise OpenSandboxBackendProtocolError(
            "Workspace control response exceeds its byte limit",
            context={"max_bytes": limit},
        )
    return content


def _raise_failures(failures: list[Exception]) -> None:
    if len(failures) == 1:
        raise failures[0]
    if failures:
        cause = ExceptionGroup("Workspace cleanup failures", failures)
        raise OpenSandboxLifecycleUncertainError(
            "Workspace cleanup could not be confirmed", cause=cause
        ) from cause


async def _finish_cleanup(
    operation: Coroutine[None, None, None], *, primary: BaseException | None
) -> None:
    """Keep cleanup owned through repeated cancellation and preserve both outcomes."""
    task = asyncio.create_task(operation, name="tinkerfin-workspace-settlement")
    try:
        await _join_owned_task(task, failure_label="Workspace cleanup")
    except asyncio.CancelledError as cancellation:
        evidence: list[BaseException] = []
        if not task.cancelled():
            task_error = task.exception()
            if task_error is not None:
                evidence.append(task_error)
        if primary is not None and primary is not cancellation:
            evidence.append(primary)
        if evidence:
            cause = (
                evidence[0]
                if len(evidence) == 1
                else BaseExceptionGroup("Workspace failure evidence", evidence)
            )
            raise cancellation from cause
        raise
    except BaseException as cleanup_error:
        if primary is not None:
            raise primary from cleanup_error
        raise


class _ProjectCoordinator(Generic[_KeyT]):
    """Own isolated Run cleanup without acquiring a physical owner claim on exit.

    The parent lease covers admission, native creation, user work, and settlement.
    Pause may hold the owner claim while waiting for that lease, so every cleanup
    call uses only its fixed parent and connection. Registry cancellation and
    native namespace identities fence delayed work. Exact parent restart receipts
    can confirm termination without an old-namespace DELETE.
    See the registry CLI and cancellation contracts in test_workspace_lifecycle.
    """

    def __init__(
        self,
        manager: OpenSandboxManager[_KeyT],
        key: _KeyT,
        workspace_key: str,
    ) -> None:
        if not isinstance(workspace_key, str):
            raise TypeError("workspace_key must be a string")
        if not workspace_key:
            raise ValueError("workspace_key must not be empty")
        self._manager = manager
        self._owner_key = manager._resolve_resource_key(key, None)
        self._project = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()

    @property
    def _control_timeout(self) -> float:
        return self._manager._client.config.ready_timeout.total_seconds()

    @asynccontextmanager
    async def watch(
        self,
    ) -> AsyncGenerator[AsyncIterator[WorkspaceChange | ResyncRequired], None]:
        """Use the bound project identity without creating an execution context."""
        async with self._manager._workspace_watches.watch(
            self._owner_key, self._project
        ) as changes:
            yield changes

    async def query_files(
        self,
        operation: Literal["list", "stat", "text"],
        path: str,
        arguments: dict[str, JsonValue],
    ) -> QueryResult:
        """Query the bound project without entering its execution lifecycle."""
        return await query_files(
            self._manager,
            self._owner_key,
            self._project,
            operation=operation,
            path=path,
            arguments=arguments,
        )

    @asynccontextmanager
    async def _parent(
        self, *, allow_create: bool
    ) -> AsyncGenerator[tuple[OpenSandboxHandle, OpenSandboxBackend] | None, None]:
        owner_key = self._owner_key
        async with self._manager._operation(), AsyncExitStack() as leases:
            borrowed: tuple[OpenSandboxHandle, OpenSandboxBackend] | None = None
            async with self._manager._claim_owner(owner_key) as claim:
                require_binding_purpose(claim.binding, "workspaces")
                if allow_create or claim.binding is not None:
                    await self._manager._availability.require_running(owner_key)
                    handle = await self._manager._get_locked(
                        owner_key,
                        claim,
                        purpose="workspaces",
                        allow_recreate=allow_create,
                    )
                    await self._manager._availability.register(owner_key, claim, handle)
                    parent = await leases.enter_async_context(handle._alease())
                    borrowed = handle, parent
            yield borrowed

    async def _registry(
        self,
        parent: OpenSandboxBackend,
        operation: str,
        arguments: Mapping[str, JsonValue],
    ) -> JsonValue:
        request = json.dumps(
            {"operation": operation, "project": self._project, "arguments": arguments},
            separators=(",", ":"),
        )
        if len(request.encode("utf-8")) > _REGISTRY_LIMIT:
            raise OpenSandboxBackendProtocolError(
                "Workspace registry request exceeds its byte limit"
            )

        command = f"printf %s {shlex.quote(request)} | {_REGISTRY_COMMAND}"

        async def execute() -> ExecuteResponse:
            async with asyncio.timeout(self._control_timeout):
                with parent._rooted_file_operation():
                    if len(command.encode("utf-8")) <= 48 * 1024:
                        return await parent.aexecute(
                            command,
                            timeout=max(1, math.ceil(self._control_timeout)),
                        )
                    # Execd invokes the shell with one -c argument. Large JSON
                    # requests must travel as files to avoid Linux MAX_ARG_STRLEN.
                    path = f"/tmp/.tinkerfin-registry-{uuid4()}.json"
                    primary: BaseException | None = None
                    try:
                        await parent._file_request(
                            parent._sandbox.files.write_file(
                                path, request.encode("utf-8"), mode=600
                            )
                        )
                        return await parent.aexecute(
                            f"{_REGISTRY_COMMAND} < {shlex.quote(path)}",
                            timeout=max(1, math.ceil(self._control_timeout)),
                        )
                    except BaseException as error:
                        primary = error
                        raise
                    finally:

                        async def remove_request() -> None:
                            await parent._file_request(
                                parent._sandbox.files.delete_files([path])
                            )

                        await _finish_cleanup(remove_request(), primary=primary)

        response = await _workspace_call(execute())
        content = _response_content(response, limit=_REGISTRY_LIMIT)
        if response.exit_code == 0:
            try:
                result = _JSON.validate_json(content, strict=True)
            except ValidationError as error:
                raise OpenSandboxBackendProtocolError(
                    "Workspace registry returned invalid JSON", cause=error
                ) from error
            if operation in {"release", "finish_delete"} and result is not None:
                raise OpenSandboxBackendProtocolError(
                    "Workspace registry returned an unexpected completion result"
                )
            return result
        if response.exit_code != 1:
            raise OpenSandboxBackendProtocolError(
                "Workspace registry did not return a terminal result"
            )
        try:
            rejection = _RegistryRejection.model_validate_json(content)
        except ValidationError as error:
            raise OpenSandboxBackendProtocolError(
                "Workspace registry returned an invalid rejection", cause=error
            ) from error
        reason = rejection.error.reason
        if reason == "busy":
            raise OpenSandboxBusyError("Workspace is in use or under maintenance")
        if reason == "changed":
            raise OpenSandboxFileChangedError("Managed files have local modifications")
        if reason == "stale":
            raise OpenSandboxLifecycleUncertainError(
                "Workspace admission is no longer current", context={"reason": reason}
            )
        raise OpenSandboxBackendUnavailableError(
            "Workspace registry is unavailable", context={"reason": reason}
        )

    @overload
    async def _network(
        self,
        parent: OpenSandboxBackend,
        owner: _SessionOwner,
        operation: Literal["grant"],
    ) -> str: ...

    @overload
    async def _network(
        self,
        parent: OpenSandboxBackend,
        owner: _SessionOwner,
        operation: Literal["revoke"],
    ) -> bool: ...

    async def _network(
        self,
        parent: OpenSandboxBackend,
        owner: _SessionOwner,
        operation: Literal["grant", "revoke"],
    ) -> str | bool:
        command = (
            f"{_NETWORK_COMMAND} {operation} "
            f"{shlex.quote(owner.session_id)} {shlex.quote(owner.session_namespace)}"
        )

        async def execute() -> ExecuteResponse:
            async with asyncio.timeout(self._control_timeout):
                with parent._rooted_file_operation():
                    return await parent.aexecute(
                        command, timeout=max(1, math.ceil(self._control_timeout))
                    )

        response = await _workspace_call(execute())
        content = _response_content(response, limit=4096)
        if response.exit_code != 0:
            raise OpenSandboxBackendUnavailableError(
                "Workspace network control is unavailable"
            )
        try:
            payload = _JSON.validate_json(content, strict=True)
        except ValidationError as error:
            raise OpenSandboxBackendProtocolError(
                "Workspace network control returned invalid JSON", cause=error
            ) from error
        if (
            operation == "revoke"
            and isinstance(payload, dict)
            and set(payload) == {"stopped"}
            and isinstance(stopped := payload["stopped"], bool)
        ):
            return stopped
        if (
            operation == "grant"
            and isinstance(payload, dict)
            and set(payload) == {"token"}
        ):
            token = payload["token"]
            if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token):
                return token
        raise OpenSandboxBackendProtocolError(
            "Workspace network control returned an invalid result"
        )

    async def _stop_run(
        self,
        parent: OpenSandboxBackend,
        connection: _WorkspaceConnection,
        owner: _SessionOwner,
    ) -> None:
        """Confirm network revocation and termination before releasing ownership.

        The supervisor can attest that this exact owner ended during a complete
        parent restart. Otherwise native DELETE must confirm the recorded namespace;
        a nonce mismatch alone never proves exit. See test_workspace_lifecycle.
        """
        failures: list[Exception] = []
        stopped = False
        try:
            stopped = await self._network(parent, owner, "revoke")
        except OpenSandboxError as error:
            failures.append(error)
        if not stopped:
            try:
                await connection.stop(owner.session_id, owner.session_namespace)
            except OpenSandboxError as error:
                failures.append(error)
        _raise_failures(failures)

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[RootedOpenSandboxBackend, None]:
        """Borrow one isolated Run and retain its project files after confirmed exit."""
        async with self._parent(allow_create=True) as borrowed:
            assert borrowed is not None
            handle, parent = borrowed
            connection = await _workspace_call(_WorkspaceConnection.connect(parent))
            owner: _SessionOwner | None = None
            incarnation: str | None = None
            backend: _WorkspaceBackend | None = None
            primary: BaseException | None = None
            reserved = False
            operations = current_remote_operations(parent._remote_operations)
            try:
                namespace = await connection.capabilities()
                owner = _SessionOwner(str(uuid4()), namespace)
                prepared = _record(await self._registry(parent, "prepare", {}))
                if prepared is None or prepared.phase != "active":
                    raise OpenSandboxBackendProtocolError(
                        "Workspace preparation did not return active admission"
                    )
                incarnation = prepared.incarnation
                arguments = {"incarnation": incarnation, **owner.payload()}
                operations.retain_resource(owner.identity)
                reserved = True
                reservation = _record(
                    await self._registry(parent, "reserve", arguments)
                )
                if (
                    reservation is None
                    or reservation.incarnation != incarnation
                    or reservation.phase != "active"
                    or owner not in reservation.sessions
                ):
                    raise OpenSandboxBackendProtocolError(
                        "Workspace reservation did not confirm its ownership"
                    )
                egress_token = await self._network(parent, owner, "grant")
                request = await self._registry(parent, "session_request", arguments)
                if not isinstance(request, dict) or any(
                    request.get(field) != value
                    for field, value in owner.payload().items()
                ):
                    raise OpenSandboxBackendProtocolError(
                        "Workspace session request has inconsistent ownership"
                    )
                await connection.start(request)
                environment = await self._initialize(
                    connection, owner.session_id, egress_token
                )
                environment.update(self._manager._client.config.command_env)
                run_owner = owner
                backend = _WorkspaceBackend(
                    connection,
                    session_id=owner.session_id,
                    session_namespace=owner.session_namespace,
                    stop_run=lambda: self._stop_run(parent, connection, run_owner),
                    environment=environment,
                    default_timeout=self._manager._client.config.command_timeout,
                    enable_capture_offload=self._manager._client.config.enable_capture_offload,
                )
                yield backend
            except BaseException as error:
                primary = error
                raise
            finally:
                await _finish_cleanup(
                    self._close_run(
                        parent,
                        handle,
                        connection,
                        operations,
                        owner=owner if reserved else None,
                        incarnation=incarnation,
                        backend=backend,
                    ),
                    primary=primary,
                )

    async def _initialize(
        self, connection: _WorkspaceConnection, session_id: str, egress_token: str
    ) -> dict[str, str]:
        """Initialize inside the private namespace before explicit Shell overrides.

        Only the runtime's returned project environment and configured command_env
        reach user commands. Physical container environment and model credentials
        are never copied into this boundary.
        """

        async def execute() -> ExecuteResponse:
            async with asyncio.timeout(self._control_timeout):
                return await connection.run(
                    session_id,
                    command=f"{_INITIALIZE_COMMAND} {shlex.quote(session_id)} {shlex.quote(egress_token)}",
                    environment={},
                    timeout=max(1, math.ceil(self._control_timeout)),
                )

        response = await _workspace_call(execute())
        content = _response_content(response, limit=_ENVIRONMENT_LIMIT)
        if response.exit_code != 0:
            raise OpenSandboxInitializationError("Workspace initialization failed")
        try:
            return _ENVIRONMENT.validate_json(content)
        except ValidationError as error:
            raise OpenSandboxBackendProtocolError(
                "Workspace initialization returned an invalid environment", cause=error
            ) from error

    async def _close_run(
        self,
        parent: OpenSandboxBackend,
        handle: OpenSandboxHandle,
        connection: _WorkspaceConnection,
        operations: RemoteOperations,
        *,
        owner: _SessionOwner | None,
        incarnation: str | None,
        backend: _WorkspaceBackend | None,
    ) -> None:
        failures: list[Exception] = []
        reservation_fenced = False
        try:
            if owner is not None:
                try:
                    cancelled = _record(
                        await self._registry(parent, "cancel", owner.payload())
                    )
                    if cancelled is not None:
                        if (
                            cancelled.incarnation != incarnation
                            or owner not in cancelled.sessions
                        ):
                            raise OpenSandboxBackendProtocolError(
                                "Workspace cancellation returned different ownership"
                            )
                        incarnation = cancelled.incarnation
                    reservation_fenced = True
                except Exception as error:  # noqa: BLE001 - still stop the known session
                    failures.append(error)
                try:
                    if backend is None:
                        await self._stop_run(parent, connection, owner)
                    else:
                        await backend.aclose()
                except Exception as error:  # noqa: BLE001 - preserve cleanup evidence
                    failures.append(error)
                else:
                    assert incarnation is not None
                    try:
                        await self._registry(
                            parent,
                            "release",
                            {"incarnation": incarnation, **owner.payload()},
                        )
                    except Exception as error:  # noqa: BLE001 - retain the obligation
                        failures.append(error)
                    else:
                        reservation_fenced = True
                    if reservation_fenced:
                        operations.confirm_resource_stopped(owner.identity)
                        handle._confirm_remote_resource(parent.id, owner.identity)
        finally:
            try:
                await _workspace_call(connection.aclose())
            except Exception as error:  # noqa: BLE001 - retain all cleanup outcomes
                failures.append(error)
        _raise_failures(failures)

    async def delete(self) -> None:
        """Stop every recorded Run and remove only its sealed project incarnation."""
        async with self._parent(allow_create=False) as borrowed:
            if borrowed is None:
                return
            handle, parent = borrowed
            connection = await _workspace_call(_WorkspaceConnection.connect(parent))
            primary: BaseException | None = None
            try:
                await connection.capabilities()
                await _finish_cleanup(
                    self._delete_project(parent, handle, connection), primary=None
                )
            except BaseException as error:
                primary = error
                raise
            finally:
                await _finish_cleanup(
                    _workspace_call(connection.aclose()), primary=primary
                )

    async def _delete_project(
        self,
        parent: OpenSandboxBackend,
        handle: OpenSandboxHandle,
        connection: _WorkspaceConnection,
    ) -> None:
        record = _record(await self._registry(parent, "begin_delete", {}))
        self._manager._workspace_watches.stop(self._owner_key, self._project)
        if record is None or record.phase == "deleted":
            return
        if record.phase != "deleting" or record.deletion_id is None:
            raise OpenSandboxBackendProtocolError(
                "Workspace deletion did not seal project admission"
            )
        operations = current_remote_operations(parent._remote_operations)
        failures: list[Exception] = []
        for owner in record.sessions:
            operations.retain_resource(owner.identity)
            try:
                await self._stop_run(parent, connection, owner)
            except Exception as error:  # noqa: BLE001 - attempt every recorded owner
                failures.append(error)
            else:
                operations.confirm_resource_stopped(owner.identity)
                handle._confirm_remote_resource(parent.id, owner.identity)
        _raise_failures(failures)
        await self._registry(
            parent,
            "finish_delete",
            {
                "incarnation": record.incarnation,
                "deletion_id": record.deletion_id,
                "confirmed": [owner.payload() for owner in record.sessions],
            },
        )
