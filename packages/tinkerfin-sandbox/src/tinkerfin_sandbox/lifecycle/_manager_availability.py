"""Coordinate explicit pause across every registered holder of one binding.

Ordinary data calls keep their process-local admission path. A remote pause is
issued only after each persisted holder has closed admission and acknowledged
settlement. Expired workers never stand in for that evidence.
"""

from __future__ import annotations

__all__ = ["_SandboxAvailability"]

import asyncio
import math
from contextvars import Context
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar
from uuid import uuid4

from ..backends.handle import OpenSandboxHandle
from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBusyError,
    OpenSandboxError,
    OpenSandboxLifecycleUncertainError,
    OpenSandboxPausedError,
    OpenSandboxStateError,
)
from ._manager_recovery import recover_binding
from ._notifications import failure_reason
from ._purpose import validate_purpose
from .availability import OpenSandboxAvailability, OpenSandboxAvailabilityPhase
from .client import _connection_deadline
from .state import OpenSandboxBinding, OpenSandboxOwnerClaim

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")


class _StateChangeCancelled(asyncio.CancelledError):
    """Keep an exact committed result available to its cancelled lifecycle owner."""

    def __init__(
        self,
        previous: OpenSandboxAvailability,
        current: OpenSandboxAvailability,
        cancellation: asyncio.CancelledError,
    ) -> None:
        super().__init__(*cancellation.args)
        self.previous = previous
        self.current = current
        self.cancellation = cancellation


@dataclass(slots=True)
class _LocalHolder:
    owner_key: str
    handle: OpenSandboxHandle
    availability: OpenSandboxAvailability


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError("timeout must be a number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    return float(timeout)


def _require_running(availability: OpenSandboxAvailability) -> None:
    if availability.phase == "paused":
        raise OpenSandboxPausedError("Sandbox is paused; call resume before using it")
    if availability.phase == "draining":
        raise OpenSandboxBusyError(
            "Sandbox is waiting for current operations before pausing"
        )
    if availability.phase != "running":
        raise OpenSandboxLifecycleUncertainError(
            "Sandbox lifecycle request has not been confirmed"
        )


class _SandboxAvailability(Generic[KeyT]):
    """Own this manager's registrations, admission updates, and refresh tasks."""

    def __init__(self, manager: OpenSandboxManager[KeyT]) -> None:
        self._manager = manager
        self._holder_id = uuid4().hex
        self._holders: dict[str, _LocalHolder] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._refresh_attempts: dict[str, tuple[int, int]] = {}
        self._sync_lock = asyncio.Lock()
        self._closed = False

    async def require_running(self, owner_key: str) -> OpenSandboxAvailability | None:
        availability = await self._manager._state.read_availability(owner_key)
        if availability is not None:
            _require_running(availability)
        return availability

    async def register(
        self, owner_key: str, claim: OpenSandboxOwnerClaim, handle: OpenSandboxHandle
    ) -> None:
        """Retain exact cleanup ownership before a possibly committed registration.

        The owner claim fixes the binding identity while State registers this
        holder. Keep that identity locally before dispatch: even a committed write
        whose response is cancelled can then be removed by close after proving
        idle. State owns settlement of its database transaction; this call remains
        cancellable while a custom State waits before writing. See registration
        cancellation and recovery contracts in test_manager and test_pause_resume.
        """
        existing = self._holders.get(claim.owner_digest)
        if (
            existing is None
            or existing.handle is not handle
            or existing.availability.sandbox_id != handle.id
        ):
            handle._suspend_calls("registering")

        snapshot = await self._snapshot(owner_key)
        _require_running(snapshot)
        local = _LocalHolder(owner_key, handle, snapshot)
        self._holders[claim.owner_digest] = local
        local.availability = await self._manager._state.register_holder(
            claim, self._holder_id
        )
        handle._allow_calls()
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(
                self._poll(), name="tinkerfin-sandbox-availability", context=Context()
            )

    async def _poll(self) -> None:
        while not self._closed:
            try:
                async with asyncio.timeout(2.0):
                    await self.synchronize()
            except (OpenSandboxStateError, TimeoutError):
                # No acknowledgement means no remote pause can proceed. A State
                # outage must not disable a previously admitted running handle.
                pass
            await asyncio.sleep(0.5)

    async def synchronize(self) -> None:
        """Apply one batch without waiting for owner claims or remote requests."""
        async with self._sync_lock:
            updates = await self._manager._state.get_holder_updates(self._holder_id)
            for update in updates:
                local = self._holders.get(update.owner_digest)
                if local is None:
                    continue
                snapshot = update.availability
                previous = local.availability
                if (
                    update.sandbox_id != previous.sandbox_id
                    or update.binding_generation != previous.binding_generation
                ):
                    # A locally retained registration attempt is not evidence that
                    # State accepted it. An earlier binding's row cannot reopen
                    # admission or acknowledge work for the pending successor.
                    continue
                if snapshot.binding_generation < previous.binding_generation:
                    continue
                same_binding = (
                    snapshot.sandbox_id == previous.sandbox_id
                    and snapshot.binding_generation == previous.binding_generation
                )
                if not same_binding:
                    # Replacement keeps the existing access-driven recovery
                    # contract. Only resume refreshes a connection proactively;
                    # an old holder can never acknowledge a different binding.
                    continue
                if same_binding and snapshot.sequence < previous.sequence:
                    continue
                if snapshot.phase != "running":
                    local.handle._suspend_calls(snapshot.phase)
                    if same_binding:
                        local.availability = snapshot
                    if (
                        same_binding
                        and snapshot.phase == "draining"
                        and local.handle._is_idle()
                    ):
                        await self._manager._state.acknowledge_idle(
                            self._holder_id, snapshot
                        )
                    continue
                if (
                    not same_binding
                    or snapshot.connection_generation != previous.connection_generation
                ):
                    local.handle._suspend_calls("resuming")
                    target = (
                        snapshot.binding_generation,
                        snapshot.connection_generation,
                    )
                    task = self._refresh_tasks.get(update.owner_digest)
                    if (task is None or task.done()) and self._refresh_attempts.get(
                        update.owner_digest
                    ) != target:
                        self._refresh_attempts[update.owner_digest] = target
                        self._refresh_tasks[update.owner_digest] = asyncio.create_task(
                            self._refresh(local.owner_key),
                            name="tinkerfin-sandbox-resume-connection",
                            context=Context(),
                        )
                    continue
                local.availability = snapshot
                local.handle._allow_calls()

    async def _refresh(self, owner_key: str) -> None:
        manager = self._manager
        try:
            async with manager._operation():
                async with manager._claim_owner(owner_key) as claim:
                    if claim.binding is None:
                        return
                    snapshot = await self.require_running(owner_key)
                    if snapshot is None:
                        return
                    local = self._holders.get(claim.owner_digest)
                    if local is not None and local.availability == snapshot:
                        return
                    handle = await recover_binding(
                        manager,
                        owner_key,
                        claim,
                        purpose=claim.binding.purpose,
                        reconnect=True,
                        allow_recreate=False,
                    )
                    await self.register(owner_key, claim, handle)
        except OpenSandboxError as error:
            local = next(
                (
                    item
                    for item in self._holders.values()
                    if item.owner_key == owner_key
                ),
                None,
            )
            if local is not None:
                manager._notifications.failed(
                    owner_key, local.handle.id, failure_reason(error)
                )

    def is_suspended(self, owner_key: str) -> bool:
        """Read the locally known pause without adding I/O to health checks."""
        return any(
            local.owner_key == owner_key and local.availability.phase != "running"
            for local in self._holders.values()
        )

    def forget(self, owner_key: str) -> None:
        """Release local registration references after confirmed remote deletion."""
        for digest, local in tuple(self._holders.items()):
            if local.owner_key == owner_key:
                self._holders.pop(digest, None)
                self._refresh_attempts.pop(digest, None)

    def confirms_binding(self, owner_key: str, binding: OpenSandboxBinding) -> bool:
        """Require reconnect validation after an owner commits a different binding.

        A repeated remote ID cannot reuse the previous generation's connection
        and skip its provider purpose check. The existing holder registration
        supplies this evidence without copying purpose into local handle state.
        """
        return any(
            local.owner_key == owner_key
            and local.availability.sandbox_id == binding.sandbox_id
            and local.availability.binding_generation == binding.generation
            for local in self._holders.values()
        )

    async def require_resolved(
        self, owner_key: str, claim: OpenSandboxOwnerClaim
    ) -> None:
        """Keep explicit replacement and deletion behind unresolved remote requests."""
        snapshot = await self._manager._state.read_availability(owner_key)
        if snapshot is not None:
            snapshot = await self._confirm_pending(claim, snapshot)
            if snapshot.phase == "uncertain":
                raise OpenSandboxLifecycleUncertainError(
                    "Sandbox lifecycle outcome is unconfirmed"
                )

    async def _snapshot(self, owner_key: str) -> OpenSandboxAvailability:
        snapshot = await self._manager._state.read_availability(owner_key)
        if snapshot is None:
            raise OpenSandboxBackendUnavailableError(
                "No Sandbox is bound to this owner", context={"reason": "not_bound"}
            )
        return snapshot

    async def _remote_state(self, sandbox_id: str) -> str:
        info = await self._manager._client.get_runtime_info(sandbox_id)
        if not info.available or info.status is None:
            raise OpenSandboxBackendUnavailableError(
                "Sandbox lifecycle status is unavailable",
                context={"reason": info.unavailable_reason or "unreachable"},
            )
        state = info.status.state.casefold()
        if state in {"failed", "stopping", "terminated"}:
            raise OpenSandboxBackendUnavailableError(
                "The existing Sandbox cannot be resumed from its stopped state",
                context={"reason": "stopped"},
            )
        return state

    async def _change(
        self,
        claim: OpenSandboxOwnerClaim,
        snapshot: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Retain a possibly committed CAS until its result reaches this owner.

        Cancelling a waiter cannot erase the returned transition identity. The
        caller uses that exact result to cancel an undispatched intent, or keeps
        a transition whose remote request has already been issued.
        """
        task = asyncio.create_task(
            self._manager._state.change_availability(
                claim, snapshot, phase=phase, refresh_connection=refresh_connection
            ),
            name="tinkerfin-sandbox-availability-change",
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        try:
            result = task.result()
        except BaseException as error:
            if cancellation is not None:
                cancellation.add_note(
                    f"Sandbox availability change also failed: {type(error).__name__}"
                )
                raise cancellation from error
            raise
        if cancellation is not None:
            raise _StateChangeCancelled(snapshot, result, cancellation)
        return result

    async def _confirm_pending(
        self, claim: OpenSandboxOwnerClaim, snapshot: OpenSandboxAvailability
    ) -> OpenSandboxAvailability:
        """Only observation of the requested target settles a possibly late request."""
        if snapshot.phase not in {"pausing", "resuming"}:
            return snapshot
        target = "paused" if snapshot.phase == "pausing" else "running"
        if await self._remote_state(snapshot.sandbox_id) != target:
            raise OpenSandboxLifecycleUncertainError(
                "The previous lifecycle request has no confirmed outcome"
            )
        try:
            return await self._change(
                claim, snapshot, phase=target, refresh_connection=target == "running"
            )
        except _StateChangeCancelled as error:
            # This change only records an already observed remote outcome. No
            # compensation is needed, and internal result carriers stay private.
            raise error.cancellation

    async def _wait_remote(self, sandbox_id: str, target: str) -> None:
        while await self._remote_state(sandbox_id) != target:
            await asyncio.sleep(0.1)

    async def _restore_rejected(
        self,
        claim: OpenSandboxOwnerClaim,
        snapshot: OpenSandboxAvailability,
        error: BaseException,
    ) -> None:
        """Only an explicit rejection plus the original state authorizes rollback."""
        if (
            not isinstance(error, OpenSandboxBackendError)
            or error.context.get("request_outcome") != "rejected"
        ):
            return
        original = "running" if snapshot.phase == "pausing" else "paused"
        try:
            async with asyncio.timeout(2.0):
                if await self._remote_state(snapshot.sandbox_id) == original:
                    await self._manager._state.change_availability(
                        claim, snapshot, phase=original
                    )
                    await self.synchronize()
        except (OpenSandboxError, TimeoutError):
            # Preserve the provider rejection as the primary error. Failed
            # reconciliation leaves the authoritative intent closed.
            return

    async def _cancel_drain(
        self,
        claim: OpenSandboxOwnerClaim,
        snapshot: OpenSandboxAvailability,
        phase: OpenSandboxAvailabilityPhase,
    ) -> None:
        """A successful CAS proves no remote pause was dispatched before reopening."""
        async with asyncio.timeout(2.0):
            await self._manager._state.change_availability(claim, snapshot, phase=phase)
            await self.synchronize()

    async def _settle_failed_drain(
        self,
        claim: OpenSandboxOwnerClaim,
        snapshot: OpenSandboxAvailability,
        phase: OpenSandboxAvailabilityPhase = "running",
    ) -> None:
        task = asyncio.create_task(
            self._cancel_drain(claim, snapshot, phase),
            name="tinkerfin-sandbox-cancel-pause",
        )
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        # Failure retains the closed intent. In particular, no local timer may
        # reopen admission while the authoritative State cannot be confirmed.
        if not task.cancelled():
            task.exception()

    async def pause(self, owner_key: str, timeout: float) -> None:
        """Drain all registered holders before issuing the one official request."""
        deadline = asyncio.get_running_loop().time() + _validate_timeout(timeout)
        manager = self._manager
        token = _connection_deadline.set(deadline)
        try:
            async with manager._operation(), asyncio.timeout_at(deadline):
                async with manager._claim_owner(owner_key) as claim:
                    snapshot = await self._confirm_pending(
                        claim, await self._snapshot(owner_key)
                    )
                    if snapshot.phase == "paused":
                        if await self._remote_state(snapshot.sandbox_id) == "paused":
                            manager._workspace_watches.stop(owner_key)
                            return
                    else:
                        _require_running(snapshot)
                    original_phase = snapshot.phase
                    request_started = False
                    try:
                        snapshot = await self._change(claim, snapshot, phase="draining")
                        manager._workspace_watches.stop(owner_key)
                        while True:
                            await self.synchronize()
                            if await manager._state.holders_are_idle(claim, snapshot):
                                break
                            await asyncio.sleep(0.05)
                        snapshot = await self._change(claim, snapshot, phase="pausing")
                        request_started = True
                        await manager._client.pause(snapshot.sandbox_id)
                        await self._wait_remote(snapshot.sandbox_id, "paused")
                        snapshot = await self._change(claim, snapshot, phase="paused")
                        await self.synchronize()
                        manager._notifications.paused(owner_key, snapshot.sandbox_id)
                    except BaseException as error:
                        if isinstance(error, _StateChangeCancelled):
                            snapshot = error.current
                        if snapshot.phase == "draining" or (
                            snapshot.phase == "pausing" and not request_started
                        ):
                            await self._settle_failed_drain(
                                claim, snapshot, original_phase
                            )
                        elif snapshot.phase == "pausing":
                            await self._restore_rejected(claim, snapshot, error)
                        if isinstance(error, _StateChangeCancelled):
                            # Normalize inside the timeout context. Python 3.11
                            # recognizes only the original CancelledError type.
                            raise error.cancellation
                        raise
        except TimeoutError as error:
            raise OpenSandboxBackendTimeoutError(
                "Sandbox pause exceeded its time budget; the existing binding was preserved",
                cause=error,
            ) from error
        finally:
            _connection_deadline.reset(token)

    async def resume(self, owner_key: str, timeout: float) -> None:
        """Resume the original instance and publish a ready local connection."""
        deadline = asyncio.get_running_loop().time() + _validate_timeout(timeout)
        manager = self._manager
        token = _connection_deadline.set(deadline)
        try:
            async with manager._operation(), asyncio.timeout_at(deadline):
                async with manager._claim_owner(owner_key) as claim:
                    if claim.binding is None:
                        raise OpenSandboxBackendUnavailableError(
                            "No Sandbox is bound to this owner",
                            context={"reason": "not_bound"},
                        )
                    purpose = validate_purpose(claim.binding.purpose)
                    snapshot = await self._confirm_pending(
                        claim, await self._snapshot(owner_key)
                    )
                    request_started = False
                    try:
                        if snapshot.phase == "draining":
                            snapshot = await self._change(
                                claim, snapshot, phase="running"
                            )
                        remote = await self._remote_state(snapshot.sandbox_id)
                        if snapshot.phase == "paused" or (
                            snapshot.phase == "running" and remote == "paused"
                        ):
                            snapshot = await self._change(
                                claim, snapshot, phase="resuming"
                            )
                            if remote != "running":
                                request_started = True
                                try:
                                    await manager._client.resume(snapshot.sandbox_id)
                                except BaseException as error:
                                    await self._restore_rejected(claim, snapshot, error)
                                    raise
                            await self._wait_remote(snapshot.sandbox_id, "running")
                            snapshot = await self._change(
                                claim,
                                snapshot,
                                phase="running",
                                refresh_connection=True,
                            )
                        _require_running(snapshot)
                        handle = await recover_binding(
                            manager,
                            owner_key,
                            claim,
                            purpose=purpose,
                            reconnect=True,
                            allow_recreate=False,
                        )
                        await self.register(owner_key, claim, handle)
                        manager._notifications.resumed(owner_key, snapshot.sandbox_id)
                    except _StateChangeCancelled as error:
                        if error.current.phase == "resuming" and not request_started:
                            await self._settle_failed_drain(
                                claim, error.current, error.previous.phase
                            )
                        raise error.cancellation
        except TimeoutError as error:
            raise OpenSandboxBackendTimeoutError(
                "Sandbox resume exceeded its time budget; the existing binding was preserved",
                cause=error,
            ) from error
        finally:
            _connection_deadline.reset(token)

    async def stop_polling(self) -> None:
        """Stop local coordination before handle closure, leaving shared intent intact."""
        self._closed = True
        tasks = [
            task
            for task in [self._poll_task, *self._refresh_tasks.values()]
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def release_idle_holders(self) -> None:
        """Remove only registrations whose admitted remote work has settled."""
        for local in tuple(self._holders.values()):
            if local.handle._is_idle():
                await self._manager._state.unregister_holder(
                    self._holder_id, local.availability
                )
        self._holders.clear()
