"""State contracts for durable OpenSandbox ownership transitions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast, runtime_checkable
from uuid import uuid4

from ..errors import (
    OpenSandboxError,
    OpenSandboxStateConfigurationError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    UnexpectedOpenSandboxStateError,
)
from ..models import OpenSandboxPurpose
from ._purpose import require_purpose, validate_purpose
from .availability import (
    OpenSandboxAvailability,
    OpenSandboxAvailabilityPhase,
    OpenSandboxHolderUpdate,
    _next_availability,
    _validate_holder_id,
)


def _owner_digest(namespace: str, owner_key: str) -> str:
    """Return a stable opaque digest without persisting the raw owner key."""
    value = f"{namespace}\0{owner_key}".encode()
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
    )


@dataclass(frozen=True, slots=True)
class OpenSandboxBinding:
    """Committed remote identity, generation, and immutable capability purpose.

    Replacement preserves purpose even when the remote instance is unavailable.
    Explicit unbinding is required before the owner selects another purpose.
    """

    sandbox_id: str
    generation: int
    purpose: OpenSandboxPurpose


@dataclass(frozen=True, slots=True)
class OpenSandboxOwnerClaim:
    """Fencing identity that exclusively owns one owner state transition."""

    owner_key: str
    owner_digest: str
    token: str
    generation: int
    binding: OpenSandboxBinding | None


@dataclass(frozen=True, slots=True)
class OpenSandboxWarmClaim:
    """Fencing identity for filling one global warm-pool slot."""

    slot: int
    token: str
    generation: int


@dataclass(frozen=True, slots=True)
class OpenSandboxReadyWarmClaim(OpenSandboxWarmClaim):
    """Fencing identity for verifying one published warm Sandbox."""

    sandbox_id: str


@dataclass(frozen=True, slots=True)
class OpenSandboxCleanupClaim:
    """Fencing identity for destroying one orphaned remote Sandbox."""

    sandbox_id: str
    token: str
    generation: int


@runtime_checkable
class OpenSandboxState(Protocol):
    """Atomic owner allocation state used by ``OpenSandboxManager``.

    Implementations serialize transitions for the same owner while allowing
    unrelated owners to proceed independently. A claim is a fencing identity;
    mutations made with a released or superseded claim must fail.
    """

    @property
    def persistent(self) -> bool:
        """Return whether bindings survive State shutdown and process exit."""

        ...

    @property
    def lease_renew_interval(self) -> float | None:
        """Return the claim renewal interval, or ``None`` for non-expiring claims."""

        ...

    async def start(self, *, warm_pool_size: int) -> None:
        """Open State with one immutable warm-pool capacity.

        Args:
            warm_pool_size: Non-negative global slot count fixed for this lifecycle.

        Raises:
            OpenSandboxStateConfigurationError: A repeated start changes capacity.
            OpenSandboxStateError: Durable setup or worker registration fails.
            ValueError: ``warm_pool_size`` is negative.
        """

        ...

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Acquire the exclusive fencing claim for one owner key.

        Args:
            owner_key: Host identity used only to derive the State-owned digest.

        Returns:
            Current exclusive claim and any previously committed binding.

        Raises:
            OpenSandboxStateError: State is closed or ownership cannot be acquired.
        """

        ...

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Renew an owner claim and return whether it still owns the fence.

        Args:
            claim: Exact owner token and generation returned by ``acquire_owner()``.

        Returns:
            Whether the same claim remains authoritative after renewal.

        Raises:
            OpenSandboxStateError: Durable renewal evidence is unavailable.
        """

        ...

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
        *,
        purpose: OpenSandboxPurpose,
    ) -> OpenSandboxBinding:
        """Commit a remote Sandbox as the claimed owner's authoritative binding.

        Args:
            claim: Current exclusive owner claim.
            sandbox_id: Canonical remote Sandbox identifier to publish.
            purpose: Capability retained until this owner is explicitly unbound.

        Returns:
            Newly authoritative binding and owner generation.

        Raises:
            OpenSandboxStateOwnershipError: The claim is stale or no longer current.
            OpenSandboxStateError: The binding cannot be committed durably.
            OpenSandboxPurposeError: An existing binding has a different purpose.
        """

        ...

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove the binding protected by an active owner claim.

        Args:
            claim: Current exclusive owner claim protecting the binding.

        Raises:
            OpenSandboxStateOwnershipError: The claim is stale or no longer current.
            OpenSandboxStateError: Durable binding removal fails.
        """

        ...

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the current authoritative binding without acquiring ownership.

        Args:
            owner_key: Host owner identity whose digest selects the binding.

        Returns:
            Detached authoritative binding, or ``None`` when no binding exists.

        Raises:
            OpenSandboxStateError: State is closed or durable lookup fails.
        """

        ...

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release an owner claim without changing its committed binding.

        Args:
            claim: Owner token and generation to release idempotently.

        Raises:
            OpenSandboxStateError: Durable release fails.
        """

        ...

    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        """Register a manager before publishing a handle for a running binding.

        The current owner fence and running intent are checked atomically. Repeated
        registration of the same holder is idempotent. A stale claim or non-running
        binding raises ``OpenSandboxStateOwnershipError``. A holder ID identifies
        one manager lifetime and must not be reused after that manager closes.

        Args:
            claim: Current owner claim, including after it commits a new binding.
            holder_id: Unique manager identity of at most 36 characters.

        Returns:
            Running availability atomically associated with the registration.

        Raises:
            OpenSandboxStateOwnershipError: Claim is stale or binding is not running.
            OpenSandboxStateError: The registration cannot be committed.
            ValueError: The holder identity is empty or too long.
        """
        ...

    async def read_availability(self, owner_key: str) -> OpenSandboxAvailability | None:
        """Read current intent without waiting for an active owner claim.

        Return ``None`` only when no binding exists. Storage failures raise
        ``OpenSandboxStateError`` and must never imply that admission is safe.

        Args:
            owner_key: Host identity selecting the current owner binding.

        Returns:
            Current availability snapshot, or ``None`` without a binding.

        Raises:
            OpenSandboxStateError: State is closed or availability cannot be read.
        """
        ...

    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        """Read this manager's registrations in one batch without owner claims.

        Results include each holder's binding identity and current owner intent.
        A missing registration means the handle is no longer registered; a changed
        binding identity requires retiring the old handle.

        Args:
            holder_id: Manager lifetime identity used for handle registration.

        Returns:
            Registered bindings and current intents, ordered by owner digest.

        Raises:
            OpenSandboxStateError: State is closed or current intent is unavailable.
        """
        ...

    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Advance a fenced intent only if its full expected snapshot is current.

        Dispatch into ``pausing`` requires every current holder to have acknowledged
        the drain. A failed fence, stale snapshot, illegal phase transition, or
        incomplete drain raises ``OpenSandboxStateOwnershipError``. Increment the
        connection generation with ``refresh_connection`` when publishing running
        availability that requires holders to reconnect. Returning from ``pausing``
        to ``running`` or ``resuming`` to ``paused`` requires an explicit upstream
        rejection and authoritative confirmation of the preceding remote state;
        an unknown request outcome never authorizes these reversals.
        Explicit recovery from an externally paused or resumed Sandbox may enter
        ``resuming`` from ``running`` or ``draining`` from ``paused`` only after
        authoritative control-plane confirmation of that remote state.

        Args:
            claim: Current exclusive owner fence.
            expected: Exact snapshot that must still be authoritative.
            phase: Permitted next phase of the pause or resume operation.
            refresh_connection: Require holder reconnection on returning to running.

        Returns:
            Committed snapshot with its availability sequence incremented.

        Raises:
            OpenSandboxStateOwnershipError: Fence, snapshot, or transition is invalid.
            OpenSandboxStateError: The new intent cannot be committed.
            ValueError: Connection refresh is requested for a non-running phase.
        """
        ...

    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        """Confirm closed admission and settled operations for an exact drain.

        The caller must retain closed admission until a later intent permits it.
        Return ``False`` for missing holders or stale intent/binding evidence.
        This operation must remain available while another manager owns the claim.

        Args:
            holder_id: Manager whose previously admitted operations have settled.
            availability: Exact draining intent observed before closing admission.

        Returns:
            Whether the exact current drain acknowledgement was committed.

        Raises:
            OpenSandboxStateError: The acknowledgement cannot be committed.
        """
        ...

    async def holders_are_idle(
        self, claim: OpenSandboxOwnerClaim, availability: OpenSandboxAvailability
    ) -> bool:
        """Check every current holder's explicit ACK under the exact drain fence.

        Expired worker leases and lost heartbeats never count as idle. A stale
        owner claim or availability snapshot raises ``OpenSandboxStateOwnershipError``.

        Args:
            claim: Current exclusive owner fence protecting the pause operation.
            availability: Exact draining snapshot requiring acknowledgements.

        Returns:
            Whether every current holder acknowledged this precise drain.

        Raises:
            OpenSandboxStateOwnershipError: The owner fence or drain is stale.
            OpenSandboxStateError: Holder evidence cannot be read reliably.
        """
        ...

    async def unregister_holder(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> None:
        """Release an exact binding registration after local operations settle.

        The caller must first close admission and prove local idle. Stale releases
        are harmless; an earlier binding cannot remove a successor's registration.
        State shutdown must not implicitly release unconfirmed registrations.

        Args:
            holder_id: Manager whose admission is closed and operations have settled.
            availability: Snapshot identifying the exact binding being released.

        Raises:
            OpenSandboxStateError: Registration removal cannot be committed.
        """
        ...

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty warm slot without waiting for another worker.

        Returns:
            Exclusive slot claim, or ``None`` when no empty slot is available.

        Raises:
            OpenSandboxStateError: State is closed or slot evidence cannot be read.
        """

        ...

    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None:
        """Claim one published slot without discarding its remote identifier.

        Args:
            exclude_slots: Slot indexes already checked by the current maintenance
                pass.

        Returns:
            Exclusive published-slot claim, or ``None`` when no eligible slot is
            available.

        Raises:
            OpenSandboxStateError: State is closed or slot evidence cannot be read.
        """

        ...

    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None:
        """Remove one verified-unusable warm ID and enqueue durable cleanup.

        Args:
            claim: Current published-slot fencing claim.

        Raises:
            OpenSandboxStateOwnershipError: The claim is stale or superseded.
            OpenSandboxStateError: Durable invalidation or cleanup enqueue fails.
        """

        ...

    async def warm_pool_ready(self) -> bool:
        """Return whether every configured slot retains verified published capacity.

        A temporary verification claim retains its published Sandbox. Consumption
        and confirmed invalidation remove it, so neither counts as ready capacity.
        Persistent publication does not establish fresh startup verification; the
        manager separately verifies its initial capacity before reporting ready.

        Returns:
            Whether the configured warm capacity remains published.

        Raises:
            OpenSandboxStateError: State is closed or capacity evidence is unavailable.
        """

        ...

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a created command-purpose Sandbox into the claimed warm slot.

        Args:
            claim: Current warm-slot fencing claim.
            sandbox_id: Canonical remote Sandbox identifier to publish.

        Raises:
            OpenSandboxStateOwnershipError: The slot claim is stale or superseded.
            OpenSandboxStateError: Durable publication fails.
        """

        ...

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Renew a warm-slot claim and report whether its fence remains current.

        Args:
            claim: Exact warm-slot token and generation to renew.

        Returns:
            Whether the same slot claim remains authoritative.

        Raises:
            OpenSandboxStateError: Durable renewal evidence is unavailable.
        """

        ...

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release a warm-slot claim without consuming its published Sandbox.

        Args:
            claim: Warm-slot token and generation to release idempotently.

        Raises:
            OpenSandboxStateError: Durable release fails.
        """

        ...

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume command capacity and commit its owner binding.

        A non-``None`` result is already authoritative. Callers must publish that
        exact binding without invoking ``bind_owner()`` again.
        A workspace-purpose owner cannot consume or change a warm slot.

        Args:
            claim: Current owner claim receiving an available warm Sandbox.

        Returns:
            Authoritative owner binding, or ``None`` when no ready slot is available.

        Raises:
            OpenSandboxStateError: Claim ownership or durable State access fails.
            OpenSandboxPurposeError: The owner is bound to workspace capability.
        """

        ...

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Idempotently enqueue an orphaned remote Sandbox for destruction.

        Args:
            sandbox_id: Canonical remote Sandbox identifier requiring cleanup.

        Raises:
            OpenSandboxStateError: The cleanup target cannot be persisted.
        """

        ...

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending cleanup item without waiting when the queue is empty.

        Returns:
            Exclusive cleanup claim, or ``None`` when no item is available.

        Raises:
            OpenSandboxStateError: State is closed or cleanup evidence cannot be read.
        """

        ...

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Renew a cleanup claim and report whether its fence remains current.

        Args:
            claim: Exact cleanup token and generation to renew.

        Returns:
            Whether the same cleanup claim remains authoritative.

        Raises:
            OpenSandboxStateError: Durable renewal evidence is unavailable.
        """

        ...

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Remove a cleanup item after confirmed remote destruction.

        Args:
            claim: Current cleanup claim whose remote Sandbox was destroyed.

        Raises:
            OpenSandboxStateOwnershipError: The cleanup claim is stale or superseded.
            OpenSandboxStateError: Durable completion fails.
        """

        ...

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release a cleanup claim so another worker can retry it.

        Args:
            claim: Cleanup token and generation to release idempotently.

        Raises:
            OpenSandboxStateError: Durable release fails.
        """

        ...

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Return process-local Sandbox IDs that this State must destroy on close.

        Returns:
            Stable unique IDs owned by this process and not durably handed off.

        Raises:
            OpenSandboxStateError: Shutdown ownership evidence cannot be read safely.
        """

        ...

    async def aclose(self) -> None:
        """Close State resources after active manager operations have settled.

        Raises:
            OpenSandboxStateError: Worker release, task settlement, or an owned Engine
                close fails. Borrowed Engines are never disposed.
        """

        ...


class _WarmPoolReconciliationState(Protocol):
    """Runtime view of the current ready-slot reconciliation contract."""

    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None: ...

    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None: ...

    async def warm_pool_ready(self) -> bool: ...


async def _call_state(
    _state: OpenSandboxState,
    operation: str,
    awaitable: object,
) -> object:
    """Translate State failures while retaining it until the awaitable settles."""

    try:
        return await cast(Awaitable[object], awaitable)
    except OpenSandboxError:
        raise
    except Exception as error:
        translated = UnexpectedOpenSandboxStateError(
            f"OpenSandbox State {operation} failed",
            diagnostic_context={"operation": operation},
            cause=error,
        )
        raise translated from error


class _OpenSandboxStateBoundary(  # pyright: ignore[reportUnusedClass]
    OpenSandboxState
):
    """Enforce the State failure contract for replaceable implementations."""

    def __init__(self, state: OpenSandboxState) -> None:
        self._state = state

    @property
    def persistent(self) -> bool:
        try:
            return self._state.persistent
        except OpenSandboxStateError:
            raise
        except Exception as error:
            translated = UnexpectedOpenSandboxStateError(
                "OpenSandbox State persistent lookup failed",
                diagnostic_context={"operation": "persistent"},
                cause=error,
            )
            raise translated from error

    @property
    def lease_renew_interval(self) -> float | None:
        try:
            return self._state.lease_renew_interval
        except OpenSandboxStateError:
            raise
        except Exception as error:
            translated = UnexpectedOpenSandboxStateError(
                "OpenSandbox State lease interval lookup failed",
                diagnostic_context={"operation": "lease_renew_interval"},
                cause=error,
            )
            raise translated from error

    async def start(self, *, warm_pool_size: int) -> None:
        if isinstance(warm_pool_size, bool) or not isinstance(warm_pool_size, int):
            raise TypeError("warm_pool_size must be an integer")
        await _call_state(
            self._state,
            "start",
            self._state.start(warm_pool_size=warm_pool_size),
        )

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        return cast(
            OpenSandboxOwnerClaim,
            await _call_state(
                self._state,
                "acquire_owner",
                self._state.acquire_owner(owner_key),
            ),
        )

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        return cast(
            bool,
            await _call_state(
                self._state,
                "renew_owner",
                self._state.renew_owner(claim),
            ),
        )

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
        *,
        purpose: OpenSandboxPurpose,
    ) -> OpenSandboxBinding:
        return cast(
            OpenSandboxBinding,
            await _call_state(
                self._state,
                "bind_owner",
                self._state.bind_owner(claim, sandbox_id, purpose=purpose),
            ),
        )

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        await _call_state(
            self._state,
            "unbind_owner",
            self._state.unbind_owner(claim),
        )

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        return cast(
            OpenSandboxBinding | None,
            await _call_state(
                self._state,
                "read_binding",
                self._state.read_binding(owner_key),
            ),
        )

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        await _call_state(
            self._state,
            "release_owner",
            self._state.release_owner(claim),
        )

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        return cast(
            OpenSandboxWarmClaim | None,
            await _call_state(
                self._state,
                "claim_warm_slot",
                self._state.claim_warm_slot(),
            ),
        )

    @property
    def supports_warm_pool_reconciliation(self) -> bool:
        """Return whether this State can fence and verify published warm slots."""

        return (
            callable(getattr(self._state, "claim_ready_warm_slot", None))
            and callable(getattr(self._state, "discard_ready_warm_slot", None))
            and callable(getattr(self._state, "warm_pool_ready", None))
        )

    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None:
        """Claim one published slot through the current reconciliation contract."""

        state = cast(_WarmPoolReconciliationState, self._state)
        return cast(
            OpenSandboxReadyWarmClaim | None,
            await _call_state(
                self._state,
                "claim_ready_warm_slot",
                state.claim_ready_warm_slot(exclude_slots=exclude_slots),
            ),
        )

    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None:
        """Invalidate one unusable published slot through its exact fence."""

        state = cast(_WarmPoolReconciliationState, self._state)
        await _call_state(
            self._state,
            "discard_ready_warm_slot",
            state.discard_ready_warm_slot(claim),
        )

    async def warm_pool_ready(self) -> bool:
        """Return whether every configured warm slot contains a published Sandbox."""

        state = cast(_WarmPoolReconciliationState, self._state)
        return cast(
            bool,
            await _call_state(
                self._state,
                "warm_pool_ready",
                state.warm_pool_ready(),
            ),
        )

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        await _call_state(
            self._state,
            "publish_warm",
            self._state.publish_warm(claim, sandbox_id),
        )

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        return cast(
            bool,
            await _call_state(
                self._state,
                "renew_warm",
                self._state.renew_warm(claim),
            ),
        )

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        await _call_state(
            self._state,
            "release_warm",
            self._state.release_warm(claim),
        )

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        return cast(
            OpenSandboxBinding | None,
            await _call_state(
                self._state,
                "consume_warm",
                self._state.consume_warm(claim),
            ),
        )

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        await _call_state(
            self._state,
            "enqueue_cleanup",
            self._state.enqueue_cleanup(sandbox_id),
        )

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        return cast(
            OpenSandboxCleanupClaim | None,
            await _call_state(
                self._state,
                "claim_cleanup",
                self._state.claim_cleanup(),
            ),
        )

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        return cast(
            bool,
            await _call_state(
                self._state,
                "renew_cleanup",
                self._state.renew_cleanup(claim),
            ),
        )

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        await _call_state(
            self._state,
            "complete_cleanup",
            self._state.complete_cleanup(claim),
        )

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        await _call_state(
            self._state,
            "release_cleanup",
            self._state.release_cleanup(claim),
        )

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return cast(
            tuple[str, ...],
            await _call_state(
                self._state,
                "shutdown_sandbox_ids",
                self._state.shutdown_sandbox_ids(),
            ),
        )

    async def aclose(self) -> None:
        await _call_state(self._state, "close", self._state.aclose())

    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        """Register before publishing a handle under the current running intent."""
        return cast(
            OpenSandboxAvailability,
            await _call_state(
                self._state,
                "register_holder",
                self._state.register_holder(claim, holder_id),
            ),
        )

    async def read_availability(self, owner_key: str) -> OpenSandboxAvailability | None:
        """Read current intent without waiting for an active owner claim."""
        return cast(
            OpenSandboxAvailability | None,
            await _call_state(
                self._state,
                "read_availability",
                self._state.read_availability(owner_key),
            ),
        )

    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        """Read all registrations and availability intents for one manager."""
        return cast(
            tuple[OpenSandboxHolderUpdate, ...],
            await _call_state(
                self._state,
                "get_holder_updates",
                self._state.get_holder_updates(holder_id),
            ),
        )

    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Advance the exact expected intent under the current owner fence."""
        return cast(
            OpenSandboxAvailability,
            await _call_state(
                self._state,
                "change_availability",
                self._state.change_availability(
                    claim, expected, phase=phase, refresh_connection=refresh_connection
                ),
            ),
        )

    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        """Acknowledge a current drain after admission closes and operations settle."""
        return cast(
            bool,
            await _call_state(
                self._state,
                "acknowledge_idle",
                self._state.acknowledge_idle(holder_id, availability),
            ),
        )

    async def holders_are_idle(
        self, claim: OpenSandboxOwnerClaim, availability: OpenSandboxAvailability
    ) -> bool:
        """Require explicit current-drain acknowledgements from every holder."""
        return cast(
            bool,
            await _call_state(
                self._state,
                "holders_are_idle",
                self._state.holders_are_idle(claim, availability),
            ),
        )

    async def unregister_holder(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> None:
        """Release an exact binding registration after proving local idle."""
        await _call_state(
            self._state,
            "unregister_holder",
            self._state.unregister_holder(holder_id, availability),
        )


@dataclass(slots=True)
class _MemoryOwnerRecord:
    lock: asyncio.Lock
    generation: int = 0
    binding: OpenSandboxBinding | None = None
    active_token: str | None = None
    availability: OpenSandboxAvailability | None = None


@dataclass(slots=True)
class _MemoryWarmSlot:
    slot: int
    generation: int = 0
    sandbox_id: str | None = None
    active_token: str | None = None


@dataclass(slots=True)
class _MemoryCleanupRecord:
    generation: int = 0
    active_token: str | None = None


class InMemoryOpenSandboxState(OpenSandboxState):
    """Keep owner allocation state inside one Python process."""

    def __init__(self, *, namespace: str = "") -> None:
        """Initialize process-local owner, warm-slot, and cleanup records."""

        self._namespace = namespace
        self._records: dict[str, _MemoryOwnerRecord] = {}
        self._records_guard = asyncio.Lock()
        self._warm_slots: list[_MemoryWarmSlot] = []
        self._warm_pool_size: int | None = None
        self._cleanup: dict[str, _MemoryCleanupRecord] = {}
        self._holders: dict[tuple[str, str], OpenSandboxHolderUpdate] = {}
        self._started = False
        self._closed = False

    @property
    def persistent(self) -> bool:
        """Process-local resources are not durable across manager shutdown."""
        return False

    @property
    def lease_renew_interval(self) -> None:
        """In-process claims do not expire while their State remains alive."""
        return None

    async def start(self, *, warm_pool_size: int) -> None:
        """Open the state; repeated calls require the original warm-pool capacity."""
        if isinstance(warm_pool_size, bool) or not isinstance(warm_pool_size, int):
            raise TypeError("warm_pool_size must be an integer")
        if warm_pool_size < 0:
            raise ValueError("warm_pool_size must not be negative")
        if self._closed:
            raise OpenSandboxStateError("OpenSandbox state is closed")
        if self._started:
            if self._warm_pool_size != warm_pool_size:
                raise OpenSandboxStateConfigurationError(
                    "OpenSandbox State is already started with a different "
                    "warm_pool_size"
                )
            return
        self._warm_slots = [
            _MemoryWarmSlot(slot=index) for index in range(warm_pool_size)
        ]
        self._warm_pool_size = warm_pool_size
        self._started = True

    def _ensure_open(self) -> None:
        if not self._started:
            raise OpenSandboxStateError("OpenSandbox state has not been started")
        if self._closed:
            raise OpenSandboxStateError("OpenSandbox state is closed")

    async def _record(self, owner_key: str) -> tuple[str, _MemoryOwnerRecord]:
        self._ensure_open()
        digest = _owner_digest(self._namespace, owner_key)
        async with self._records_guard:
            record = self._records.get(digest)
            if record is None:
                record = _MemoryOwnerRecord(lock=asyncio.Lock())
                self._records[digest] = record
        return digest, record

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Wait for and exclusively claim one owner's next transition."""
        digest, record = await self._record(owner_key)
        await record.lock.acquire()
        try:
            self._ensure_open()
            record.generation += 1
            token = uuid4().hex
            record.active_token = token
            return OpenSandboxOwnerClaim(
                owner_key=owner_key,
                owner_digest=digest,
                token=token,
                generation=record.generation,
                binding=record.binding,
            )
        except BaseException:
            record.lock.release()
            raise

    def _claimed_record(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> _MemoryOwnerRecord:
        self._ensure_open()
        record = self._records.get(claim.owner_digest)
        if (
            record is None
            or record.active_token != claim.token
            or record.generation != claim.generation
        ):
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )
        return record

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
        *,
        purpose: OpenSandboxPurpose,
    ) -> OpenSandboxBinding:
        """Commit a Sandbox ID only for the current fencing claim."""
        record = self._claimed_record(claim)
        return self._commit_binding(record, claim, sandbox_id, purpose=purpose)

    @staticmethod
    def _commit_binding(
        record: _MemoryOwnerRecord,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
        *,
        purpose: OpenSandboxPurpose,
    ) -> OpenSandboxBinding:
        """Publish binding and intent without yielding or invoking public overrides.

        Warm consumption commits its own binding atomically with slot removal.
        It must not call the replaceable standalone binding operation.
        """
        validate_purpose(purpose)
        if record.binding is not None:
            require_purpose(record.binding.purpose, purpose)
        binding = OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=claim.generation,
            purpose=purpose,
        )
        if record.binding == binding and record.availability is not None:
            return binding
        record.binding = binding
        record.availability = OpenSandboxAvailability(
            owner_digest=claim.owner_digest,
            sandbox_id=binding.sandbox_id,
            binding_generation=binding.generation,
            sequence=0,
            phase="running",
            connection_generation=0,
        )
        return binding

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Confirm that an in-process owner claim is still current."""
        try:
            self._claimed_record(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the latest committed binding after any active transition."""
        _, record = await self._record(owner_key)
        async with record.lock:
            return record.binding

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove the binding protected by the current owner claim."""
        record = self._claimed_record(claim)
        # Explicit unbinding retires all generations of this owner. Keeping an
        # older holder would resurrect an orphan registration on a later bind.
        self._holders = {
            key: holder
            for key, holder in self._holders.items()
            if holder.owner_digest != claim.owner_digest
        }
        record.binding = None
        record.availability = None

    @staticmethod
    def _holder_matches(
        holder: OpenSandboxHolderUpdate, availability: OpenSandboxAvailability
    ) -> bool:
        return (
            holder.owner_digest == availability.owner_digest
            and holder.sandbox_id == availability.sandbox_id
            and holder.binding_generation == availability.binding_generation
        )

    def _expected_availability(
        self, claim: OpenSandboxOwnerClaim, expected: OpenSandboxAvailability
    ) -> _MemoryOwnerRecord:
        record = self._claimed_record(claim)
        if record.availability != expected:
            raise OpenSandboxStateOwnershipError(
                "Sandbox availability is no longer current"
            )
        return record

    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        """Register a manager atomically with the current running binding."""
        _validate_holder_id(holder_id)
        record = self._claimed_record(claim)
        availability = record.availability
        if availability is None or availability.phase != "running":
            raise OpenSandboxStateOwnershipError("Sandbox binding is not running")
        self._holders[(holder_id, claim.owner_digest)] = OpenSandboxHolderUpdate(
            holder_id=holder_id,
            owner_digest=claim.owner_digest,
            sandbox_id=availability.sandbox_id,
            binding_generation=availability.binding_generation,
            acknowledged_sequence=None,
            availability=availability,
        )
        return availability

    async def read_availability(self, owner_key: str) -> OpenSandboxAvailability | None:
        """Read committed intent without acquiring the owner's transition lock."""
        self._ensure_open()
        record = self._records.get(_owner_digest(self._namespace, owner_key))
        return None if record is None else record.availability

    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        """Read this manager's holder identities and each owner's latest intent."""
        self._ensure_open()
        updates: list[OpenSandboxHolderUpdate] = []
        for (registered_holder, digest), holder in self._holders.items():
            if registered_holder != holder_id:
                continue
            record = self._records.get(digest)
            if record is not None and record.availability is not None:
                updates.append(replace(holder, availability=record.availability))
        return tuple(sorted(updates, key=lambda holder: holder.owner_digest))

    def _all_holders_idle(self, availability: OpenSandboxAvailability) -> bool:
        return all(
            holder.acknowledged_sequence == availability.sequence
            for holder in self._holders.values()
            if self._holder_matches(holder, availability)
        )

    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Advance an exact intent without yielding between fencing and mutation."""
        record = self._expected_availability(claim, expected)
        updated = _next_availability(
            expected, phase=phase, refresh_connection=refresh_connection
        )
        if phase == "pausing" and not self._all_holders_idle(expected):
            raise OpenSandboxStateOwnershipError(
                "Sandbox holders have not all acknowledged idle"
            )
        record.availability = updated
        return updated

    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        """Record explicit drain evidence without waiting for the owner claim."""
        self._ensure_open()
        record = self._records.get(availability.owner_digest)
        key = (holder_id, availability.owner_digest)
        holder = self._holders.get(key)
        if (
            availability.phase != "draining"
            or record is None
            or record.availability != availability
            or holder is None
            or not self._holder_matches(holder, availability)
        ):
            return False
        self._holders[key] = replace(
            holder, acknowledged_sequence=availability.sequence
        )
        return True

    async def holders_are_idle(
        self, claim: OpenSandboxOwnerClaim, availability: OpenSandboxAvailability
    ) -> bool:
        """Require explicit ACKs for the exact current drain and owner fence."""
        self._expected_availability(claim, availability)
        if availability.phase != "draining":
            raise OpenSandboxStateOwnershipError("Sandbox availability is not draining")
        return self._all_holders_idle(availability)

    async def unregister_holder(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> None:
        """Remove only the exact binding registration after local idle is proven."""
        self._ensure_open()
        key = (holder_id, availability.owner_digest)
        holder = self._holders.get(key)
        if holder is not None and self._holder_matches(holder, availability):
            del self._holders[key]

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release a current claim; stale releases cannot unlock a successor."""
        record = self._records.get(claim.owner_digest)
        if record is None or record.active_token != claim.token:
            return
        record.active_token = None
        record.lock.release()

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty global slot without waiting for remote creation."""
        self._ensure_open()
        for slot in self._warm_slots:
            if slot.sandbox_id is None and slot.active_token is None:
                slot.generation += 1
                token = uuid4().hex
                slot.active_token = token
                return OpenSandboxWarmClaim(
                    slot=slot.slot,
                    token=token,
                    generation=slot.generation,
                )
        return None

    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None:
        """Claim one published slot so its remote resource can be renewed or replaced."""

        self._ensure_open()
        excluded = set(exclude_slots)
        for slot in self._warm_slots:
            if (
                slot.slot in excluded
                or slot.sandbox_id is None
                or slot.active_token is not None
            ):
                continue
            slot.generation += 1
            token = uuid4().hex
            slot.active_token = token
            return OpenSandboxReadyWarmClaim(
                slot=slot.slot,
                token=token,
                generation=slot.generation,
                sandbox_id=slot.sandbox_id,
            )
        return None

    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None:
        """Clear one unusable published slot and retain its cleanup obligation."""

        slot = self._claimed_warm_slot(claim)
        if slot.sandbox_id != claim.sandbox_id:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} no longer contains the claimed Sandbox"
            )
        slot.sandbox_id = None
        slot.active_token = None
        self._cleanup.setdefault(claim.sandbox_id, _MemoryCleanupRecord())

    async def warm_pool_ready(self) -> bool:
        """Return whether every process-local warm slot retains a published Sandbox."""

        self._ensure_open()
        return all(slot.sandbox_id is not None for slot in self._warm_slots)

    def _claimed_warm_slot(
        self,
        claim: OpenSandboxWarmClaim,
    ) -> _MemoryWarmSlot:
        self._ensure_open()
        try:
            slot = self._warm_slots[claim.slot]
        except IndexError as exc:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            ) from exc
        if slot.active_token != claim.token or slot.generation != claim.generation:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            )
        return slot

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a created Sandbox into its claimed global warm slot."""
        slot = self._claimed_warm_slot(claim)
        slot.sandbox_id = sandbox_id
        slot.active_token = None

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Confirm that an in-process warm claim is still current."""
        try:
            self._claimed_warm_slot(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release an unfilled warm claim without affecting a successor."""
        if claim.slot >= len(self._warm_slots):
            return
        slot = self._warm_slots[claim.slot]
        if slot.active_token == claim.token:
            slot.active_token = None

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume and authoritatively bind one ready warm Sandbox."""
        record = self._claimed_record(claim)
        if record.binding is not None:
            require_purpose(record.binding.purpose, "commands")
        for slot in self._warm_slots:
            if slot.sandbox_id is None or slot.active_token is not None:
                continue
            binding = self._commit_binding(
                record, claim, slot.sandbox_id, purpose="commands"
            )
            slot.sandbox_id = None
            return binding
        return None

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Persist an idempotent orphan cleanup target."""
        self._ensure_open()
        self._cleanup.setdefault(sandbox_id, _MemoryCleanupRecord())

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending cleanup target without blocking."""
        self._ensure_open()
        for sandbox_id, record in self._cleanup.items():
            if record.active_token is not None:
                continue
            record.generation += 1
            token = uuid4().hex
            record.active_token = token
            return OpenSandboxCleanupClaim(
                sandbox_id=sandbox_id,
                token=token,
                generation=record.generation,
            )
        return None

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Confirm that an in-process cleanup claim is still current."""
        try:
            self._claimed_cleanup(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    def _claimed_cleanup(
        self,
        claim: OpenSandboxCleanupClaim,
    ) -> _MemoryCleanupRecord:
        self._ensure_open()
        record = self._cleanup.get(claim.sandbox_id)
        if (
            record is None
            or record.active_token != claim.token
            or record.generation != claim.generation
        ):
            raise OpenSandboxStateOwnershipError(
                f"Cleanup claim for {claim.sandbox_id!r} is no longer current"
            )
        return record

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Remove a cleanup target after confirmed remote destruction."""
        self._claimed_cleanup(claim)
        self._cleanup.pop(claim.sandbox_id, None)

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release a failed cleanup target for a later retry."""
        record = self._cleanup.get(claim.sandbox_id)
        if record is not None and record.active_token == claim.token:
            record.active_token = None

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Return every remote resource exclusively owned by this memory State."""
        self._ensure_open()
        sandbox_ids = {
            record.binding.sandbox_id
            for record in self._records.values()
            if record.binding is not None
        }
        sandbox_ids.update(
            slot.sandbox_id for slot in self._warm_slots if slot.sandbox_id is not None
        )
        sandbox_ids.update(self._cleanup)
        return tuple(sorted(sandbox_ids))

    async def aclose(self) -> None:
        """Close this process-local state idempotently."""
        self._closed = True
