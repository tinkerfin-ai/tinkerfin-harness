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

from opensandbox import Sandbox
from opensandbox import SandboxManager as OpenSandboxSDKManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException, SandboxReadyTimeoutException
from opensandbox.models import WriteEntry
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import SandboxFilter, SandboxInfo
from opensandbox.transport import RetryPolicy

from ..backends.sdk import (
    OpenSandboxBackend,
    _connection_failure_reason,
    unavailable_reason,
)
from ..errors import (
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
from ._transport import _join_owned_task, _SDKRequestTracker

_CREATE_TOKEN_METADATA_KEY = "tinkerfin.ai/create-token"
# Manager recovery scopes this deadline to its same-instance work. Child SDK tasks
# inherit it so cancellation can settle under the owner claim without extending
# connection or initialization work to the client's longer standalone deadline.
_connection_deadline: ContextVar[float | None] = ContextVar(
    "tinkerfin_sandbox_connection_deadline", default=None
)


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

    async def _workspace_health(self, sandbox: Sandbox) -> bool:
        """Wait for the trusted proxy's explicit readiness, not merely execd's ping."""
        try:
            result = await sandbox.commands.run(
                "/opt/sandbox-runtime/venv/bin/python -I -S "
                "/opt/sandbox-runtime/workspaces/network.py status",
                opts=RunCommandOpts(timeout=timedelta(seconds=30)),
            )
        except SandboxApiException:
            return False
        return (
            result.error is None
            and "".join(message.text for message in result.logs.stdout).strip()
            == '{"ready":true}'
        )

    @staticmethod
    async def _close_quietly(
        backend: OpenSandboxBackend,
    ) -> None:
        try:
            await backend.aclose()
        except Exception:  # noqa: BLE001 - local close is best effort
            pass

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
                    health_check=self._workspace_health
                    if purpose == "workspaces"
                    else None,
                    extensions={"bootstrap.execd.isolation": "enable"}
                    if purpose == "workspaces"
                    else None,
                )
            except Exception as error:
                try:
                    sandbox = await self._recover_unknown_create(creation_metadata)
                except Exception as recovery_error:  # noqa: BLE001 - SDK recovery boundary
                    error.add_note(
                        "OpenSandbox create recovery also failed: "
                        f"{type(recovery_error).__name__}"
                    )
                    translated = _backend_error("create", error)
                    raise translated from error
                if sandbox is None:
                    translated = _backend_error("create", error)
                    raise translated from error
            backend = self._wrap(sandbox)
            try:
                await self._initialize_workspace(sandbox)
            except Exception as error:
                try:
                    await backend.akill()
                except Exception:  # noqa: BLE001 - preserve initialization failure
                    pass
                await self._close_quietly(backend)
                translated = OpenSandboxInitializationError(
                    "OpenSandbox workspace initialization failed", cause=error
                )
                raise translated from error
            try:
                await self._initialize(backend)
            except BaseException:
                try:
                    await backend.akill()
                except Exception:  # noqa: BLE001 - preserve initializer failure
                    pass
                await self._close_quietly(backend)
                raise
            return backend

    async def _recover_unknown_create(
        self,
        creation_metadata: Mapping[str, str],
    ) -> Sandbox | None:
        """Recover a possibly created instance through its unique creation token."""
        token = creation_metadata[_CREATE_TOKEN_METADATA_KEY]
        purpose = validate_purpose(creation_metadata[PURPOSE_METADATA_KEY])
        manager: OpenSandboxSDKManager | None = None
        try:
            manager = await OpenSandboxSDKManager.create(
                connection_config=self._sdk_connection_config
            )
            candidates_by_id: dict[str, SandboxInfo] = {}
            page_number = 1
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
                        and info.metadata.get(_CREATE_TOKEN_METADATA_KEY) == token
                    ):
                        require_remote_purpose(info.metadata, purpose)
                        candidates_by_id.setdefault(info.id, info)
                if not page.pagination.has_next_page:
                    break
                page_number += 1
            candidates = list(candidates_by_id.values())
            if len(candidates) == 1:
                candidate = candidates[0]
                try:
                    return await Sandbox.connect(
                        candidate.id,
                        connection_config=self._sdk_connection_config,
                        connect_timeout=self.config.connect_timeout,
                        health_check=self._workspace_health
                        if purpose == "workspaces"
                        else None,
                    )
                except Exception:  # noqa: BLE001 - unknown create result is reclaimed
                    await self._kill_discovered_candidates(manager, candidates)
                    return None
            if len(candidates) > 1:
                await self._kill_discovered_candidates(manager, candidates)
            return None
        except Exception:  # noqa: BLE001 - recovery failure preserves create outcome
            return None
        finally:
            if manager is not None:
                try:
                    await manager.close()
                # Cleanup of a temporary query must not mask the creation outcome.
                except Exception:  # noqa: BLE001 - temporary client close is best effort
                    pass

    @staticmethod
    async def _kill_discovered_candidates(
        manager: OpenSandboxSDKManager,
        candidates: Sequence[SandboxInfo],
    ) -> None:
        """Best-effort delete creation-token candidates that cannot be adopted."""
        for candidate in candidates:
            try:
                await manager.kill_sandbox(candidate.id)
            except Exception:  # noqa: BLE001 - candidate cleanup is best effort
                pass

    async def _reclaim_backend(self, backend: OpenSandboxBackend) -> None:
        """Reclaim a remote instance whose creation completed after cancellation."""
        try:
            await backend.akill()
        except Exception:  # noqa: BLE001 - cancelled creation cleanup is best effort
            pass
        finally:
            await self._close_quietly(backend)

    async def _reclaim_cancelled_create(
        self,
        creation_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Await a retained creation and take ownership of any returned backend."""
        try:
            backend = await creation_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # The original creation path cleans up SDK and initializer failures.
            return
        await self._reclaim_backend(backend)

    async def _close_cancelled_connect(
        self,
        connection_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Close a late reconnect without destroying the existing remote instance."""
        try:
            backend = await connection_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # Failed SDK and initializer paths already close their own connection.
            return
        await self._close_quietly(backend)

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
            creation_task = asyncio.create_task(self._create(metadata, purpose))
            try:
                # Waiting does not cancel the owned operation. Unlike shield on
                # Python 3.14, it does not report a late failure before reclamation
                # can consume it (test_cancelled_native_open_owns_late_initializer_failure).
                await asyncio.wait((creation_task,))
                return creation_task.result()
            except asyncio.CancelledError as cancellation:
                cleanup_task = asyncio.create_task(
                    self._reclaim_cancelled_create(creation_task)
                )
                self._track_cleanup_task(cleanup_task)
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # Repeated cancellation affects only the waiter; cleanup stays retained.
                    pass
                raise cancellation

    async def _connect(
        self,
        sandbox_id: str,
        purpose: OpenSandboxPurpose,
        *,
        initialize: bool = True,
    ) -> OpenSandboxBackend:
        """Bound lookup and initialization while preserving their recovery semantics.

        OpenSandbox 0.1.16 does not include every endpoint request in its connect
        timeout. Both phases share one deadline; initialization failures remain
        distinct so a retry policy cannot repeat a caller's initializer.
        """
        async with self._sdk_requests.operation():
            deadline = (
                asyncio.get_running_loop().time()
                + self.config.connect_timeout.total_seconds()
            )
            recovery_deadline = _connection_deadline.get()
            if recovery_deadline is not None:
                deadline = min(deadline, recovery_deadline)
            try:
                async with asyncio.timeout_at(deadline):
                    sandbox = await Sandbox.connect(
                        sandbox_id,
                        connection_config=self._sdk_connection_config,
                        connect_timeout=self.config.connect_timeout,
                    )
            except Exception as error:
                translated = _backend_error("connect", error)
                raise translated from error
            backend = self._wrap(sandbox)
            try:
                async with asyncio.timeout_at(deadline):
                    info = await sandbox.get_info()
                    require_remote_purpose(info.metadata, purpose)
            except BaseException as error:
                await self._close_quietly(backend)
                if not isinstance(error, Exception) or isinstance(
                    error, OpenSandboxError
                ):
                    raise
                translated = _backend_error("connect", error)
                raise translated from error
            if not initialize:
                return backend
            try:
                async with asyncio.timeout_at(deadline):
                    await self._initialize_workspace(sandbox)
                    await self._initialize(backend)
            except BaseException as error:
                await self._close_quietly(backend)
                if not isinstance(error, Exception) or isinstance(
                    error, OpenSandboxInitializationError
                ):
                    raise
                raise OpenSandboxInitializationError(
                    "OpenSandbox initialization failed", cause=error
                ) from error
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
        """Retain connection ownership until delivery or cancelled-call settlement."""
        with self._sdk_requests.owned_call():
            connection_task = asyncio.create_task(
                self._connect(sandbox_id, purpose, initialize=initialize)
            )
            try:
                # The late-result cleanup owns any failure after caller cancellation;
                # asyncio.wait keeps that task alive without shield's extra error log.
                await asyncio.wait((connection_task,))
                return connection_task.result()
            except asyncio.CancelledError as cancellation:
                cleanup_task = asyncio.create_task(
                    self._close_cancelled_connect(connection_task)
                )
                self._track_cleanup_task(cleanup_task)
                try:
                    await _join_owned_task(
                        cleanup_task, failure_label="Cancelled connection cleanup"
                    )
                except asyncio.CancelledError:
                    # Even repeated cancellation must settle initialization before a
                    # manager can release its owner claim to the next operation.
                    pass
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

    async def aclose(self) -> None:
        """Finish client work and close only the asynchronous transport it owns.

        Concurrent callers join one retained close task. Cancelling a caller does
        not cancel resource settlement; cancellation propagates after that shared
        task completes. If the close task itself fails or is cancelled, the owned
        transport remains available for a later retry.

        Raises:
            asyncio.CancelledError: The caller is cancelled after settlement, or the
                retained close task is cancelled independently.
            OpenSandboxBackendError: Owned transport closure fails.
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
