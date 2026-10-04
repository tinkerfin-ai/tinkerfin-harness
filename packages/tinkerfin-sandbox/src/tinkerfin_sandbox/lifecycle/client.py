"""Native asynchronous lifecycle client for OpenSandbox instances.

The client uses the asynchronous OpenSandbox 0.1.16 API. It retains creation and
connection tasks after caller cancellation so late resources are reclaimed.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from opensandbox import Sandbox
from opensandbox import SandboxManager as OpenSandboxSDKManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxInternalException,
    SandboxReadyTimeoutException,
)
from opensandbox.models import WriteEntry
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import SandboxFilter, SandboxInfo
from opensandbox.transport import RetryPolicy
from pydantic import BaseModel, ConfigDict

from ..backends.sdk import (
    OpenSandboxBackend,
    _connection_failure_reason,
    unavailable_reason,
)
from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxError,
    OpenSandboxInitializationError,
    OpenSandboxPurposeError,
    UnexpectedOpenSandboxBackendError,
)
from ..models import (
    OpenSandboxConfig,
    OpenSandboxDiagnosticContent,
    OpenSandboxPurpose,
    OpenSandboxRuntimeInfo,
)
from ._protocols import _SandboxClient
from ._purpose import PURPOSE_METADATA_KEY, require_remote_purpose, validate_purpose
from ._sql_tasks import select_failure
from ._transport import _join_owned_task, _SDKRequestTracker

_CREATE_TOKEN_METADATA_KEY = "tinkerfin.ai/create-token"
_WORKSPACE_PROBE_SECONDS = 30.0
_WORKSPACE_POLL_SECONDS = 0.2
# Manager recovery scopes this deadline to its same-instance work. Child SDK tasks
# inherit it so cancellation can settle under the owner claim without extending
# connection or initialization work to the client's longer standalone deadline.
_connection_deadline: ContextVar[float | None] = ContextVar(
    "tinkerfin_sandbox_connection_deadline", default=None
)


class _WorkspaceReadiness(BaseModel):
    """Accept only the trusted runtime's explicit network readiness result."""

    model_config = ConfigDict(strict=True, extra="forbid")

    ready: bool


def _transient_workspace_probe(error: Exception) -> bool:
    """Retry only this read-only probe's typed connectivity or service failures."""
    if _connection_failure_reason(error) in {"timeout", "unreachable"}:
        return True
    if isinstance(error, httpx.RemoteProtocolError):
        return True
    if isinstance(error, SandboxInternalException) and isinstance(
        error.__cause__, Exception
    ):
        return _transient_workspace_probe(error.__cause__)
    return False


def _backend_error(
    operation: str,
    error: Exception,
) -> OpenSandboxError:
    diagnostic_context = {
        "implementation": "opensandbox_sdk",
        "operation": operation,
    }
    if isinstance(error, OpenSandboxError):
        error._enrich_diagnostic_context(diagnostic_context)
        return error
    if isinstance(error, SandboxReadyTimeoutException | TimeoutError):
        return OpenSandboxBackendTimeoutError(
            f"OpenSandbox {operation} timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    if isinstance(error, (TypeError, ValueError)):
        return OpenSandboxBackendProtocolError(
            f"OpenSandbox {operation} returned an invalid response",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    reason = _connection_failure_reason(error)
    if reason is not None:
        return OpenSandboxBackendUnavailableError(
            f"OpenSandbox is unavailable for {operation}",
            context={"reason": reason},
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return UnexpectedOpenSandboxBackendError(
        f"OpenSandbox {operation} failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


OpenSandboxInitializer = Callable[
    [OpenSandboxBackend],
    Awaitable[None] | None,
]


class OpenSandboxClient(_SandboxClient):
    """Create, connect, inspect, and destroy native asynchronous Sandboxes.

    ``create`` transfers backend ownership to its caller. ``connect`` opens only a
    local connection, ``inspect`` is read-only, and ``destroy`` is idempotent when
    the remote instance is absent. Initializers run in declaration order and may be
    native async callbacks or non-blocking synchronous callbacks.

    Workspace-purpose connections have full parent-instance access. Keep them in
    trusted lifecycle code; project workloads require isolated workspace sessions.
    """

    _tinkerfin_error_boundary = True

    def __init__(
        self,
        *,
        connection_config: ConnectionConfig | None,
        config: OpenSandboxConfig | None = None,
        initializers: Sequence[OpenSandboxInitializer] = (),
    ) -> None:
        """Configure the client without opening a connection.

        Args:
            connection_config: SDK endpoint, authentication, and asynchronous
                transport configuration. ``None`` lets the SDK read its standard
                environment variables. Implicit SDK transport retries are disabled;
                explicit policies must not replay POST/PATCH response failures.
                SDK creation telemetry is disabled in the managed copy because its
                background tasks cannot be joined at client close. Caller transports
                remain borrowed and keep their original policy and lifetime.
            config: Image, resources, mounts, timeouts, metadata, and health policy.
            initializers: Idempotent callbacks run in order after creation or connect.
                Use native async callbacks for I/O. Synchronous callbacks run on the
                event loop and must not block. Connect and initialization share
                ``config.connect_timeout``; cancellation and timeouts cannot preempt
                blocking code. Async callbacks must propagate cancellation.
        """
        self.config = config or OpenSandboxConfig()
        self.connection_config = self._resolve_connection_config(connection_config)
        self._sdk_requests = _SDKRequestTracker()
        (
            self._owned_connection_config,
            self._sdk_connection_config,
        ) = self._scope_sdk_transport(self.connection_config)
        self._initializers = tuple(initializers)
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._destroy_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_destruction: dict[str, OpenSandboxError] = {}
        self._pending_closes: dict[OpenSandboxBackend, OpenSandboxError] = {}
        self._close_task: asyncio.Task[None] | None = None

    def _resolve_connection_config(
        self,
        connection_config: ConnectionConfig | None,
    ) -> ConnectionConfig:
        """Replace only the SDK's implicit short timeout for cold image pulls."""
        if connection_config is None:
            return ConnectionConfig(
                request_timeout=self.config.lifecycle_request_timeout
            )
        if "request_timeout" in connection_config.model_fields_set:
            return connection_config
        return connection_config.model_copy(
            update={
                "request_timeout": self.config.lifecycle_request_timeout,
            }
        )

    def _scope_sdk_transport(
        self,
        connection_config: ConnectionConfig,
    ) -> tuple[ConnectionConfig | None, ConnectionConfig]:
        """Give every SDK call one client-scoped borrowed transport.

        ``opensandbox==0.1.16`` creates a transport inside ``Sandbox.connect`` when
        the supplied config has none, but its exception cleanup does not catch task
        cancellation. The client therefore creates that transport before any SDK
        coroutine starts and retains the only owning config until :meth:`aclose`.
        SDK resources receive a validated borrowed copy so closing a temporary
        Sandbox cannot close the shared client transport. A caller-supplied transport
        remains borrowed and is never closed here.
        """

        policy = connection_config.retry_policy
        if policy.max_retries > 0 and policy.retryable_status_codes_non_idempotent:
            raise ValueError(
                "OpenSandbox retry_policy must not replay POST/PATCH response failures"
            )
        # Configuration construction may add the SDK client-IP header in place.
        # Only our own copied headers may be changed during SDK validation.
        managed_config = connection_config.model_copy(
            update={
                "headers": dict(connection_config.headers),
                "disable_metrics": True,
                "retry_policy": (
                    policy
                    if "retry_policy" in connection_config.model_fields_set
                    else RetryPolicy.disabled()
                ),
            }
        )
        owner = (
            managed_config.with_transport_if_missing()
            if managed_config.transport is None
            else None
        )
        source = managed_config if owner is None else owner
        sdk_config = ConnectionConfig.model_validate(source.model_dump(mode="python"))
        if sdk_config.transport is None:
            raise RuntimeError("OpenSandbox SDK transport initialization failed")
        sdk_config.transport = self._sdk_requests.borrow(sdk_config.transport)
        return owner, sdk_config

    def _wrap(self, sandbox: Sandbox) -> OpenSandboxBackend:
        """Wrap an SDK sandbox as an asynchronous Deep Agents backend."""
        return OpenSandboxBackend(
            sandbox=sandbox,
            default_timeout=self.config.command_timeout,
            command_env=self.config.command_env,
            working_directory=self.config.workspace_root,
            health_command=self.config.health_command,
            enable_capture_offload=self.config.enable_capture_offload,
        )

    async def _initialize(self, backend: OpenSandboxBackend) -> None:
        """Run initializers in declaration order and stop at the first failure."""
        try:
            with self._sdk_requests.caller_work():
                for initializer in self._initializers:
                    result = initializer(backend)
                    if inspect.isawaitable(result):
                        await result
        except Exception as error:
            raise OpenSandboxInitializationError(
                "OpenSandbox initializer failed", cause=error
            ) from error

    async def _initialize_workspace(self, sandbox: Sandbox) -> None:
        """Idempotently create the shell and file-tool workspace through the SDK."""
        workspace_root = self.config.workspace_root
        if workspace_root is None:
            return
        await sandbox.files.create_directories(
            # SDK 0.1.16 expects decimal Unix permission text, not 0o755.
            [WriteEntry(path=workspace_root, mode=755)]
        )

    async def _wait_workspace_ready(self, sandbox: Sandbox, *, deadline: float) -> None:
        """Bound every probe and interval by one workspace readiness deadline.

        OpenSandbox 0.1.16 disables SSE read timeouts and its health loop hides
        typed failures. Direct command probes retain those causes and close their
        response on cancellation. Only workspace-purpose instances use this path;
        ordinary command instances retain the SDK's existing ping contract.
        """
        loop = asyncio.get_running_loop()
        last_error: Exception | None = None
        while loop.time() < deadline:
            probe_timeout = asyncio.timeout_at(
                min(deadline, loop.time() + _WORKSPACE_PROBE_SECONDS)
            )
            try:
                async with probe_timeout:
                    result = await sandbox.commands.run(
                        "/opt/sandbox-runtime/venv/bin/python -I -S "
                        "/opt/sandbox-runtime/workspaces/network.py status",
                        opts=RunCommandOpts(
                            timeout=timedelta(seconds=_WORKSPACE_PROBE_SECONDS)
                        ),
                    )
                if result.error is not None or result.exit_code != 0:
                    raise OpenSandboxBackendProtocolError(
                        "OpenSandbox workspace readiness command did not succeed",
                        diagnostic_context={
                            "operation": "workspace readiness",
                            "exit_code": result.exit_code,
                            "command_error": result.error is not None,
                        },
                    )
                payload = "".join(message.text for message in result.logs.stdout)
                if _WorkspaceReadiness.model_validate_json(payload).ready:
                    return
            except Exception as error:
                if (
                    isinstance(error, TimeoutError) and probe_timeout.expired()
                ) or _transient_workspace_probe(error):
                    last_error = error
                else:
                    translated = _backend_error("workspace readiness", error)
                    if translated is error:
                        raise
                    raise translated from error
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            interval_timeout = asyncio.timeout_at(deadline)
            try:
                async with interval_timeout:
                    await asyncio.sleep(min(_WORKSPACE_POLL_SECONDS, remaining))
            except TimeoutError:
                if not interval_timeout.expired():
                    raise
                break
        failure = OpenSandboxBackendTimeoutError(
            "OpenSandbox workspace readiness timed out",
            diagnostic_context={
                "operation": "workspace readiness",
                "last_result": "probe_failed"
                if last_error is not None
                else "not_ready",
            },
            cause=last_error,
        )
        raise failure from last_error

    def _connect_deadline(self) -> float:
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.connect_timeout.total_seconds()
        )
        inherited = _connection_deadline.get()
        return deadline if inherited is None else min(deadline, inherited)

    @staticmethod
    def _cleanup_error(
        operation: str, sandbox_id: str, error: BaseException
    ) -> OpenSandboxError:
        """Retain cleanup evidence without replaying an earlier cancellation."""
        failure = (
            _backend_error(operation, error)
            if isinstance(error, Exception)
            else OpenSandboxBackendError(
                "OpenSandbox resource cleanup was interrupted", cause=error
            )
        )
        failure._enrich_diagnostic_context(
            {"operation": operation, "sandbox_id": sandbox_id}
        )
        return failure

    async def _close_backend(
        self,
        backend: OpenSandboxBackend,
        *,
        owned: bool,
        primary: BaseException | None = None,
    ) -> None:
        """Close after kill failure while preserving every original failure.

        The enclosing lifecycle task owns both awaits. Cancellation and process
        control keep precedence without skipping local close or losing the
        readiness failure that caused reclamation. Failed owned destruction and
        local close remain with this client for its next explicit close attempt.
        """
        failure = primary
        if owned:
            try:
                await backend.akill()
            except BaseException as error:  # noqa: BLE001 - close remains mandatory after kill failure
                cleanup_error = self._cleanup_error("destroy", backend.id, error)
                if cleanup_error.context.get("reason") != "not_found":
                    self._pending_destruction.setdefault(backend.id, cleanup_error)
                    outcome = cleanup_error if isinstance(error, Exception) else error
                    failure = (
                        outcome if failure is None else select_failure(failure, outcome)
                    )
                else:
                    self._pending_destruction.pop(backend.id, None)
            else:
                self._pending_destruction.pop(backend.id, None)
        try:
            await backend.aclose()
        except BaseException as error:  # noqa: BLE001 - retain both preparation and close outcomes
            cleanup_error = self._cleanup_error("close", backend.id, error)
            self._pending_closes.setdefault(backend, cleanup_error)
            outcome = cleanup_error if isinstance(error, Exception) else error
            failure = outcome if failure is None else select_failure(failure, outcome)
        else:
            self._pending_closes.pop(backend, None)
        if failure is not None:
            raise failure

    @staticmethod
    async def _close_sdk_quietly(
        sandbox: Sandbox,
    ) -> None:
        try:
            await sandbox.close()
        except Exception:  # noqa: BLE001 - temporary SDK close is best effort
            pass

    async def _create(
        self,
        metadata: Mapping[str, str] | None,
        purpose: OpenSandboxPurpose,
        *,
        cancellation_requested: asyncio.Event,
        owned_instance: asyncio.Event,
    ) -> OpenSandboxBackend:
        """Create and initialize a sandbox, reclaiming it on initialization failure."""
        async with self._sdk_requests.operation():
            volumes = [volume.model_copy(deep=True) for volume in self.config.volumes]
            creation_metadata = dict(self.config.metadata)
            creation_metadata.update(metadata or {})
            creation_metadata[_CREATE_TOKEN_METADATA_KEY] = uuid4().hex
            creation_metadata[PURPOSE_METADATA_KEY] = purpose
            environment = dict(self.config.env)
            if purpose == "workspaces":
                control_host = urlsplit(
                    self._sdk_connection_config.get_base_url()
                ).hostname
                if control_host is None:
                    raise ValueError("OpenSandbox control-plane hostname is required")
                environment.update(
                    {
                        "TINKERFIN_WORKSPACES": "1",
                        "TINKERFIN_CONTROL_HOST": control_host,
                        "EXECD_ISOLATION_CONFIG": "/opt/sandbox-runtime/workspaces/isolation.toml",
                    }
                )
            try:
                sandbox = await Sandbox.create(
                    self.config.image,
                    entrypoint=list(self.config.entrypoint),
                    env=environment,
                    metadata=creation_metadata,
                    resource=dict(self.config.resource),
                    volumes=volumes or None,
                    timeout=self.config.ttl,
                    ready_timeout=self.config.ready_timeout,
                    connection_config=self._sdk_connection_config,
                    skip_health_check=purpose == "workspaces",
                    extensions={"bootstrap.execd.isolation": "enable"}
                    if purpose == "workspaces"
                    else None,
                )
                ready_deadline = (
                    asyncio.get_running_loop().time()
                    + self.config.ready_timeout.total_seconds()
                    if purpose == "workspaces"
                    else None
                )
            except Exception as error:
                try:
                    recovered = await self._recover_unknown_create(
                        creation_metadata,
                        cancellation_requested=cancellation_requested,
                        owned_instance=owned_instance,
                    )
                except Exception as recovery_error:  # noqa: BLE001 - SDK recovery boundary
                    select_failure(error, recovery_error)
                    translated = _backend_error("create", error)
                    raise translated from error
                if recovered is None:
                    translated = _backend_error("create", error)
                    raise translated from error
                sandbox, ready_deadline = recovered
            backend = self._wrap(sandbox)
            owned_instance.set()
            try:
                if purpose == "workspaces":
                    if cancellation_requested.is_set():
                        raise asyncio.CancelledError
                    assert ready_deadline is not None
                    await self._wait_workspace_ready(sandbox, deadline=ready_deadline)
                await self._initialize_workspace(sandbox)
                await self._initialize(backend)
            except BaseException as error:
                failure = (
                    OpenSandboxInitializationError(
                        "OpenSandbox workspace initialization failed", cause=error
                    )
                    if isinstance(error, Exception)
                    and not isinstance(error, OpenSandboxError)
                    else error
                )
                await self._close_backend(backend, owned=True, primary=failure)
                raise
            return backend

    async def _recover_unknown_create(
        self,
        creation_metadata: Mapping[str, str],
        *,
        cancellation_requested: asyncio.Event,
        owned_instance: asyncio.Event,
    ) -> tuple[Sandbox, float | None] | None:
        """Recover a possibly created instance through its unique creation token."""
        token = creation_metadata[_CREATE_TOKEN_METADATA_KEY]
        purpose = validate_purpose(creation_metadata[PURPOSE_METADATA_KEY])
        deadline = self._connect_deadline() if purpose == "workspaces" else None
        manager: OpenSandboxSDKManager | None = None
        try:
            async with asyncio.timeout_at(deadline):
                manager = await OpenSandboxSDKManager.create(
                    connection_config=self._sdk_connection_config
                )
                candidates_by_id: dict[str, SandboxInfo] = {}
                page_number = 1
                try:
                    while True:
                        page = await manager.list_sandbox_infos(
                            SandboxFilter(
                                metadata={_CREATE_TOKEN_METADATA_KEY: token},
                                page_size=2,
                                page=page_number,
                            )
                        )
                        for info in page.sandbox_infos:
                            if (
                                info.metadata is not None
                                and info.metadata.get(_CREATE_TOKEN_METADATA_KEY)
                                == token
                            ):
                                require_remote_purpose(info.metadata, purpose)
                                candidates_by_id.setdefault(info.id, info)
                        if not page.pagination.has_next_page:
                            break
                        page_number += 1
                except BaseException as error:
                    for sandbox_id in candidates_by_id:
                        self._pending_destruction.setdefault(
                            sandbox_id,
                            self._cleanup_error("create discovery", sandbox_id, error),
                        )
                    raise
                candidates = list(candidates_by_id.values())
                if len(candidates) == 1:
                    candidate = candidates[0]
                    owned_instance.set()
                    try:
                        if purpose == "workspaces" and cancellation_requested.is_set():
                            raise asyncio.CancelledError
                        sandbox = await Sandbox.connect(
                            candidate.id,
                            connection_config=self._sdk_connection_config,
                            connect_timeout=self.config.connect_timeout,
                            skip_health_check=purpose == "workspaces",
                        )
                        return sandbox, deadline
                    except BaseException as error:
                        try:
                            await self._kill_discovered_candidates(manager, candidates)
                        except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation and exact candidate cleanup
                            raise select_failure(error, cleanup_error)
                        raise
                if len(candidates) > 1:
                    await self._kill_discovered_candidates(manager, candidates)
                return None
        finally:
            if manager is not None:
                try:
                    await manager.close()
                # Cleanup of a temporary query must not mask the creation outcome.
                except Exception:  # noqa: BLE001 - temporary client close is best effort
                    pass

    async def _kill_discovered_candidates(
        self,
        manager: OpenSandboxSDKManager,
        candidates: Sequence[SandboxInfo],
    ) -> None:
        """Delete every exact creation-token candidate and retain cleanup failures."""
        failure: BaseException | None = None
        for candidate in candidates:
            try:
                await manager.kill_sandbox(candidate.id)
            except BaseException as error:  # noqa: BLE001 - every discovered owned candidate still needs cleanup
                if isinstance(error, SandboxApiException) and error.status_code == 404:
                    self._pending_destruction.pop(candidate.id, None)
                    continue
                cleanup_error = self._cleanup_error("destroy", candidate.id, error)
                self._pending_destruction.setdefault(candidate.id, cleanup_error)
                outcome = cleanup_error if isinstance(error, Exception) else error
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
            else:
                self._pending_destruction.pop(candidate.id, None)
        if failure is not None:
            raise failure

    async def _reclaim_backend(self, backend: OpenSandboxBackend) -> None:
        """Reclaim a remote instance whose creation completed after cancellation."""
        await self._close_backend(backend, owned=True)

    async def _reclaim_cancelled_create(
        self,
        creation_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Await a retained creation and take ownership of any returned backend."""
        backend = await creation_task
        await self._reclaim_backend(backend)

    async def _close_cancelled_connect(
        self,
        connection_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Close a late reconnect without destroying the existing remote instance."""
        backend = await connection_task
        await self._close_backend(backend, owned=False)

    def _track_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Retain a background cleanup task until completion."""
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def create(
        self,
        *,
        purpose: OpenSandboxPurpose = "commands",
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        """Create a Sandbox, attach metadata, and run every initializer.

        Caller cancellation stops waiting but does not abandon a possibly created
        remote instance. Internal cleanup takes ownership of any late result. A
        unique reserved create token permits discovery after an unconfirmed SDK
        response; ambiguous candidates are destroyed instead of being adopted.
        Once a workspace instance is known, cancellation stops its preparation
        and waits for that instance's reclamation. Workspace readiness has one
        client-side budget, including response headers, bodies, and intervals.

        Args:
            purpose: Select command execution or isolated project workspaces.
                Workspaces request the provider's isolation capability. This
                choice must match any binding committed for the new instance.
            metadata: Additional internal metadata for this creation. Keys are merged
                over host configuration before reserved ownership fields are added.

        Returns:
            An initialized asynchronous backend owned by the caller.

        Raises:
            OpenSandboxPurposeError: The requested purpose is unknown.
            OpenSandboxBackendError: Creation or initialization fails.
        """
        validate_purpose(purpose)
        with self._sdk_requests.owned_call():
            cancellation_requested = asyncio.Event()
            owned_instance = asyncio.Event()
            creation_task = asyncio.create_task(
                self._create(
                    metadata,
                    purpose,
                    cancellation_requested=cancellation_requested,
                    owned_instance=owned_instance,
                )
            )
            try:
                # Waiting does not cancel the owned operation. Unlike shield on
                # Python 3.14, it does not report a late failure before reclamation
                # can consume it (test_cancelled_native_open_owns_late_initializer_failure).
                await asyncio.wait((creation_task,))
                return creation_task.result()
            except asyncio.CancelledError as cancellation:
                cancellation_requested.set()
                if purpose == "workspaces" and owned_instance.is_set():
                    creation_task.cancel()
                cleanup_task = asyncio.create_task(
                    self._reclaim_cancelled_create(creation_task)
                )
                self._track_cleanup_task(cleanup_task)
                try:
                    await _join_owned_task(
                        cleanup_task, failure_label="Cancelled creation cleanup"
                    )
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve caller cancellation and retained cleanup
                    raise select_failure(cancellation, cleanup_error)
                raise cancellation

    async def _connect(
        self,
        sandbox_id: str,
        purpose: OpenSandboxPurpose,
        *,
        initialize: bool = True,
        cancellation_requested: asyncio.Event,
        connection_acquired: asyncio.Event,
    ) -> OpenSandboxBackend:
        """Bound lookup and initialization while preserving their recovery semantics.

        OpenSandbox 0.1.16 does not include every endpoint request in its connect
        timeout. Both phases share one deadline; initialization failures remain
        distinct so a retry policy cannot repeat a caller's initializer.
        """
        async with self._sdk_requests.operation():
            deadline = self._connect_deadline()
            try:
                async with asyncio.timeout_at(deadline):
                    sandbox = await Sandbox.connect(
                        sandbox_id,
                        connection_config=self._sdk_connection_config,
                        connect_timeout=self.config.connect_timeout,
                        skip_health_check=purpose == "workspaces",
                    )
            except Exception as error:
                translated = _backend_error("connect", error)
                raise translated from error
            backend = self._wrap(sandbox)
            connection_acquired.set()
            try:
                if purpose == "workspaces" and cancellation_requested.is_set():
                    raise asyncio.CancelledError
                async with asyncio.timeout_at(deadline):
                    info = await sandbox.get_info()
                    require_remote_purpose(info.metadata, purpose)
            except BaseException as error:
                failure = (
                    _backend_error("connect", error)
                    if isinstance(error, Exception)
                    else error
                )
                await self._close_backend(backend, owned=False, primary=failure)
                raise
            if not initialize:
                return backend
            try:
                if purpose == "workspaces":
                    await self._wait_workspace_ready(sandbox, deadline=deadline)
                async with asyncio.timeout_at(deadline):
                    await self._initialize_workspace(sandbox)
                    await self._initialize(backend)
            except BaseException as error:
                failure = (
                    OpenSandboxInitializationError(
                        "OpenSandbox initialization failed", cause=error
                    )
                    if isinstance(error, Exception)
                    and not isinstance(error, OpenSandboxError)
                    else error
                )
                await self._close_backend(backend, owned=False, primary=failure)
                raise
            return backend

    async def connect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxBackend:
        """Connect only when remote ownership matches the requested capability.

        Args:
            sandbox_id: Existing remote Sandbox identity.
            purpose: Expected committed purpose, checked before initialization.

        Returns:
            A caller-owned connection without creating a replacement.

        Raises:
            OpenSandboxPurposeError: Reserved purpose metadata is absent or differs.
            OpenSandboxBackendError: Connection or initialization fails.
        """
        validate_purpose(purpose)
        return await self._connect_owned(sandbox_id, purpose, initialize=True)

    async def _connect_observer(self, sandbox_id: str) -> OpenSandboxBackend:
        """Observe a workspace parent without changing its filesystem or lifetime."""
        return await self._connect_owned(sandbox_id, "workspaces", initialize=False)

    async def _connect_owned(
        self, sandbox_id: str, purpose: OpenSandboxPurpose, *, initialize: bool
    ) -> OpenSandboxBackend:
        """Retain acquisition, then cancel preparation without losing late resources."""
        with self._sdk_requests.owned_call():
            cancellation_requested = asyncio.Event()
            connection_acquired = asyncio.Event()
            connection_task = asyncio.create_task(
                self._connect(
                    sandbox_id,
                    purpose,
                    initialize=initialize,
                    cancellation_requested=cancellation_requested,
                    connection_acquired=connection_acquired,
                )
            )
            try:
                # The late-result cleanup owns any failure after caller cancellation;
                # asyncio.wait keeps that task alive without shield's extra error log.
                await asyncio.wait((connection_task,))
                return connection_task.result()
            except asyncio.CancelledError as cancellation:
                cancellation_requested.set()
                if purpose == "workspaces" and connection_acquired.is_set():
                    connection_task.cancel()
                cleanup_task = asyncio.create_task(
                    self._close_cancelled_connect(connection_task)
                )
                self._track_cleanup_task(cleanup_task)
                try:
                    await _join_owned_task(
                        cleanup_task, failure_label="Cancelled connection cleanup"
                    )
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve caller cancellation and retained cleanup
                    raise select_failure(cancellation, cleanup_error)
                raise cancellation

    async def _change_lifecycle(
        self, sandbox_id: str, operation: Literal["pause", "resume"]
    ) -> None:
        """Settle one issued control-plane mutation within its shared deadline."""
        async with self._sdk_requests.operation():
            deadline = (
                asyncio.get_running_loop().time()
                + self._sdk_connection_config.request_timeout.total_seconds()
            )
            shared_deadline = _connection_deadline.get()
            if shared_deadline is not None:
                deadline = min(deadline, shared_deadline)
            manager: OpenSandboxSDKManager | None = None
            try:
                async with asyncio.timeout_at(deadline):
                    manager = await OpenSandboxSDKManager.create(
                        connection_config=self._sdk_connection_config
                    )
                    if operation == "pause":
                        await manager.pause_sandbox(sandbox_id)
                    else:
                        await manager.resume_sandbox(sandbox_id)
            except Exception as error:
                failure = _backend_error(operation, error)
                status = (
                    error.status_code
                    if isinstance(error, SandboxApiException)
                    else None
                )
                # A timeout or server failure cannot prove that the remote state
                # stayed unchanged. Managers must preserve that uncertain state
                # until an explicit inspection resolves it; never replay here.
                outcome = (
                    "rejected"
                    if status in {400, 401, 403, 404, 409, 422}
                    else "unknown"
                )
                translated = type(failure)(
                    failure.message,
                    context={
                        **failure.context,
                        "request_outcome": outcome,
                        "status_code": status,
                    },
                    diagnostic_context=failure.diagnostic_context,
                    cause=error,
                )
                raise translated from error
            finally:
                if manager is not None:
                    await manager.close()

    async def pause(self, sandbox_id: str) -> None:
        """Pause an existing instance through the official control-plane API.

        The request does not reconnect, initialize, replace, or close the remote
        instance. Caller cancellation waits for the issued request to settle before
        propagating. A transport timeout leaves the remote outcome unknown.

        Args:
            sandbox_id: Existing remote Sandbox identity.

        Raises:
            ValueError: The ID is empty.
            OpenSandboxBackendError: The request fails. ``request_outcome`` is
                ``"rejected"`` only for a confirmed client-error response; otherwise
                it is ``"unknown"`` and cannot authorize replay or state rollback.
            asyncio.CancelledError: Cancellation propagates after request settlement.
        """
        if not sandbox_id:
            raise ValueError("sandbox_id must not be empty")
        task = asyncio.create_task(
            self._change_lifecycle(sandbox_id, "pause"),
            name="tinkerfin-sandbox-pause-request",
        )
        self._track_cleanup_task(task)
        await _join_owned_task(task, failure_label="OpenSandbox pause")

    async def resume(self, sandbox_id: str) -> None:
        """Resume an existing paused instance through the official control-plane API.

        This call only submits the state change. Reconnection and readiness belong
        to the manager holding the Sandbox owner claim. It never initializes or
        replaces an instance. Cancellation and unknown outcomes follow :meth:`pause`.

        Args:
            sandbox_id: Existing remote Sandbox identity.

        Raises:
            ValueError: The ID is empty.
            OpenSandboxBackendError: The request fails with the outcome classification
                documented by :meth:`pause`.
            asyncio.CancelledError: Cancellation propagates after request settlement.
        """
        if not sandbox_id:
            raise ValueError("sandbox_id must not be empty")
        task = asyncio.create_task(
            self._change_lifecycle(sandbox_id, "resume"),
            name="tinkerfin-sandbox-resume-request",
        )
        self._track_cleanup_task(task)
        await _join_owned_task(task, failure_label="OpenSandbox resume")

    async def _get_diagnostic_content(
        self,
        sandbox_id: str,
        *,
        kind: Literal["logs", "events"],
        scope: str,
    ) -> OpenSandboxDiagnosticContent:
        async with self._sdk_requests.operation():
            manager: OpenSandboxSDKManager | None = None
            try:
                manager = await OpenSandboxSDKManager.create(
                    connection_config=self._sdk_connection_config
                )
                result = (
                    await manager.get_diagnostic_logs(sandbox_id, scope)
                    if kind == "logs"
                    else await manager.get_diagnostic_events(sandbox_id, scope)
                )
                return OpenSandboxDiagnosticContent.model_validate(
                    {
                        **result.model_dump(by_alias=False),
                        "warnings": tuple(result.warnings or ()),
                    }
                )
            except Exception as error:
                translated = _backend_error(f"diagnostic {kind}", error)
                raise translated from error
            finally:
                if manager is not None:
                    await manager.close()

    async def get_diagnostic_logs(
        self, sandbox_id: str, *, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent:
        """Read provider diagnostic logs without reconnecting or renewing a Sandbox.

        Args:
            sandbox_id: Existing remote Sandbox identity.
            scope: Provider-supported log scope, such as ``"container"`` for Docker.

        Returns:
            Validated inline content or a provider-managed content URL.

        Raises:
            OpenSandboxBackendError: Reading or validating the diagnostic result fails.
        """
        return await self._get_diagnostic_content(sandbox_id, kind="logs", scope=scope)

    async def get_diagnostic_events(
        self, sandbox_id: str, *, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent:
        """Read provider diagnostic events without reconnecting or renewing a Sandbox.

        Args:
            sandbox_id: Existing remote Sandbox identity.
            scope: Provider-supported event scope, such as ``"runtime"`` for Docker.

        Returns:
            Validated inline content or a provider-managed content URL.

        Raises:
            OpenSandboxBackendError: Reading or validating the diagnostic result fails.
        """
        return await self._get_diagnostic_content(
            sandbox_id, kind="events", scope=scope
        )

    async def get_runtime_info(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        """Read control-plane details without endpoint discovery or health checks.

        ``healthy=False`` means that this read did not perform a health probe; it
        does not establish that a running instance is unhealthy. Control-plane
        access remains available while the Sandbox is paused.

        Args:
            sandbox_id: Existing remote Sandbox identity.

        Returns:
            The provider's current runtime details with no data-plane health claim.

        Raises:
            OpenSandboxBackendError: Reading or validating the runtime details fails.
        """
        async with self._sdk_requests.operation():
            manager: OpenSandboxSDKManager | None = None
            try:
                manager = await OpenSandboxSDKManager.create(
                    connection_config=self._sdk_connection_config
                )
                info = await manager.get_sandbox_info(sandbox_id)
                return OpenSandboxRuntimeInfo.from_sdk(info, healthy=False)
            except Exception as error:
                translated = _backend_error("runtime info", error)
                raise translated from error
            finally:
                if manager is not None:
                    await manager.close()

    async def inspect(
        self, sandbox_id: str, *, purpose: OpenSandboxPurpose = "commands"
    ) -> OpenSandboxRuntimeInfo:
        """Inspect an existing Sandbox with verified capability ownership.

        Args:
            sandbox_id: Existing remote Sandbox identity.
            purpose: Expected committed purpose, checked before any health command.

        Returns:
            Current details without initialization or lifecycle mutation.

        Raises:
            OpenSandboxPurposeError: Reserved purpose metadata is absent or differs.
        """
        validate_purpose(purpose)
        async with self._sdk_requests.operation():
            sandbox: Sandbox | None = None
            try:
                sandbox = await Sandbox.connect(
                    sandbox_id,
                    connection_config=self._sdk_connection_config,
                    connect_timeout=self.config.connect_timeout,
                    skip_health_check=True,
                )
                info = await sandbox.get_info()
                require_remote_purpose(info.metadata, purpose)
                backend = self._wrap(sandbox)
                return await backend.aget_runtime_info()
            except OpenSandboxPurposeError:
                raise
            except Exception as exc:  # noqa: BLE001 - inspection returns unavailable details
                return OpenSandboxRuntimeInfo.unavailable(
                    sandbox_id,
                    unavailable_reason(exc),
                )
            finally:
                if sandbox is not None:
                    await self._close_sdk_quietly(sandbox)

    async def _destroy_once(self, sandbox_id: str) -> None:
        """Connect, kill, and close one Sandbox while preserving the kill outcome."""

        async with self._sdk_requests.operation():
            try:
                sandbox = await Sandbox.connect(
                    sandbox_id,
                    connection_config=self._sdk_connection_config,
                    connect_timeout=self.config.connect_timeout,
                    skip_health_check=True,
                )
            except Exception as exc:
                if unavailable_reason(exc) == "not_found":
                    return
                translated = _backend_error("destroy lookup", exc)
                raise translated from exc
            kill_error: Exception | None = None
            try:
                await sandbox.kill()
            except Exception as error:  # noqa: BLE001 - preserve SDK kill failure through close
                # A repeated DELETE can confirm absence after its first response was
                # lost. Only the structured SDK status proves idempotent destruction.
                if unavailable_reason(error) != "not_found":
                    kill_error = error
            finally:
                await self._close_sdk_quietly(sandbox)
            if kill_error is not None:
                error = kill_error
                translated = _backend_error("destroy", error)
                raise translated from error

    async def destroy(self, sandbox_id: str) -> None:
        """Idempotently destroy one remote Sandbox through a retained task.

        Caller cancellation stops no remote work. The method waits for the shared kill
        and local close settlement, then preserves the caller's cancellation signal.
        Concurrent calls for the same ID join one task.

        Args:
            sandbox_id: Canonical remote Sandbox identifier to destroy idempotently.

        Raises:
            asyncio.CancelledError: The caller cancels after retained destruction settles.
            OpenSandboxBackendError: Lookup, kill, or SDK resource settlement fails.
        """

        task = self._destroy_tasks.get(sandbox_id)
        if task is None:
            task = asyncio.create_task(
                self._destroy_once(sandbox_id),
                name=f"tinkerfin-opensandbox-destroy:{sandbox_id}",
            )
            self._destroy_tasks[sandbox_id] = task

            def discard(completed: asyncio.Task[None]) -> None:
                if self._destroy_tasks.get(sandbox_id) is completed:
                    self._destroy_tasks.pop(sandbox_id, None)

            task.add_done_callback(discard)
        await _join_owned_task(task, failure_label="OpenSandbox destruction")

    async def _close_resources(self) -> None:
        """Settle client work before releasing its only owned transport.

        The owning config remains reachable until transport closure succeeds. This
        lets a later close retry if the retained task itself is cancelled, while
        SDK calls continue to borrow only ``_sdk_connection_config``.
        """

        while self._destroy_tasks:
            tasks = tuple(self._destroy_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
        while self._cleanup_tasks:
            tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._sdk_requests.aclose()
        # Cancellation can register late-result reclamation after close started
        # waiting for an active create/connect call. Recheck after SDK scopes and
        # public result handoff have settled, while their transport is still open.
        while self._cleanup_tasks:
            tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._settle_pending_cleanup()
        owned_connection_config = self._owned_connection_config
        if owned_connection_config is None:
            return
        try:
            transport = owned_connection_config.transport
            if transport is None:
                raise RuntimeError("OpenSandbox owned transport is missing")
            # OpenSandbox 0.1.16's ownership helper suppresses ordinary close
            # failures. This client has already proven ownership in
            # ``_scope_sdk_transport``, so it closes the transport directly and keeps
            # the owner reachable until a successful result. The retry contract is
            # guarded by ``test_client_close_reports_an_owned_transport_failure_and_retries``.
            await transport.aclose()
        except Exception as error:
            translated = _backend_error("client close", error)
            raise translated from error
        self._owned_connection_config = None

    async def _settle_pending_cleanup(self) -> None:
        """Attempt each unresolved resource once before releasing the transport.

        This close task owns the direct SDK manager DELETE after ordinary SDK
        scopes have drained. OpenSandbox 0.1.16 ``kill_sandbox`` awaits one request,
        without endpoint discovery or SDK children, so it needs no new operation
        scope while the tracker is closing. A fresh manager avoids relying on a
        Sandbox connection whose local close already succeeded. Failed identities
        and local connections remain reachable for a later explicit close only.
        """
        failure: BaseException | None = None
        if self._pending_destruction:
            try:
                manager = await OpenSandboxSDKManager.create(
                    connection_config=self._sdk_connection_config
                )
            except Exception as error:
                raise _backend_error("cleanup open", error) from error
            try:
                for sandbox_id, previous in tuple(self._pending_destruction.items()):
                    try:
                        await manager.kill_sandbox(sandbox_id)
                    except BaseException as error:  # noqa: BLE001 - retain every unresolved owned identity
                        if (
                            isinstance(error, SandboxApiException)
                            and error.status_code == 404
                        ):
                            self._pending_destruction.pop(sandbox_id)
                            continue
                        outcome = (
                            self._cleanup_error("destroy", sandbox_id, error)
                            if isinstance(error, Exception)
                            else error
                        )
                        outcome = select_failure(outcome, previous)
                        failure = (
                            outcome
                            if failure is None
                            else select_failure(failure, outcome)
                        )
                    else:
                        self._pending_destruction.pop(sandbox_id)
            finally:
                try:
                    await manager.close()
                except BaseException as error:  # noqa: BLE001 - local settlement must retain destruction failures
                    outcome = (
                        _backend_error("cleanup close", error)
                        if isinstance(error, Exception)
                        else error
                    )
                    failure = (
                        outcome if failure is None else select_failure(failure, outcome)
                    )
        for backend, previous in tuple(self._pending_closes.items()):
            try:
                await backend.aclose()
            except BaseException as error:  # noqa: BLE001 - retain failed local connections without destroying borrowed instances
                outcome = (
                    self._cleanup_error("close", backend.id, error)
                    if isinstance(error, Exception)
                    else error
                )
                outcome = select_failure(outcome, previous)
                failure = (
                    outcome if failure is None else select_failure(failure, outcome)
                )
            else:
                self._pending_closes.pop(backend)
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        """Finish client work and close only the asynchronous transport it owns.

        Concurrent callers join one retained close task. Cancelling a caller does
        not cancel resource settlement; cancellation propagates after that shared
        task completes. If the close task itself fails or is cancelled, the owned
        transport remains available for a later retry. Failed reclamation keeps
        its exact owned remote identity and local connection until this method
        confirms cleanup. Each call attempts every unresolved resource once;
        borrowed remote instances are never destroyed. Keep a caller-supplied
        transport open until this method succeeds.

        Raises:
            asyncio.CancelledError: The caller is cancelled after settlement, or the
                retained close task is cancelled independently.
            OpenSandboxBackendError: Owned destruction, local connection closure,
                or owned transport closure fails.
        """

        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_resources(),
                name="tinkerfin-opensandbox-client-close",
            )
            self._close_task = close_task
        try:
            await _join_owned_task(
                close_task,
                failure_label="OpenSandbox client close",
            )
        finally:
            # A finished task no longer owns work. The transport owner itself is
            # cleared only by a successful settlement, so failed closure can retry.
            if close_task.done() and self._close_task is close_task:
                self._close_task = None
