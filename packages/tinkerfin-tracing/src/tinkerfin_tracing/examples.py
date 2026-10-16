"""Protocol-neutral example Projection for validating extension behavior."""

from __future__ import annotations

from pydantic import Field

from ._models import TraceModel
from .facts import TraceSemanticFact


class FactCountState(TraceModel, frozen=True):
    """Internal deterministic counts keyed by semantic fact kind."""

    counts: dict[str, int] = Field(default_factory=dict)


class FactCountResult(TraceModel, frozen=True):
    """Public semantic fact counts for one selected head lineage."""

    counts: dict[str, int]


class FactCountProjection:
    """Count semantic fact categories without knowing any host application."""

    name = "fact_counts"
    state_type = FactCountState
    result_type = FactCountResult

    def initial_state(self) -> FactCountState:
        """Return empty semantic fact counts."""

        return FactCountState()

    def apply(
        self,
        state: FactCountState,
        fact: TraceSemanticFact,
    ) -> FactCountState:
        """Increment the category represented by one stable semantic fact."""

        counts = dict(state.counts)
        counts[fact.kind] = counts.get(fact.kind, 0) + 1
        return FactCountState(counts=counts)

    def finish(self, state: FactCountState) -> FactCountResult:
        """Expose an immutable copy of the accumulated counts."""

        return FactCountResult(counts=dict(state.counts))


__all__ = ["FactCountProjection", "FactCountResult", "FactCountState"]
