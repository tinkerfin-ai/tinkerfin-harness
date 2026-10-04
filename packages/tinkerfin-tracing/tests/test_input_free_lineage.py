"""Input-free writers expose execution without inventing input or ancestry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Literal, TypedDict

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    CallTrackingFact,
    CapturedValue,
    ContextContributionFact,
    InMemoryTraceStore,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    NativeExtraFact,
    PlanRevisionFact,
    ReasoningFact,
    RunFact,
    StateRevisionFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceCorruption,
    TraceEvent,
    TraceGraphDelta,
    TraceGraphNodeStatus,
    Tracer,
    TraceSemanticFact,
    TraceStoreUpdate,
    TraceThreadKey,
    TraceUpdate,
    TurnFact,
)
from tinkerfin_tracing.projection import (
    CoreProjectionState,
    advance_core_projection_state,
    empty_core_projection_state,
    project_core,
    project_core_checkpoint,
)

_NOW = datetime(2026, 10, 4, tzinfo=UTC)
_FIRST = RunIdentity(namespace="test", thread_id="input-free", run_id="first")
_SECOND = _FIRST.model_copy(update={"run_id": "second"})
_CAPTURED = CapturedValue(disposition="inline", safe_size_bytes=2, value={})


class _Source(TypedDict):
    identity: RunIdentity
    source_observation_id: str
    occurred_at: datetime
    monotonic_ns: int


def _source(identity: RunIdentity, name: str) -> _Source:
    return {
        "identity": identity,
        "source_observation_id": f"{identity.run_id}:{name}",
        "occurred_at": _NOW,
        "monotonic_ns": 1,
    }


def _run(
    identity: RunIdentity,
    phase: Literal["started", "input", "terminal"],
    *,
    parent: str | None = None,
) -> RunFact:
    return RunFact(
        **_source(identity, phase),
        phase=phase,
        input_kind=None if phase == "terminal" else "ordinary",
        parent_run_id=parent,
        outcome="succeeded" if phase == "terminal" else None,
        input=_CAPTURED if phase == "input" else None,
        config=_CAPTURED if phase == "input" else None,
    )


def _tool(identity: RunIdentity) -> ToolFact:
    return ToolFact(
        **_source(identity, "tool"),
        phase="started",
        tool_call_id=f"{identity.run_id}:tool",
        source_tool_call_id=f"{identity.run_id}:tool",
        tool_name="lookup",
    )


def _events(facts: tuple[TraceSemanticFact, ...]) -> tuple[TraceEvent, ...]:
    return tuple(
        TraceEvent(
            event_id=f"event:{sequence}",
            trace_seq=sequence,
            generation="generation",
            fact=fact,
            persisted_bytes=len(fact.model_dump_json().encode()),
        )
        for sequence, fact in enumerate(facts, start=1)
    )


def _checkpoint(events: tuple[TraceEvent, ...], split: int) -> CoreProjectionState:
    state = advance_core_projection_state(empty_core_projection_state(), events[:split])
    restored = CoreProjectionState.model_validate_json(state.model_dump_json())
    return advance_core_projection_state(restored, events[split:])


def _completed_parent() -> tuple[TraceSemanticFact, ...]:
    return (
        _run(_FIRST, "started"),
        TurnFact(**_source(_FIRST, "turn"), turn_id="first-turn"),
        StateRevisionFact(
            **_source(_FIRST, "state"),
            revision_id="first-state",
            changes=CapturedValue(
                disposition="inline", safe_size_bytes=15, value={"preserved": 1}
            ),
        ),
        MessageFact(
            **_source(_FIRST, "message"),
            phase="completed",
            message_id="first-message",
            role="assistant",
            content=CapturedValue(
                disposition="inline", safe_size_bytes=7, value="first"
            ),
        ),
        _run(_FIRST, "terminal"),
    )


@pytest.mark.parametrize(
    "execution",
    (
        _tool(_SECOND),
        MessageFact(
            **_source(_SECOND, "message"),
            phase="content",
            message_id="second-message",
            role="assistant",
            content=_CAPTURED,
        ),
        ReasoningFact(
            **_source(_SECOND, "reasoning"),
            phase="content",
            reasoning_id="reasoning",
            message_id="second-message",
            source_message_id="second-message",
            extractor="fixture",
            content=_CAPTURED,
        ),
        StateRevisionFact(
            **_source(_SECOND, "state"),
            revision_id="second-state",
            changes=CapturedValue(
                disposition="inline", safe_size_bytes=11, value={"added": 2}
            ),
        ),
        InteractionFact(
            **_source(_SECOND, "interaction"),
            phase="opened",
            interaction_id="interaction",
            source_interaction_id="interaction",
            interaction_kind="approval",
            status="pending",
        ),
        SubagentFact(
            **_source(_SECOND, "subagent"),
            phase="started",
            subagent_id="subagent",
            status="running",
        ),
        PlanRevisionFact(
            **_source(_SECOND, "plan"), revision_id="plan", plan=_CAPTURED
        ),
        NativeExtraFact(**_source(_SECOND, "extra"), mode="custom", data_type="dict"),
        ModelCallFact(
            **_source(_SECOND, "model"),
            phase="started",
            call_id="model",
            request=_CAPTURED,
            context_started_at=_NOW,
            system_message_positions=(),
            output_message_ids=(),
        ),
        ToolExecutionFact(
            **_source(_SECOND, "execution"),
            phase="started",
            execution_id="execution",
            tool_name="lookup",
            input=_CAPTURED,
        ),
        ContextContributionFact(
            **_source(_SECOND, "contribution"),
            phase="started",
            contribution_id="contribution",
            context_kind="retrieval",
            name="lookup",
        ),
    ),
    ids=lambda fact: fact.kind,
)
def test_first_execution_binds_completed_parent_before_folding_body(
    execution: TraceSemanticFact,
) -> None:
    events = _events(
        (
            *_completed_parent(),
            _run(_SECOND, "started"),
            CallTrackingFact(**_source(_SECOND, "tracking")),
            execution,
        )
    )
    prepared = advance_core_projection_state(empty_core_projection_state(), events[:-1])
    assert prepared.heads == ("first", "second")
    assert not prepared.runs["second"].lineage_bound
    assert prepared.runs["second"].parent_run_id is None
    with pytest.raises(AmbiguousTraceHead):
        project_core(
            events[:-1], head_run_id=None, turn_limit=100, active_run_ids=("second",)
        )

    baseline = project_core(
        events, head_run_id=None, turn_limit=100, active_run_ids=("second",)
    )
    assert baseline.available_heads == ("second",)
    assert baseline.selected_run_ids == {"first", "second"}
    assert baseline.state.root["preserved"] == 1
    for split in range(len(events) + 1):
        state = _checkpoint(events, split)
        assert state.runs["second"].lineage_bound
        assert state.runs["second"].parent_run_id == "first"
        assert (
            project_core_checkpoint(
                state, head_run_id=None, turn_limit=100, active_run_ids=("second",)
            )
            == baseline
        )


@pytest.mark.parametrize("parent_finishes", ["before_execution", "after_execution"])
def test_concurrent_completion_cannot_absorb_an_input_free_branch(
    parent_finishes: str,
) -> None:
    facts: tuple[TraceSemanticFact, ...] = (
        _run(_FIRST, "started"),
        _run(_SECOND, "started"),
        CallTrackingFact(**_source(_SECOND, "tracking")),
    )
    ending = _run(_FIRST, "terminal")
    body = _tool(_SECOND)
    facts += (ending, body) if parent_finishes == "before_execution" else (body, ending)
    events = _events((*facts, _run(_SECOND, "terminal")))
    for prefix in (events[:-1], events):
        with pytest.raises(AmbiguousTraceHead):
            project_core(
                prefix, head_run_id=None, turn_limit=100, active_run_ids=("second",)
            )
        baseline = project_core(
            prefix, head_run_id="second", turn_limit=100, active_run_ids=("second",)
        )
        for split in range(len(prefix) + 1):
            state = _checkpoint(prefix, split)
            assert state.heads == ("first", "second")
            assert state.runs["second"].lineage_bound
            assert state.runs["second"].parent_run_id is None
            with pytest.raises(AmbiguousTraceHead):
                project_core_checkpoint(
                    state,
                    head_run_id=None,
                    turn_limit=100,
                    active_run_ids=("second",),
                )
            projected = project_core_checkpoint(
                state, head_run_id="second", turn_limit=100, active_run_ids=("second",)
            )
            assert projected.selected_run_ids == baseline.selected_run_ids == {"second"}
            assert projected.status == baseline.status


@pytest.mark.parametrize("explicit_start", [False, True])
@pytest.mark.parametrize("evidence_kind", ["input", "turn"])
@pytest.mark.parametrize("conflicting", [False, True])
def test_late_explicit_parent_preserves_the_execution_lineage_contract(
    explicit_start: bool, evidence_kind: str, conflicting: bool
) -> None:
    parent = "other" if conflicting else "first"
    evidence = (
        _run(_SECOND, "input", parent=parent)
        if evidence_kind == "input"
        else TurnFact(
            **_source(_SECOND, "turn"), turn_id="second-turn", parent_run_id=parent
        )
    )
    events = _events(
        (
            *_completed_parent(),
            _run(_SECOND, "started", parent="first" if explicit_start else None),
            _tool(_SECOND),
            evidence,
        )
    )
    if conflicting:
        with pytest.raises(TraceCorruption, match="parent changed"):
            project_core(
                events, head_run_id="second", turn_limit=100, active_run_ids=("second",)
            )
        for split in range(len(events) + 1):
            with pytest.raises(TraceCorruption, match="parent changed"):
                _checkpoint(events, split)
    else:
        baseline = project_core(
            events, head_run_id=None, turn_limit=100, active_run_ids=("second",)
        )
        for split in range(len(events) + 1):
            state = _checkpoint(events, split)
            assert state.runs["second"].parent_run_id == "first"
            assert (
                project_core_checkpoint(
                    state, head_run_id=None, turn_limit=100, active_run_ids=("second",)
                )
                == baseline
            )


@pytest.mark.parametrize("view_kind", ["history", "graph"])
@pytest.mark.parametrize("overlapping", [False, True])
async def test_active_input_free_writer_is_queryable_and_followable(
    monkeypatch: pytest.MonkeyPatch, view_kind: str, overlapping: bool
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    first_writer = await store.open_writer(_FIRST)
    second_writer = await store.open_writer(_SECOND)
    await first_writer.append((_run(_FIRST, "started"),))
    if not overlapping:
        await first_writer.append((_run(_FIRST, "terminal"),), mandatory=True)
        await first_writer.aclose()
    view = (
        await tracer.get(_FIRST.thread)
        if view_kind == "history"
        else await tracer.query(_FIRST.thread)
    )
    source_follow = store.follow
    processed: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    source_closed = asyncio.Event()

    async def followed(
        key: TraceThreadKey, *, after_seq: int
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        source = source_follow(key, after_seq=after_seq)
        try:
            async for update in source:
                yield update
                if update.events:
                    processed.put_nowait(None)
        finally:
            await source.aclose()
            source_closed.set()

    monkeypatch.setattr(store, "follow", followed)
    follower = view.follow()
    pending = asyncio.create_task(anext(follower))

    async def consume_fact(*, resolves: bool) -> None:
        acknowledged = asyncio.create_task(processed.get())
        try:
            await asyncio.wait(
                (acknowledged, pending), return_when=asyncio.FIRST_COMPLETED
            )
            assert pending.done() is resolves
        finally:
            acknowledged.cancel()
            await asyncio.gather(acknowledged, return_exceptions=True)

    try:
        started = await second_writer.append((_run(_SECOND, "started"),))
        await consume_fact(resolves=False)
        tracking = await second_writer.append(
            (CallTrackingFact(**_source(_SECOND, "tracking")),)
        )
        await consume_fact(resolves=False)
        if overlapping:
            await first_writer.append((_run(_FIRST, "terminal"),), mandatory=True)
            await consume_fact(resolves=False)
        execution = await second_writer.append((_tool(_SECOND),))
        await consume_fact(resolves=True)
        if overlapping:
            with pytest.raises(AmbiguousTraceHead):
                await pending
            with pytest.raises(AmbiguousTraceHead):
                await tracer.get(_FIRST.thread)
            with pytest.raises(AmbiguousTraceHead):
                await tracer.query(_FIRST.thread)
        else:
            update = await pending
            assert update.as_of_seq == execution[-1].trace_seq
            if isinstance(update, TraceUpdate):
                assert update.events == (*started, *tracking, *execution)
                assert update.tool_call_count == 1
            else:
                assert isinstance(update, TraceGraphDelta)
                assert {node.run_id for node in update.node_upserts} == {"second"}
            history = await tracer.get(_FIRST.thread)
            graph = await tracer.query(_FIRST.thread)
            assert history.head_run_id == "second"
            assert history.tool_call_count == 1
            assert history.status.execution == "running"
            assert graph.as_of_seq == history.as_of_seq == execution[-1].trace_seq
            assert {node.run_id for node in graph.nodes} == {"second"}
            assert all(
                node.status is TraceGraphNodeStatus.WAITING for node in graph.nodes
            )
            assert (await store.snapshot(_FIRST.thread)).active_run_ids == ("second",)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
        await second_writer.aclose()
        await first_writer.aclose()
    assert source_closed.is_set()
