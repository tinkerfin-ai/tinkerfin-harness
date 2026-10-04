"""Keep followers open only while active writers can determine new Run lineage."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .projection import CoreProjectionState


class PendingTraceLineage(Exception):
    """Pause an unscoped follower until its new heads have determined ancestry."""


def head_selection_pending(
    state: CoreProjectionState, active_run_ids: tuple[str, ...]
) -> bool:
    """Wait only while active writers can resolve an ambiguous head set.

    Input, Turn, execution, or terminal facts can settle ancestry. A writer that
    closes before settling it cannot resolve it later and must not keep followers
    waiting.
    """

    if len(state.heads) < 2:
        return False
    fixed_heads = sum(
        state.runs[run_id].lineage_bound
        or state.runs[run_id].parent_run_id is not None
        or state.runs[run_id].terminal is not None
        or run_id not in active_run_ids
        for run_id in state.heads
    )
    return fixed_heads < 2
