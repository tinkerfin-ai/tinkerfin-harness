"""Manage stable OpenSandbox backends, warm capacity, and remote cleanup.

The manager owns every local connection it opens. Its State determines remote
shutdown behavior: process-local resources are destroyed, while persistent bindings,
shared warm slots, and cleanup work remain available to another worker.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Generic, Self

from deepagents.backends.protocol import BackendProtocol
from typing_extensions import TypeVar

from tinkerfin_contracts import Workspace

from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxManagerClosedError,
    OpenSandboxSettlementTimeoutError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    OpenSandboxWarmPoolUnavailableError,
)
from ..models import OpenSandboxDetails, OpenSandboxDiagnosticContent
from . import _manager_bindings, _manager_resources
from ._identity import SandboxResourceIdentity
from ._manager_availability import _SandboxAvailability
from ._manager_bindings import _BindingResolution
from ._manager_resources import (
    _BackendAcquisition,
    _HealthBackend,
    _ManagedBackend,
)
from ._notifications import _LifecycleNotifications
from ._protocols import _SandboxClient, _SandboxClientBoundary
from .notifications import OpenSandboxLifecycleObserver, OpenSandboxNotificationOptions
from .recovery import OpenSandboxRecoveryPolicy
from .state import (
    InMemoryOpenSandboxState,
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxReadyWarmClaim,
    OpenSandboxState,
    OpenSandboxWarmClaim,
    _OpenSandboxStateBoundary,
)

KeyT = TypeVar("KeyT", default=str)


class OpenSandboxManager(Generic[KeyT]):
    """Manage stable OpenSandbox backends for caller-defined ownership keys.

    The State serializes transitions for one owner and fences stale workers while
    allowing unrelated owners to proceed concurrently. A cached
    ``OpenSandboxHandle`` retains its object identity when its remote backend is
    replaced, so existing callers continue using the current Sandbox.

    Cancellation cannot abandon a Sandbox after it leaves the warm pool or client.
    Explicit destruction is strict and retryable; cleanup after a successful
    replacement is durable when the configured State is persistent.

    Optional observers receive confirmed lifecycle changes without participating in
    resource decisions. Reentering this manager from an observer raises
    ``OpenSandboxObserverReentryError``. The manager drains owned delivery tasks on
    close but never closes the borrowed observers themselves.
    """

    def __init__(
        self,
        *,
        client: _SandboxClient,
        key_resolver: Callable[[KeyT], str] | None = None,
        state: OpenSandboxState | None = None,
        warm_pool_size: int | None = None,
        fail_on_startup_warmup_error: bool = False,
        settlement_timeout: float | None = None,
        recovery_policy: OpenSandboxRecoveryPolicy | None = None,
        observers: Sequence[OpenSandboxLifecycleObserver] = (),
        notification_options: OpenSandboxNotificationOptions | None = None,
    ) -> None:
        """Configure the manager without opening resources or creating a Sandbox.

        Args:
            client: Asynchronous creation and lifecycle client owned and closed by
                this manager.
            key_resolver: Convert an opaque application key into a stable non-blank
                owner key used by lifecycle State. Omit it for string keys.
            state: Atomic allocation, warm-pool, and cleanup state owned and closed
                by this manager. Omit it to use process-local memory.
            warm_pool_size: Target number of unbound ready Sandboxes. ``None`` uses
                the client configuration.
            fail_on_startup_warmup_error: Whether one failed startup warm creation
                prevents the manager from opening.
            settlement_timeout: Optional per-caller close wait in seconds. ``None``
                waits for complete cleanup. A finite timeout never cancels the shared
                close task.
            recovery_policy: Limits for existing-instance connection and health
                recovery. Omit it to retry briefly, then preserve the binding and
                report failure. Recreation requires an explicit policy choice.
            observers: Borrowed asynchronous observers of confirmed lifecycle changes.
                Each receives events in order through an independent bounded queue.
                Observers must not reenter this manager's resource operations or close.
            notification_options: Per-observer queue capacity and execution timeout.
                Omit it for 128 pending events and one second per callback.

        Raises:
            TypeError: ``settlement_timeout`` is not numeric or is a boolean.
            ValueError: A capacity is negative, or the timeout is negative or non-finite.
        """
        resolved_warm_size = (
            client.config.warm_pool_size if warm_pool_size is None else warm_pool_size
        )
        if isinstance(resolved_warm_size, bool) or not isinstance(
            resolved_warm_size, int
        ):
            raise TypeError("warm_pool_size must be an integer or None")
        if resolved_warm_size < 0:
            raise ValueError("warm_pool_size must not be negative")
        if key_resolver is not None and not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        if recovery_policy is not None and not isinstance(
            recovery_policy, OpenSandboxRecoveryPolicy
        ):
            raise TypeError(
                "recovery_policy must be an OpenSandboxRecoveryPolicy or None"
            )
        if notification_options is not None and not isinstance(
            notification_options, OpenSandboxNotificationOptions
        ):
            raise TypeError(
                "notification_options must be an OpenSandboxNotificationOptions or None"
            )
        resolved_observers = tuple(observers)
        for observer in resolved_observers:
            if not callable(getattr(observer, "on_sandbox_event", None)):
                raise TypeError("Each observer must provide on_sandbox_event")
        if len({id(observer) for observer in resolved_observers}) != len(
            resolved_observers
        ):
            raise ValueError("observers must not contain the same observer twice")
        if settlement_timeout is None:
            resolved_timeout = None
        else:
            if isinstance(settlement_timeout, bool) or not isinstance(
                settlement_timeout,
                int | float,
            ):
                raise TypeError("settlement_timeout must be a number or None")
            resolved_timeout = float(settlement_timeout)
            if not math.isfinite(resolved_timeout) or resolved_timeout < 0:
                raise ValueError("settlement_timeout must be finite and non-negative")

        self._client = _SandboxClientBoundary(client)
        self._key_resolver = key_resolver
        self._state = _OpenSandboxStateBoundary(state or InMemoryOpenSandboxState())
        self._warm_pool_size = resolved_warm_size
        self._fail_on_startup_warmup_error = fail_on_startup_warmup_error
        self._settlement_timeout = resolved_timeout
        self._recovery_policy = recovery_policy or OpenSandboxRecoveryPolicy()
        self._notifications = _LifecycleNotifications(
            resolved_observers, notification_options or OpenSandboxNotificationOptions()
        )
        self._handles: dict[str, OpenSandboxHandle] = {}
        self._backend_views: dict[str, tuple[OpenSandboxHandle, _ManagedBackend]] = {}
        self._warm_backends: list[OpenSandboxBackend] = []
        self._warm_lock = asyncio.Lock()
        self._warm_fill_lock = asyncio.Lock()
        self._warm_maintenance_task: asyncio.Task[None] | None = None
        self._warm_maintenance_wakeup = asyncio.Event()
        self._warm_ready = asyncio.Event()
        # Persistent publication alone cannot certify this manager's startup. Each
        # slot must be verified or created here once; progress across maintenance
        # rounds is bounded by the configured capacity and survives peer claims.
        self._initial_warm_verified: set[int] = set()
        self._startup_complete = False
        self._warm_failure: BaseException | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_wakeup = asyncio.Event()
        self._cleanup_queue_task: asyncio.Task[None] | None = None
        self._pending_destroy_ids: dict[str, set[str]] = {}
        self._state_lock = asyncio.Lock()
        self._active_operations = 0
        self._operations_done = asyncio.Event()
        self._operations_done.set()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._availability = _SandboxAvailability(self)

    def _resolve_resource_key(self, key: KeyT, namespace: str | None) -> str:
        """Bind namespace before every cache, State, claim, or availability lookup."""
        self._notifications.check_reentry()
        if self._key_resolver is None:
            if not isinstance(key, str):
                raise TypeError("non-string Sandbox keys require key_resolver")
            owner_key = key
        else:
            owner_key = self._key_resolver(key)
        return SandboxResourceIdentity(namespace, owner_key).canonical_key()

    async def __aenter__(self) -> Self:
        """Open the State and warm pool, then return this owned resource."""
        try:
            await self.start()
        except BaseException as startup_error:  # noqa: BLE001 - preserve startup outcome
            try:
                await self.aclose()
            except BaseException as cleanup_error:
                current = asyncio.current_task()
                if isinstance(cleanup_error, asyncio.CancelledError) and (
                    current is not None and current.cancelling()
                ):
                    cleanup_error.add_note(
                        "OpenSandbox startup also failed: "
                        f"{type(startup_error).__name__}: {startup_error}"
                    )
                    raise
                startup_error.add_note(
                    "OpenSandbox startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise startup_error.with_traceback(startup_error.__traceback__)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close all resources according to the configured State's durability."""
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            current = asyncio.current_task()
            if isinstance(cleanup_error, asyncio.CancelledError) and (
                current is not None and current.cancelling()
            ):
                cleanup_error.add_note(
                    f"OpenSandbox context body also failed: {type(exc).__name__}: {exc}"
                )
                raise
            exc.add_note(
                "OpenSandbox context cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def start(self) -> None:
        """Idempotently open the State and complete initial warm-pool filling.

        Concurrent callers await the same startup task. A warm creation failure is
        logged and does not disable on-demand creation unless strict startup warmup
        was configured.

        Raises:
            OpenSandboxManagerClosedError: The manager has begun closing.
            OpenSandboxStateError: The allocation State cannot start safely.
            OpenSandboxWarmPoolUnavailableError: Strict warmup could not verify
                initial capacity, including slots currently held by another worker.
        """
        self._notifications.check_reentry()
        async with self._state_lock:
            if self._closed:
                raise OpenSandboxManagerClosedError("OpenSandbox manager is closed")
            start_task = self._start_task
            if start_task is None:
                start_task = asyncio.create_task(self._start_resources())
                self._start_task = start_task
        await asyncio.shield(start_task)

    async def _start_resources(self) -> None:
        """Initialize authoritative State before starting dependent shared tasks."""
        await self._state.start(warm_pool_size=self._warm_pool_size)
        self._started = True
        retry_cleanup = await self._drain_cleanup_queue()
        self._cleanup_queue_task = asyncio.create_task(
            self._cleanup_queue_loop(retry_pending=retry_cleanup),
            name="tinkerfin-opensandbox-cleanup",
        )
        await self._maintain_warm_pool(fail_on_error=self._fail_on_startup_warmup_error)
        self._warm_maintenance_task = asyncio.create_task(
            self._warm_maintenance_loop(),
            name="tinkerfin-opensandbox-warm-maintenance",
        )
        self._startup_complete = True

    def _ensure_open(self) -> None:
        self._notifications.check_reentry()
        if self._closed:
            raise OpenSandboxManagerClosedError("OpenSandbox manager is closed")

    async def check_ready(self) -> None:
        """Raise when the configured ready Sandbox capacity is unavailable.

        Hosts can use this check in their readiness endpoint. It
        reads shared capacity and reports startup or background warm-pool failures.
        Routine verification retains published capacity until a failure is known;
        consumption removes capacity immediately. Provider responses, credentials,
        and remote identifiers are never included in the public failure.

        Raises:
            OpenSandboxManagerClosedError: The manager is not available for work.
            OpenSandboxWarmPoolUnavailableError: The target warm capacity is not
                currently backed by verified remote Sandboxes.
        """

        self._ensure_open()
        if not self._startup_complete:
            raise OpenSandboxWarmPoolUnavailableError(
                "OpenSandbox warm capacity has not started",
                context={"target_capacity": self._warm_pool_size},
            )
        if self._warm_pool_size == 0:
            return
        async with self._operation():
            ready = self._warm_failure is None and await self._state.warm_pool_ready()
            self._notifications.warm_capacity(ready=ready)
            if ready:
                return
        failure = self._warm_failure
        raise OpenSandboxWarmPoolUnavailableError(
            "OpenSandbox warm capacity is unavailable",
            context={"target_capacity": self._warm_pool_size},
            diagnostic_context={
                "error_type": (
                    type(failure).__name__ if failure is not None else "not_ready"
                )
            },
            cause=failure,
        ) from failure

    @asynccontextmanager
    async def _operation(self) -> AsyncGenerator[None]:
        """Register a public operation so closure waits for its complete exit."""
        async with self._state_lock:
            self._ensure_open()
            self._active_operations += 1
            self._operations_done.clear()
        try:
            yield
        finally:
            async with self._state_lock:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._operations_done.set()

    @asynccontextmanager
    async def _renew_owner_claim(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> AsyncGenerator[None]:
        """Renew an owner claim and stop the operation immediately if ownership is lost."""
        interval = self._state.lease_renew_interval
        if interval is None:
            yield
            return

        operation_task = asyncio.current_task()
        if operation_task is None:
            raise RuntimeError("OpenSandbox owner claims require an asyncio task")
        closing = False
        renewal_failure: Exception | None = None

        async def renew() -> None:
            nonlocal renewal_failure
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self._state.renew_owner(claim)
                except asyncio.CancelledError:
                    raise
                # State is host-replaceable, so any renewal failure makes claim
                # ownership unverifiable.
                except Exception as exc:  # noqa: BLE001
                    if closing:
                        return
                    renewal_failure = exc
                    operation_task.cancel()
                    return
                if renewed:
                    continue
                if closing:
                    return
                renewal_failure = OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} was lost during the operation"
                )
                operation_task.cancel()
                return

        renewal_task = asyncio.create_task(
            renew(),
            name=f"tinkerfin-opensandbox-owner-lease:{claim.owner_digest}",
        )
        try:
            try:
                yield
            finally:
                closing = True
                if not renewal_task.done():
                    renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
        except asyncio.CancelledError as cancellation:
            if renewal_failure is None:
                raise cancellation
            if isinstance(renewal_failure, OpenSandboxStateError):
                raise renewal_failure
            raise OpenSandboxStateError(
                f"Renewing owner claim for {claim.owner_key!r} failed"
            ) from renewal_failure

    @asynccontextmanager
    async def _claim_owner(
        self,
        owner_key: str,
    ) -> AsyncGenerator[OpenSandboxOwnerClaim]:
        """Acquire the unique fencing claim for one owner from State."""
        claim = await self._state.acquire_owner(owner_key)
        try:
            async with self._renew_owner_claim(claim):
                yield claim
        finally:
            release_task = asyncio.create_task(
                self._state.release_owner(claim),
                name=f"tinkerfin-opensandbox-owner-release:{claim.owner_digest}",
            )
            await self._await_claim_release(release_task)

    async def _create_for_warm_claim(
        self,
        claim: OpenSandboxWarmClaim,
    ) -> OpenSandboxBackend:
        """Create a remote instance while renewing its warm claim."""

        return await _manager_resources._create_for_warm_claim(
            self,
            claim,
        )

    async def _is_backend_healthy(self, backend: _HealthBackend) -> bool:
        """Treat nonzero health results and all probe failures as unhealthy."""

        return await _manager_resources._is_backend_healthy(
            self,
            backend,
        )

    async def _renew_backend(self, backend: _HealthBackend) -> None:
        """Best-effort finite expiry renewal that preserves healthy owner handles."""

        return await _manager_resources._renew_backend(
            self,
            backend,
        )

    async def _renew_ready_backend(self, backend: _HealthBackend) -> None:
        """Renew finite warm expiry strictly; manual-cleanup instances need no renewal."""

        return await _manager_resources._renew_ready_backend(
            self,
            backend,
        )

    async def _close_backend(self, backend: OpenSandboxBackend) -> None:
        """Best-effort local closure that does not mask the primary result."""

        return await _manager_resources._close_backend(
            backend,
        )

    async def _close_handle(self, handle: OpenSandboxHandle) -> None:
        """Retire a handle and close its local backend through the manager."""

        return await _manager_resources._close_handle(
            handle,
        )

    def _track_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Retain a cleanup task until completion so ``aclose`` can await it."""

        return _manager_resources._track_cleanup_task(
            self,
            task,
        )

    def _start_backend_cleanup(
        self,
        backend: OpenSandboxBackend,
        *,
        destroy: bool,
    ) -> asyncio.Task[None]:
        """Create a managed cleanup task and transfer backend ownership immediately."""

        return _manager_resources._start_backend_cleanup(
            self,
            backend,
            destroy=destroy,
        )

    async def _cleanup_owned_backend(
        self,
        backend: OpenSandboxBackend,
        *,
        destroy: bool,
    ) -> None:
        """Shield cleanup so a currently owned backend is always disposed."""

        return await _manager_resources._cleanup_owned_backend(
            self,
            backend,
            destroy=destroy,
        )

    async def _cleanup_after_health_check(
        self,
        backend: OpenSandboxBackend,
        health_task: asyncio.Task[bool],
        *,
        destroy: bool,
    ) -> None:
        """Settle a shielded native health probe before disposing its backend."""

        return await _manager_resources._cleanup_after_health_check(
            self,
            backend,
            health_task,
            destroy=destroy,
        )

    async def _check_owned_backend(
        self,
        backend: OpenSandboxBackend,
        *,
        destroy_on_cancel: bool,
    ) -> bool:
        """Check an unpublished backend while retaining cleanup on cancellation.

        ``destroy_on_cancel`` is true only while no authoritative State binding can
        reference the remote instance. A recovered or consumed-warm binding owns only
        this worker's local connection and must preserve the remote Sandbox.
        """

        return await _manager_resources._check_owned_backend(
            self,
            backend,
            destroy_on_cancel=destroy_on_cancel,
        )

    async def _wait_for_cleanup_tasks(self) -> None:
        """Wait for current cleanup tasks and any tasks they derive.

        Removing each completed snapshot explicitly avoids a busy loop when task done
        callbacks have not yet removed those same tasks from the retained set.
        """

        return await _manager_resources._wait_for_cleanup_tasks(
            self,
        )

    async def _destroy_remote(
        self,
        sandbox_id: str,
        *,
        strict: bool = False,
    ) -> None:
        """Choose strict destruction or best-effort reclamation by call site.

        ``delete`` uses strict mode so failed bindings remain retryable. Replacement
        and warm-pool shutdown use best effort so stale cleanup cannot block a new
        handle or process closure.
        """

        return await _manager_resources._destroy_remote(
            self,
            sandbox_id,
            strict=strict,
        )

    async def _drain_cleanup_queue(self) -> bool:
        """Consume claimable orphan work and report whether retry needs backoff."""

        return await _manager_resources._drain_cleanup_queue(
            self,
        )

    async def _release_cleanup_claim(
        self,
        claim: OpenSandboxCleanupClaim,
    ) -> None:
        """Settle cleanup-claim release before propagating caller cancellation."""

        return await _manager_resources._release_cleanup_claim(
            self,
            claim,
        )

    @staticmethod
    async def _await_claim_release(release_task: asyncio.Task[None]) -> None:
        """Settle State release before propagating repeated caller cancellation."""

        return await _manager_resources._await_claim_release(
            release_task,
        )

    async def _destroy_for_cleanup_claim(
        self,
        claim: OpenSandboxCleanupClaim,
    ) -> None:
        """Destroy an orphan resource while renewing its cleanup claim."""

        return await _manager_resources._destroy_for_cleanup_claim(
            self,
            claim,
        )

    async def _cleanup_queue_loop(self, *, retry_pending: bool) -> None:
        """Process durable cleanup left by this process and other workers."""

        return await _manager_resources._cleanup_queue_loop(
            self,
            retry_pending=retry_pending,
        )

    async def _dispose_backend(
        self,
        backend: OpenSandboxBackend,
        *,
        strict: bool = False,
    ) -> None:
        """Attempt remote destruction before unconditionally closing locally."""

        return await _manager_resources._dispose_backend(
            self,
            backend,
            strict=strict,
        )

    async def _fill_warm_pool(self) -> tuple[str, ...]:
        """Claim and fill every currently empty global warm slot."""

        return await _manager_resources._fill_warm_pool(
            self,
        )

    async def _reconcile_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> str | None:
        """Verify one published remote Sandbox or invalidate its fenced slot."""

        return await _manager_resources._reconcile_ready_warm_slot(self, claim)

    async def _maintain_warm_pool(self, *, fail_on_error: bool) -> None:
        """Reconcile published slots, fill capacity, and update host readiness."""

        return await _manager_resources._maintain_warm_pool(
            self,
            fail_on_error=fail_on_error,
        )

    async def _warm_maintenance_loop(self) -> None:
        """Check warm health, renew finite expiry, and restore capacity until close."""

        return await _manager_resources._warm_maintenance_loop(self)

    def _schedule_replenish(self) -> None:
        """Schedule at most one replenishment task for an open undersized pool."""

        return _manager_resources._schedule_replenish(
            self,
        )

    async def _take_local_warm_backend(
        self,
        sandbox_id: str,
    ) -> OpenSandboxBackend | None:
        """Take the local backend retained for one shared warm slot."""

        return await _manager_resources._take_local_warm_backend(
            self,
            sandbox_id,
        )

    async def _acquire_backend(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> _BackendAcquisition:
        """Acquire a healthy candidate backend uniquely owned by the caller.

        State first transfers a global warm slot atomically to the owner. Instances
        created by this process reuse their local backend; instances created by other
        workers reconnect by remote ID. Unusable remote IDs are reclaimed only after
        a healthy candidate becomes authoritative.

        Returns:
            Candidate backend, optional committed binding, warm-slot fact, and remote
            IDs that become reclaimable only after this candidate is authoritative.
        """

        return await _manager_resources._acquire_backend(
            self,
            claim,
        )

    async def _reconcile_candidate_binding(
        self,
        *,
        owner_key: str,
        expected: OpenSandboxBinding,
        primary_error: BaseException,
    ) -> _BindingResolution:
        """Classify a failed bind from one authoritative State read."""

        return await _manager_bindings._reconcile_candidate_binding(
            self,
            owner_key=owner_key,
            expected=expected,
            primary_error=primary_error,
        )

    async def _bind_on_demand_backend(
        self,
        *,
        owner_key: str,
        claim: OpenSandboxOwnerClaim,
        backend: OpenSandboxBackend,
    ) -> OpenSandboxBinding:
        """Commit or reconcile one candidate before deciding its cleanup ownership."""

        return await _manager_bindings._bind_on_demand_backend(
            self,
            owner_key=owner_key,
            claim=claim,
            backend=backend,
        )

    async def _replace(
        self,
        owner_key: str,
        claim: OpenSandboxOwnerClaim,
        existing_handle: OpenSandboxHandle | None,
        *,
        old_id: str | None,
    ) -> OpenSandboxHandle:
        """Commit a new binding and backend before safely reclaiming the old instance.

        External binding commits before in-memory publication so crash recovery cannot
        restore an old ID. A consumed warm binding is already authoritative; an
        uncertain on-demand bind is reconciled before its local connection is closed or
        its remote Sandbox is destroyed. After publication, stable handle identity is
        preserved while old leases drain; cancellation retains cleanup.
        """

        return await _manager_bindings._replace(
            self,
            owner_key,
            claim,
            existing_handle,
            old_id=old_id,
        )

    async def _retire_replaced_backend(
        self,
        handle: OpenSandboxHandle,
        backend: OpenSandboxBackend,
    ) -> None:
        """Destroy and close an old backend after its in-flight calls exit."""

        return await _manager_bindings._retire_replaced_backend(
            self,
            handle,
            backend,
        )

    def _workspace_root(self) -> str | None:
        """Read and validate the client-declared model-visible workspace root."""

        return _manager_bindings._workspace_root(
            self,
        )

    def _backend_view(
        self,
        owner_key: str,
        handle: OpenSandboxHandle,
    ) -> _ManagedBackend:
        """Cache a borrowed rooted view with stable per-owner object identity."""

        return _manager_bindings._backend_view(
            self,
            owner_key,
            handle,
        )

    def workspace(
        self, key: KeyT, *, routes: Mapping[str, BackendProtocol] | None = None
    ) -> Workspace[RootedOpenSandboxBackend, BackendProtocol]:
        """Declare a rooted workspace that uses each Runtime's logical namespace.

        Pass this value as the Runtime's backend. Creation performs no resource
        I/O. Each Run borrows the manager until its Graph has stopped; finishing a
        Run neither closes the manager nor destroys the stable remote Sandbox.

        Args:
            key: Application key selecting a user, session, project, or other owner.
            routes: Optional filesystem paths served by other borrowed backends.

        Returns:
            A lazy declaration exposing the rooted workspace to Tool preparation.

        Raises:
            ValueError: The client configuration disables rooted workspaces.
        """

        from ._workspace import SandboxWorkspace

        if self._workspace_root() is None:
            raise ValueError("workspace requires a configured workspace_root")
        return SandboxWorkspace(self, key, routes or {})

    async def get(self, key: KeyT, *, namespace: str | None = None) -> _ManagedBackend:
        """Return the healthy stable backend for one caller-defined key.

        Resolution checks the local handle, committed State binding, warm pool, and
        on-demand creation in that order. An unavailable binding follows the recovery
        policy; defaults preserve it. Calls
        resolving to the same owner receive the same stable handle.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Returns:
            A manager-owned backend view whose remote Sandbox can be replaced.

        Raises:
            OpenSandboxManagerClosedError: The manager has begun closing.
            OpenSandboxStateError: State acquisition, renewal, or commit failed.
            Exception: The OpenSandbox client could not create a remote instance.
        """

        return await _manager_bindings.get(
            self,
            key,
            namespace=namespace,
        )

    async def _get_locked(
        self,
        owner_key: str,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxHandle:
        """Resolve the authoritative binding while holding its State owner claim."""

        return await _manager_bindings._get_locked(
            self,
            owner_key,
            claim,
        )

    async def _close_replaced_backend(
        self,
        handle: OpenSandboxHandle,
        backend: OpenSandboxBackend,
    ) -> None:
        """Close an idle old connection without destroying its rebound remote instance."""

        return await _manager_bindings._close_replaced_backend(
            self,
            handle,
            backend,
        )

    async def recreate(
        self, key: KeyT, *, namespace: str | None = None
    ) -> _ManagedBackend:
        """Create and commit a replacement Sandbox for one caller-defined key.

        An open handle is updated in place. The previous remote instance is retired
        only after the replacement binding is committed.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Returns:
            The stable backend view pointing at the replacement instance.
        """

        return await _manager_bindings.recreate(
            self,
            key,
            namespace=namespace,
        )

    async def reconnect(
        self, key: KeyT, *, namespace: str | None = None
    ) -> _ManagedBackend:
        """Reconnect an existing binding while preserving its remote identity.

        Uses the recovery retry limits but never creates or recreates an instance,
        even when the configured failure action permits recreation for ``get()``.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Returns:
            A stable backend view connected to the same remote instance.

        Raises:
            OpenSandboxBackendUnavailableError: No binding exists or recovery fails.
            OpenSandboxStateError: Authoritative ownership cannot be established.
        """
        return await _manager_bindings.reconnect(self, key, namespace=namespace)

    async def reset(self, key: KeyT, *, namespace: str | None = None) -> None:
        """Clear the configured workspace while retaining identity and binding.

        ``workspace_root`` is the only permitted deletion boundary. Reset is refused
        when it is disabled. Once deletion begins, cancellation waits for the fixed
        workspace operation to settle before releasing the owner claim.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Raises:
            OpenSandboxResetError: No safe workspace is configured or cleanup fails.
        """

        return await _manager_bindings.reset(
            self,
            key,
            namespace=namespace,
        )

    async def pause(
        self, key: KeyT, *, namespace: str | None = None, timeout: float = 30.0
    ) -> None:
        """Pause the original instance after all registered processes finish work.

        Holders close local admission and confirm remote settlement before the
        official pause request. A drain timeout cancels the intent only when State
        confirms no request was dispatched. Pausing does not extend expiration.

        Args:
            key: Caller-defined identity resolved to an existing binding.
            namespace: Logical resource scope, or None for standalone use.
            timeout: Positive total work budget in seconds, including coordination.

        Raises:
            OpenSandboxBackendError: Pause or remote confirmation fails.
            OpenSandboxStateError: Shared ownership cannot be verified.
        """
        await self._availability.pause(
            self._resolve_resource_key(key, namespace), timeout
        )

    async def resume(
        self, key: KeyT, *, namespace: str | None = None, timeout: float = 30.0
    ) -> _ManagedBackend:
        """Resume the original paused instance and return its ready stable backend.

        A definitely undispatched drain is cancelled. Unconfirmed requests are not
        replayed. Each holder refreshes its connection before reopening admission.
        No instance is recreated.

        Args:
            key: Caller-defined identity resolved to an existing binding.
            namespace: Logical resource scope, or None for standalone use.
            timeout: Positive total work budget in seconds, including readiness.

        Returns:
            The manager-owned backend view for the original instance.

        Raises:
            OpenSandboxBackendError: The instance is stopped, missing, or not ready.
            OpenSandboxStateError: Shared ownership cannot be verified.
        """
        owner_key = self._resolve_resource_key(key, namespace)
        handle = await self._availability.resume(owner_key, timeout)
        return self._backend_view(owner_key, handle)

    async def get_diagnostic_logs(
        self, key: KeyT, *, namespace: str | None = None, scope: str = "container"
    ) -> OpenSandboxDiagnosticContent:
        """Read scoped logs without creating, reconnecting, or renewing an instance.

        Args:
            key: Caller-defined identity resolved to an existing binding.
            namespace: Logical resource scope, or None for standalone use.
            scope: Provider log scope; Docker supports ``container`` and ``all``.

        Returns:
            Trusted diagnostic content including truncation and completeness.

        Raises:
            OpenSandboxBackendError: The binding is absent or the query is rejected.
            OpenSandboxStateError: The authoritative binding cannot be read.
        """
        owner_key = self._resolve_resource_key(key, namespace)
        async with self._operation():
            binding = await self._state.read_binding(owner_key)
            if binding is None:
                raise OpenSandboxBackendUnavailableError(
                    "No Sandbox is bound to this owner", context={"reason": "not_bound"}
                )
            return await self._client.get_diagnostic_logs(
                binding.sandbox_id, scope=scope
            )

    async def get_diagnostic_events(
        self, key: KeyT, *, namespace: str | None = None, scope: str = "runtime"
    ) -> OpenSandboxDiagnosticContent:
        """Read scoped event diagnostics without waking or changing an instance.

        Args:
            key: Caller-defined identity resolved to an existing binding.
            namespace: Logical resource scope, or None for standalone use.
            scope: Provider event scope; Docker supports ``runtime`` and ``all``.

        Returns:
            Trusted diagnostic content. Docker supplies a state summary, not a
            complete historical event stream.

        Raises:
            OpenSandboxBackendError: The binding is absent or the query is rejected.
            OpenSandboxStateError: The authoritative binding cannot be read.
        """
        owner_key = self._resolve_resource_key(key, namespace)
        async with self._operation():
            binding = await self._state.read_binding(owner_key)
            if binding is None:
                raise OpenSandboxBackendUnavailableError(
                    "No Sandbox is bound to this owner", context={"reason": "not_bound"}
                )
            return await self._client.get_diagnostic_events(
                binding.sandbox_id, scope=scope
            )

    async def destroy(self, key: KeyT, *, namespace: str | None = None) -> None:
        """Destroy known remote instances and remove the committed binding."""
        await self.delete(key, namespace=namespace)

    async def is_healthy(self, key: KeyT, *, namespace: str | None = None) -> bool:
        """Check only the currently cached local handle for one key.

        This method does not read State, create, reconnect, or renew a Sandbox.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Returns:
            Whether the open cached handle passes its health command.
        """

        return await _manager_bindings.is_healthy(
            self,
            key,
            namespace=namespace,
        )

    async def get_details(
        self, key: KeyT, *, namespace: str | None = None
    ) -> OpenSandboxDetails | None:
        """Read stable details for the Sandbox committed to one key.

        This method does not create, renew, reconnect a business handle, or mutate
        State. It may run the configured health command through either the cached
        backend or a temporary read-only connection.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Returns:
            Owner-aware details, or ``None`` when no binding exists.

        Raises:
            OpenSandboxStateError: The authoritative binding could not be read.
        """

        return await _manager_bindings.get_details(
            self,
            key,
            namespace=namespace,
        )

    async def _delete_locked(
        self,
        owner_key: str,
        claim: OpenSandboxOwnerClaim,
    ) -> None:
        """Run retryable deletion and remove the binding after confirmed destruction.

        Every ID that may belong to the owner enters ``remaining_ids`` first. Strict
        destruction failures retain unconfirmed IDs in memory so a manager without an
        external store can retry deletion.
        """

        return await _manager_bindings._delete_locked(
            self,
            owner_key,
            claim,
        )

    async def delete(self, key: KeyT, *, namespace: str | None = None) -> None:
        """Destroy all known instances and remove one key binding.

        Once destruction starts, caller cancellation waits for the internal operation
        to settle. A failed destruction retains the binding or in-memory retry target.

        Args:
            key: Opaque application identity accepted by ``key_resolver``.
            namespace: Logical resource scope, or None for standalone use.

        Raises:
            OpenSandboxDestroyError: Destruction is unconfirmed and can be retried.
            OpenSandboxStateError: The State claim or binding mutation failed.
            OpenSandboxManagerClosedError: The manager has begun closing.
        """

        return await _manager_bindings.delete(
            self,
            key,
            namespace=namespace,
        )

    async def _close_resources(self) -> None:
        try:
            await _manager_resources._close_resources(self)
        finally:
            # Public operations and warm maintenance have settled before delivery
            # stops accepting events. Observer objects remain owned by the host.
            await self._notifications.aclose()

    async def aclose(self) -> None:
        """Idempotently close every resource owned by this manager.

        All callers await one shielded close task. Process-local State resources are
        destroyed; persistent bindings, shared warm slots, and cleanup work remain
        available to another worker. The client and State are both closed. A finite
        ``settlement_timeout`` limits only the current caller's wait; the same close
        task remains owned and can be awaited by calling ``aclose`` again.
        Accepted lifecycle notifications are drained after resources settle. Observer
        objects remain borrowed; slow cooperative observers can extend this wait by
        their bounded pending capacity and per-callback timeout.

        Raises:
            OpenSandboxSettlementTimeoutError: The configured caller wait expires
                before the shared close task settles.
            OpenSandboxObserverReentryError: An observer reenters its own manager.
        """
        self._notifications.check_reentry()
        async with self._state_lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                self._cleanup_wakeup.set()
                close_task = asyncio.create_task(
                    self._close_resources(),
                    name="tinkerfin-opensandbox-close",
                )
                self._close_task = close_task
                close_task.add_done_callback(self._close_finished)
        timeout = self._settlement_timeout
        if timeout is None:
            await asyncio.shield(close_task)
            return
        if close_task.done():
            close_task.result()
            return
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await asyncio.shield(close_task)
        except TimeoutError as error:
            if deadline.expired():
                raise OpenSandboxSettlementTimeoutError(timeout=timeout) from error
            raise

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure when no caller waits again."""

        if not task.cancelled():
            task.exception()
