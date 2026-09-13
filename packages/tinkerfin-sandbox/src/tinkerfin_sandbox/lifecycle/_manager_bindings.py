"""Authoritative Sandbox binding, replacement, and deletion operations."""

from __future__ import annotations

__all__ = [
    "_backend_view",
    "_bind_on_demand_backend",
    "_close_replaced_backend",
    "_delete_locked",
    "_get_locked",
    "_reconcile_candidate_binding",
    "_replace",
    "_retire_replaced_backend",
    "_workspace_root",
]

import asyncio
from typing import TYPE_CHECKING, Literal, TypeAlias, TypeVar

from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxDestroyError,
    OpenSandboxResetError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
)
from ..models import (
    OpenSandboxDetails,
    OpenSandboxRuntimeInfo,
    _normalize_workspace_root,
)
from ._identity import SandboxResourceIdentity
from ._manager_recovery import _check_health, recover_binding
from ._manager_resources import _ManagedBackend
from ._notifications import failure_reason
from .notifications import OpenSandboxLifecycleReason as Reason
from .state import OpenSandboxBinding, OpenSandboxOwnerClaim

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")
_BindingResolution: TypeAlias = Literal[
    "authoritative",
    "not_authoritative",
    "unknown",
]


async def _reconcile_candidate_binding(
    self: OpenSandboxManager[KeyT],
    *,
    owner_key: str,
    expected: OpenSandboxBinding,
    primary_error: BaseException,
) -> _BindingResolution:
    """Classify a failed bind from one authoritative State read."""

    try:
        binding = await self._state.read_binding(owner_key)
    except (Exception, asyncio.CancelledError) as reconciliation_error:  # noqa: BLE001 - host State boundary
        primary_error.add_note(
            "OpenSandbox binding reconciliation also failed: "
            f"{type(reconciliation_error).__name__}: {reconciliation_error}"
        )
        return "unknown"
    if binding == expected:
        return "authoritative"
    return "not_authoritative"


async def _bind_on_demand_backend(
    self: OpenSandboxManager[KeyT],
    *,
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
    backend: OpenSandboxBackend,
) -> OpenSandboxBinding:
    """Commit or reconcile one candidate before deciding its cleanup ownership."""

    expected = OpenSandboxBinding(
        sandbox_id=backend.id,
        generation=claim.generation,
    )
    try:
        committed = await self._state.bind_owner(claim, backend.id)
    except asyncio.CancelledError as cancellation:
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=cancellation,
        )
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise
    except Exception as bind_error:
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=bind_error,
        )
        if resolution == "authoritative":
            return expected
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise

    if committed != expected:
        mismatch = OpenSandboxStateError(
            "OpenSandbox State returned a binding that does not match the "
            "committed candidate"
        )
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=mismatch,
        )
        if resolution == "authoritative":
            return expected
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise mismatch
    return committed


async def _replace(
    self: OpenSandboxManager[KeyT],
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
    acquisition = await self._acquire_backend(claim)
    backend = acquisition.backend
    try:
        committed = acquisition.committed_binding
        if committed is None:
            await self._bind_on_demand_backend(
                owner_key=owner_key,
                claim=claim,
                backend=backend,
            )
        elif committed != OpenSandboxBinding(
            sandbox_id=backend.id,
            generation=claim.generation,
        ):
            await self._cleanup_owned_backend(backend, destroy=False)
            raise OpenSandboxStateError(
                "OpenSandbox State returned an incompatible committed warm binding"
            )
    finally:
        if acquisition.consumed_warm_slot:
            self._schedule_replenish()

    old_backend = None
    if existing_handle is None or existing_handle.is_closed:
        handle = OpenSandboxHandle(backend)
    else:
        try:
            old_backend = existing_handle._replace_backend(backend)
        except RuntimeError:
            # Closure can make a handle non-replaceable during lifecycle settlement.
            handle = OpenSandboxHandle(backend)
        else:
            handle = existing_handle
    self._handles[owner_key] = handle
    # The State commit and stable handle publication make replacement observable.
    # Subsequent old-resource cleanup cannot revoke this confirmed transition.
    self._notifications.published(owner_key, backend.id, old_id)

    cleanup_tasks: list[asyncio.Task[None]] = []
    retire_ids = set(acquisition.retire_after_commit_ids)
    retire_ids.discard(backend.id)
    if old_backend is not None:
        retire = (
            self._retire_replaced_backend(handle, old_backend)
            if old_backend.id == old_id or old_backend.id in retire_ids
            else self._close_replaced_backend(handle, old_backend)
        )
        cleanup_tasks.append(asyncio.create_task(retire))
        retire_ids.discard(old_backend.id)
    if old_id is not None and old_id not in {
        backend.id,
        None if old_backend is None else old_backend.id,
    }:
        retire_ids.add(old_id)
    for sandbox_id in sorted(retire_ids):
        cleanup_tasks.append(asyncio.create_task(self._destroy_remote(sandbox_id)))

    # Transfer cleanup ownership for every stale resource before awaiting any one
    # task so cancellation cannot orphan the remainder.
    for cleanup_task in cleanup_tasks:
        self._track_cleanup_task(cleanup_task)
    # Registration can fail or be cancelled. Every old resource must already
    # belong to cleanup before that new I/O boundary is entered.
    await self._availability.register(owner_key, claim, handle)
    for cleanup_task in cleanup_tasks:
        await asyncio.shield(cleanup_task)

    await self._renew_backend(handle)
    return handle


async def _retire_replaced_backend(
    self: OpenSandboxManager[KeyT],
    handle: OpenSandboxHandle,
    backend: OpenSandboxBackend,
) -> None:
    """Destroy and close an old backend after its in-flight calls exit."""
    await handle._await_until_idle(backend)
    await self._dispose_backend(backend)


def _workspace_root(self: OpenSandboxManager[KeyT]) -> str | None:
    """Read and validate the client-declared model-visible workspace root."""
    value = getattr(self._client.config, "workspace_root", None)
    return _normalize_workspace_root(value)


def _backend_view(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    handle: OpenSandboxHandle,
) -> _ManagedBackend:
    """Cache a borrowed rooted view with stable per-owner object identity."""
    workspace_root = self._workspace_root()
    if workspace_root is None:
        self._backend_views.pop(owner_key, None)
        return handle
    cached = self._backend_views.get(owner_key)
    if cached is not None and cached[0] is handle:
        return cached[1]
    backend = RootedOpenSandboxBackend(
        handle,
        root=workspace_root,
    )
    self._backend_views[owner_key] = (handle, backend)
    return backend


async def get(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
) -> _ManagedBackend:
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
    owner_key = self._resolve_resource_key(key, namespace)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            await self._availability.require_running(owner_key)
            handle = await self._get_locked(owner_key, claim)
            await self._availability.register(owner_key, claim, handle)
            return self._backend_view(owner_key, handle)


async def _get_locked(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
) -> OpenSandboxHandle:
    """Resolve the authoritative binding while holding its State owner claim."""
    self._ensure_open()
    handle = self._handles.get(owner_key)
    stored_id = claim.binding.sandbox_id if claim.binding is not None else None

    if stored_id is not None:
        return await recover_binding(self, owner_key, claim)
    if handle is not None:
        raise OpenSandboxStateOwnershipError(
            "The local Sandbox no longer has an authoritative owner binding"
        )
    return await self._replace(
        owner_key,
        claim,
        None,
        old_id=None,
    )


async def reconnect(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
) -> _ManagedBackend:
    """Open a verified connection to an existing binding without recreating it."""
    owner_key = self._resolve_resource_key(key, namespace)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            await self._availability.require_running(owner_key)
            handle = await recover_binding(
                self, owner_key, claim, reconnect=True, allow_recreate=False
            )
            await self._availability.register(owner_key, claim, handle)
            return self._backend_view(owner_key, handle)


async def _close_replaced_backend(
    self: OpenSandboxManager[KeyT],
    handle: OpenSandboxHandle,
    backend: OpenSandboxBackend,
) -> None:
    """Close an idle old connection without destroying its rebound remote instance."""
    await handle._await_until_idle(backend)
    await self._close_backend(backend)


async def recreate(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
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
    owner_key = self._resolve_resource_key(key, namespace)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            self._ensure_open()
            await self._availability.require_resolved(owner_key, claim)
            handle = self._handles.get(owner_key)
            if claim.binding is None and handle is not None:
                raise OpenSandboxStateOwnershipError(
                    "The local Sandbox no longer has an authoritative owner binding"
                )
            old_id = claim.binding.sandbox_id if claim.binding is not None else None
            replaceable_handle = (
                handle if handle is not None and not handle.is_closed else None
            )
            if handle is not None and handle.is_closed:
                self._handles.pop(owner_key, None)
            if old_id is not None:
                self._notifications.recovering(
                    owner_key, old_id, Reason.EXPLICIT_RECREATE
                )
            try:
                replaced = await self._replace(
                    owner_key,
                    claim,
                    replaceable_handle,
                    old_id=old_id,
                )
            except Exception as error:
                if old_id is not None:
                    self._notifications.failed(owner_key, old_id, failure_reason(error))
                raise
            return self._backend_view(owner_key, replaced)


async def reset(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
) -> None:
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
    owner_key = self._resolve_resource_key(key, namespace)
    workspace_root = self._workspace_root()
    if workspace_root is None:
        raise OpenSandboxResetError(
            "OpenSandbox workspace_root is required for a safe reset"
        )

    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            await self._availability.require_running(owner_key)
            handle = await recover_binding(self, owner_key, claim, allow_recreate=False)
            await self._availability.register(owner_key, claim, handle)

            async def reset_workspace() -> None:
                await handle._areset_workspace_from_manager(workspace_root)
                self._notifications.workspace_reset(owner_key, handle.id)

            reset_task = asyncio.create_task(reset_workspace())
            try:
                await asyncio.shield(reset_task)
            except asyncio.CancelledError as cancellation:
                while not reset_task.done():
                    try:
                        await asyncio.shield(reset_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                try:
                    reset_task.result()
                except Exception:  # noqa: BLE001 - cancellation remains primary
                    pass
                raise cancellation
            except Exception as exc:
                raise OpenSandboxResetError(
                    f"Failed to clear the OpenSandbox workspace for {owner_key!r}"
                ) from exc


async def is_healthy(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
) -> bool:
    """Check only the currently cached local handle for one key.

    This method does not read State, create, reconnect, or renew a Sandbox.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.
        namespace: Logical resource scope, or None for standalone use.

    Returns:
        Whether the open cached handle passes its health command.
    """
    owner_key = self._resolve_resource_key(key, namespace)
    async with self._operation():
        handle = self._handles.get(owner_key)
        if handle is None or handle.is_closed:
            return False
        if self._availability.is_suspended(owner_key):
            return False
        sandbox_id = handle.id
        check = self._notifications.begin_check(owner_key)
        try:
            try:
                await _check_health(self, handle)
            except Exception as error:  # noqa: BLE001 - preserve this boolean check's contract
                self._notifications.checked(
                    owner_key, sandbox_id, check, failure_reason(error)
                )
                return False
            self._notifications.checked(owner_key, sandbox_id, check, None)
            return True
        finally:
            self._notifications.end_check(owner_key, check)


async def get_details(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
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
    owner_key = self._resolve_resource_key(key, namespace)
    resource = SandboxResourceIdentity.from_key(owner_key)
    async with self._operation():
        check = self._notifications.begin_check(owner_key)
        try:
            binding = await self._state.read_binding(owner_key)
            if binding is None:
                return None
            handle = self._handles.get(owner_key)
            availability = await self._state.read_availability(owner_key)
            suspended = availability is not None and availability.phase != "running"
            refreshing = (
                handle is not None
                and not handle.is_closed
                and not handle._accepts_calls()
            )
            if suspended or refreshing:
                try:
                    runtime = await self._client.get_runtime_info(binding.sandbox_id)
                except OpenSandboxBackendError as error:
                    runtime = OpenSandboxRuntimeInfo.unavailable(
                        binding.sandbox_id,
                        "not_found"
                        if error.context.get("reason") == "not_found"
                        else "unreachable",
                    )
                if not runtime.available:
                    self._notifications.checked(
                        owner_key,
                        binding.sandbox_id,
                        check,
                        Reason.NOT_FOUND
                        if runtime.unavailable_reason == "not_found"
                        else Reason.UNREACHABLE,
                    )
                return OpenSandboxDetails.from_runtime(
                    runtime,
                    owner_key=resource.key,
                    namespace=resource.namespace,
                    cached=handle is not None and not handle.is_closed,
                    access_state=availability.phase
                    if availability is not None
                    else None,
                )
            if (
                handle is not None
                and handle.id == binding.sandbox_id
                and not handle.is_closed
            ):
                runtime = await handle.aget_runtime_info()
                cached = True
            else:
                runtime = await self._client.inspect(binding.sandbox_id)
                cached = False
            reason = None
            if not runtime.available:
                reason = (
                    Reason.NOT_FOUND
                    if runtime.unavailable_reason == "not_found"
                    else Reason.UNREACHABLE
                )
            elif not runtime.healthy:
                reason = Reason.UNHEALTHY
            # A temporary inspection can discover an outage, but cannot confirm
            # managed recovery before this manager publishes a usable handle.
            if reason is not None or cached:
                self._notifications.checked(
                    owner_key, binding.sandbox_id, check, reason
                )
            return OpenSandboxDetails.from_runtime(
                runtime,
                owner_key=resource.key,
                namespace=resource.namespace,
                cached=cached,
                access_state="running" if availability is not None else None,
            )
        finally:
            self._notifications.end_check(owner_key, check)


async def _delete_locked(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
) -> None:
    """Run retryable deletion and remove the binding after confirmed destruction.

    Every ID that may belong to the owner enters ``remaining_ids`` first. Strict
    destruction failures retain unconfirmed IDs in memory so a manager without an
    external store can retry deletion.
    """
    handle = self._handles.get(owner_key)
    stored_id = claim.binding.sandbox_id if claim.binding is not None else None
    self._handles.pop(owner_key, None)
    self._backend_views.pop(owner_key, None)

    remaining_ids = set(self._pending_destroy_ids.get(owner_key, ()))
    if stored_id is not None:
        remaining_ids.add(stored_id)
    backend = None
    if handle is not None:
        backend = await handle._aretire()
        remaining_ids.add(backend.id)

    had_instances = bool(remaining_ids)
    notification_id = (
        stored_id
        if stored_id is not None
        else (backend.id if backend is not None else next(iter(remaining_ids), None))
    )

    try:
        if backend is not None:
            await self._dispose_backend(backend, strict=True)
            remaining_ids.discard(backend.id)
        for sandbox_id in tuple(remaining_ids):
            await self._destroy_remote(sandbox_id, strict=True)
            remaining_ids.discard(sandbox_id)
    except OpenSandboxDestroyError:
        self._pending_destroy_ids[owner_key] = remaining_ids
        raise

    self._pending_destroy_ids.pop(owner_key, None)
    await self._state.unbind_owner(claim)
    self._availability.forget(owner_key)
    if had_instances:
        self._notifications.destroyed(owner_key, notification_id)


async def delete(
    self: OpenSandboxManager[KeyT], key: KeyT, *, namespace: str | None = None
) -> None:
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
    owner_key = self._resolve_resource_key(key, namespace)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            self._ensure_open()
            await self._availability.require_resolved(owner_key, claim)
            delete_task = asyncio.create_task(self._delete_locked(owner_key, claim))
            try:
                await asyncio.shield(delete_task)
            except asyncio.CancelledError as cancellation:
                # Hold the owner claim until destructive work settles so
                # cancellation cannot lose target IDs.
                while not delete_task.done():
                    try:
                        await asyncio.shield(delete_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                try:
                    delete_task.result()
                except Exception:  # noqa: BLE001 - cancellation remains primary
                    pass
                raise cancellation
