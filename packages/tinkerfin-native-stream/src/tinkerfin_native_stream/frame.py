"""Canonical per-part output shared by all TinkerFin stream consumers."""

from __future__ import annotations

from dataclasses import dataclass

from tinkerfin_contracts import GraphOrigin, NativeObservation, SubagentRequestReference

from .serialization import NativeStreamPart
from .stream import NativeValidatedStreamPart


@dataclass(frozen=True, slots=True)
class NativeStreamFrame:
    """Bind one upstream part to its single canonical normalization result.

    Runtime observers consume ``observations`` while protocol adapters, replay, and
    native SSE consume the same frame rather than parsing the upstream object again.
    ``canonical`` is TinkerFin's live object contract; it is not a project-level
    upstream version selector. A concrete Driver owns the mapping from its third-party
    stream profile into this contract.

    Attributes:
        canonical: Validated envelope containing borrowed live objects or normalized
            message copies. Normalization never changes the upstream message.
            Consumers must treat it as read-only; nested objects are not frozen.
        observations: Ordered protocol-neutral facts emitted for Runtime observers.
        replay: Detached finite representation used by native persistence and SSE.
        root_interrupt_ids: Root interrupt IDs observed at this exact part boundary.
        origin: Proven physical opening task and logical owner of this part.
        subagent_requests: Logical delegations declared by this exact task start.
        internal: Whether this frame carries a framework-owned private record that
            must not reach public observers, event conversion, or transport.
    """

    canonical: NativeValidatedStreamPart
    observations: tuple[NativeObservation, ...]
    replay: NativeStreamPart
    root_interrupt_ids: tuple[str, ...] = ()
    origin: GraphOrigin = GraphOrigin()
    subagent_requests: tuple[SubagentRequestReference, ...] = ()
    internal: bool = False


__all__ = ["NativeStreamFrame"]
