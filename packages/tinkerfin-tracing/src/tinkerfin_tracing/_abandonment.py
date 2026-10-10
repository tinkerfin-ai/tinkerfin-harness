"""Strict evidence checks for non-executing, read-only Run replay."""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from tinkerfin_contracts import (
    ObservationBoundary,
    RunClosedObservation,
    RunInputObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RuntimeObservation,
)

from .errors import TraceRunConflict
from .facts import (
    CallTrackingFact,
    InteractionFact,
    RunFact,
    SubagentFact,
    ToolFact,
    TraceEvent,
    TraceSemanticFact,
)


def _nonexecuting_fact(
    fact: TraceSemanticFact, terminal: RunFact, ids: tuple[str, ...]
) -> bool:
    if isinstance(fact, RunFact | CallTrackingFact):
        return True
    if isinstance(fact, InteractionFact):
        return (
            fact.phase == "resolved"
            and fact.status == "cancelled"
            and fact.source_interaction_id in ids
        )
    # Abandonment also settles proposals inherited from the interrupted Run. Those
    # facts share the terminal observation and do not represent new Tool execution.
    if fact.source_observation_id != terminal.source_observation_id:
        return False
    return (isinstance(fact, ToolFact) and fact.phase == "abandoned") or (
        isinstance(fact, SubagentFact)
        and fact.phase == "completed"
        and fact.status == "abandoned"
    )


def cancelled_batch(fact: RunFact) -> tuple[str, ...]:
    """Return a complete retained cancellation batch, or no proof."""

    if (
        fact.phase != "resumed"
        or fact.input_kind != "abandon"
        or fact.input is None
        or fact.input.disposition != "inline"
        or not isinstance(fact.input.value, list)
    ):
        return ()
    try:
        summaries = tuple(
            RunResumeSummary.model_validate(item) for item in fact.input.value
        )
    except ValidationError:
        return ()
    ids = tuple(sorted(item.interrupt_id for item in summaries))
    if (
        not ids
        or len(set(ids)) != len(ids)
        or ids != tuple(sorted(fact.interrupt_ids))
        or any(
            item.status != "cancelled" or item.decision is not None
            for item in summaries
        )
    ):
        return ()
    return ids


def completed_abandonment(
    events: tuple[TraceEvent, ...],
) -> tuple[RunFact, RunFact] | None:
    """Return the input and terminal only when a complete Run proves no execution."""

    runs = [event.fact for event in events if isinstance(event.fact, RunFact)]
    if (
        [fact.phase for fact in runs] != ["started", "resumed", "terminal", "closed"]
        or runs[0].input_kind != "abandon"
        or not (ids := cancelled_batch(runs[1]))
        or runs[2].outcome != "abandoned"
        or runs[2].error_type is not None
        or runs[3].outcome != "abandoned"
        or any(not _nonexecuting_fact(event.fact, runs[2], ids) for event in events)
    ):
        return None
    return runs[1], runs[2]


def nonexecuting_settlements(
    events: tuple[TraceEvent, ...],
) -> frozenset[tuple[str, str]]:
    """Identify synthetic proposal settlements without discarding actual results.

    A cancelled approval has no Native work. Its terminal can still display
    inherited Tools and Subagents as abandoned. Those facts must not settle the
    interrupted Graph's execution state when a later Run answers the same review.
    Real execution, incomplete evidence, and other cancellation kinds retain their
    ordinary hydration semantics.
    """

    candidates: dict[str, list[TraceEvent]] = {
        event.fact.identity.run_id: []
        for event in events
        if isinstance(event.fact, RunFact) and cancelled_batch(event.fact)
    }
    for event in events:
        history = candidates.get(event.fact.identity.run_id)
        if history is not None:
            history.append(event)
    return frozenset(
        (evidence[1].identity.run_id, evidence[1].source_observation_id)
        for history in candidates.values()
        if (evidence := completed_abandonment(tuple(history))) is not None
    )


class AbandonmentReplay:
    """Validate a completed cancellation and observe its retry without a writer.

    Archived input, terminal, and close must independently prove abandonment. The
    session observes the current lifecycle; it never certifies prior host cleanup.
    """

    def __init__(
        self, context: RunSourceContext, events: tuple[TraceEvent, ...]
    ) -> None:
        evidence = completed_abandonment(events)
        if evidence is None:
            raise TraceRunConflict("Run has no complete matching abandonment evidence")
        source, terminal = evidence
        ids = tuple(sorted(item.interrupt_id for item in context.resume))
        if (
            context.input_kind != "abandon"
            or not ids
            or any(item.status != "cancelled" for item in context.resume)
            or cancelled_batch(source) != ids
            or source.parent_run_id != context.parent_run_id
        ):
            raise TraceRunConflict("Run has no complete matching abandonment evidence")
        self._context = context
        self._code = terminal.code
        self._next = 0
        self._failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )

    async def observe(self, observation: RuntimeObservation) -> None:
        """Accept only the same non-executing lifecycle in its original order."""

        expected = (
            RunStartedObservation,
            RunInputObservation,
            RunTerminalObservation,
            RunClosedObservation,
        )
        if (
            self._next >= len(expected)
            or not isinstance(observation, expected[self._next])
            or observation.identity != self._context.identity
        ):
            raise TraceRunConflict("Abandonment replay changed its lifecycle")
        if isinstance(observation, RunInputObservation):
            source = observation.source
            if (
                source.input_kind != "abandon"
                or source.parent_run_id != self._context.parent_run_id
                or source.resume != self._context.resume
                or source.runtime_profile != self._context.runtime_profile
            ):
                raise TraceRunConflict("Abandonment replay changed its input")
        if isinstance(observation, RunTerminalObservation) and (
            observation.outcome != "abandoned"
            or observation.code != self._code
            or observation.error_type is not None
        ):
            raise TraceRunConflict("Abandonment replay did not complete")
        if (
            isinstance(observation, RunClosedObservation)
            and observation.outcome != "abandoned"
        ):
            raise TraceRunConflict("Abandonment replay did not close normally")
        self._next += 1

    async def force(self, boundary: ObservationBoundary) -> None:
        """Check the close boundary without committing additional facts."""

        if boundary == ObservationBoundary.CLOSE and self._next != 4:
            raise TraceRunConflict("Abandonment replay is incomplete")

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        """Expose the request-owned waiter required by Runtime observation."""

        return self._failure

    async def aclose(self) -> None:
        """Release the waiter even when this retry was interrupted or failed."""

        self._failure.cancel()
