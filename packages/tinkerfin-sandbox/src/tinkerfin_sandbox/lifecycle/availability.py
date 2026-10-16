"""Shared Sandbox availability and explicit handle-holder drain evidence."""

from __future__ import annotations

__all__ = [
    "OpenSandboxAvailability",
    "OpenSandboxAvailabilityPhase",
    "OpenSandboxHolderUpdate",
    "_next_availability",
    "_validate_holder_id",
]

from dataclasses import dataclass, replace
from typing import Literal

from ..errors import OpenSandboxStateOwnershipError

OpenSandboxAvailabilityPhase = Literal[
    "running", "draining", "pausing", "paused", "resuming", "uncertain"
]


@dataclass(frozen=True, slots=True)
class OpenSandboxAvailability:
    """Authoritative availability intent for exactly one owner binding.

    ``sequence`` advances on every intent transition. ``connection_generation``
    advances when holders must reconnect before accepting operations. Both counters
    are scoped to ``binding_generation``; an earlier binding cannot acknowledge or
    change a successor, even if the remote identifier is reused.
    """

    owner_digest: str
    sandbox_id: str
    binding_generation: int
    sequence: int
    phase: OpenSandboxAvailabilityPhase
    connection_generation: int


@dataclass(frozen=True, slots=True)
class OpenSandboxHolderUpdate:
    """One registered holder and its owner's current availability snapshot.

    The holder's binding fields may differ from ``availability`` after replacement.
    Such a holder cannot acknowledge the new binding; normal access reconciles its
    handle. ``acknowledged_sequence`` is evidence
    that local admission is closed and all previously admitted operations settled;
    worker expiry or shutdown never supplies this evidence.
    """

    holder_id: str
    owner_digest: str
    sandbox_id: str
    binding_generation: int
    acknowledged_sequence: int | None
    availability: OpenSandboxAvailability


def _next_availability(
    expected: OpenSandboxAvailability,
    *,
    phase: OpenSandboxAvailabilityPhase,
    refresh_connection: bool,
) -> OpenSandboxAvailability:
    transitions: dict[
        OpenSandboxAvailabilityPhase, set[OpenSandboxAvailabilityPhase]
    ] = {
        "running": {"draining", "resuming"},
        "draining": {"running", "paused", "pausing"},
        "pausing": {"paused", "running", "uncertain"},
        "paused": {"resuming", "draining"},
        "resuming": {"running", "paused", "uncertain"},
        "uncertain": set(),
    }
    if phase not in transitions[expected.phase]:
        raise OpenSandboxStateOwnershipError(
            f"Sandbox availability cannot change from {expected.phase} to {phase}"
        )
    if refresh_connection and phase != "running":
        raise ValueError("refresh_connection requires the running phase")
    return replace(
        expected,
        phase=phase,
        sequence=expected.sequence + 1,
        connection_generation=expected.connection_generation + int(refresh_connection),
    )


def _validate_holder_id(holder_id: str) -> None:
    if not holder_id or len(holder_id) > 36:
        raise ValueError("holder_id must contain between 1 and 36 characters")
