"""Failed Tool attempts retain history without contaminating a later execution."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    InMemoryTraceStore,
    RunFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceSemanticFact,
)
from tinkerfin_tracing._graph_projection import (
    project_trace_graph_node,
    reduce_trace_graph_records,
)
from tinkerfin_tracing._graph_reducer import (
    ReducedTraceGraphRevision,
    apply_graph_node_mutation,
    effective_graph_nodes,
    graph_node_mutations,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore
from tinkerfin_tracing.store import TraceWriter

NOW = datetime(2026, 9, 13, tzinfo=UTC)
Outcome = Literal["completed", "failed", "cancelled"]


def _capture(value: str) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(json.dumps(value).encode()),
        value=value,
    )


def _events(
    outcome: Outcome,
    *,
    same_run: bool,
    nested: bool,
    owns_failure: bool,
) -> tuple[TraceEvent, ...]:
    namespace = ("tools:research",) if nested else ()
    first = RunIdentity(namespace="test", thread_id="recovery", run_id="failed")
    retried = first if same_run else first.model_copy(update={"run_id": "retry"})
    facts: list[TraceSemanticFact] = []
    executions: tuple[tuple[RunIdentity, Literal["started"] | Outcome], ...] = (
        (first, "started"),
        (first, "failed"),
        (retried, "started"),
        (retried, outcome),
    )
    for index, (identity, phase) in enumerate(executions, start=1):
        facts.append(
            ToolExecutionFact(
                source_observation_id=f"observation-{index}",
                identity=identity,
                graph_namespace=namespace,
                occurred_at=NOW + timedelta(seconds=index),
                monotonic_ns=index,
                phase=phase,
                execution_id="first" if index < 3 else "second",
                source_tool_call_id="report",
                tool_name="deliver_report",
                input=_capture(f"input-{index}") if phase == "started" else None,
                output=_capture("delivered") if phase == "completed" else None,
                error_type="ValueError" if phase == "failed" else None,
                error_message=_capture(f"failure-{index}")
                if phase == "failed"
                else None,
                failure_origin=phase == "failed" and (index == 2 or owns_failure),
            )
        )
    if outcome != "cancelled":
        facts.append(
            ToolFact(
                source_observation_id="native-result",
                identity=retried,
                graph_namespace=namespace,
                occurred_at=NOW + timedelta(seconds=5),
                monotonic_ns=5,
                phase="result",
                tool_call_id=scope_id("tool", namespace, "report"),
                source_tool_call_id="report",
                tool_name="deliver_report",
                result_status="success" if outcome == "completed" else "error",
                content=_capture(
                    "delivered" if outcome == "completed" else "handled error"
                ),
            )
        )
    return tuple(
        TraceEvent(
            event_id=f"event-{index}",
            trace_seq=index,
            generation="generation",
            fact=fact,
            persisted_bytes=1,
        )
        for index, fact in enumerate(facts, start=1)
    )


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("same_run", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("owns_failure", [False, True])
def test_execution_recovery_is_independent_of_commit_partition(
    outcome: Outcome, same_run: bool, nested: bool, owns_failure: bool
) -> None:
    events = _events(
        outcome, same_run=same_run, nested=nested, owns_failure=owns_failure
    )
    run_ids = frozenset(event.fact.identity.run_id for event in events)
    expected_status = {
        "completed": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }[outcome]
    for prefix_length in range(1, len(events) + 1):
        prefix = events[:prefix_length]
        reference = reduce_trace_graph_records(prefix, run_ids=run_ids)[0]
        projected = project_trace_graph_node(
            reference,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=run_ids,
        )
        if prefix_length == 3:
            assert projected.status == "running"
            assert projected.result is None
            assert projected.failure is None
            assert projected.completed_at is None
            assert projected.started_at == NOW + timedelta(seconds=3)
        elif prefix_length >= 4:
            assert projected.status == expected_status
            assert projected.started_at == NOW + timedelta(seconds=3)
            assert (projected.failure is not None) is (
                outcome == "failed" and owns_failure
            )
            if projected.failure is not None:
                assert projected.failure.message == "failure-4"
        for split in range(prefix_length + 1):
            revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
            for batch in (prefix[:split], prefix[split:]):
                for mutation in graph_node_mutations(batch):
                    apply_graph_node_mutation(
                        revisions,
                        mutation,
                        source_events={event.trace_seq: event for event in batch},
                    )
            reduced = effective_graph_nodes(revisions.values(), run_ids=run_ids)[0]
            assert reduced.status == reference.status
            assert reduced.started_at == reference.started_at
            assert reduced.completed_at == reference.completed_at
            assert reduced.result_seq == reference.result_seq
            assert reduced.failure_seq == reference.failure_seq
    if not same_run:
        previous = reduce_trace_graph_records(events, run_ids=frozenset({"failed"}))[0]
        assert previous.status == "failed"
        assert previous.failure_seq == 2


def test_waiting_arguments_do_not_replace_proposal_start_time() -> None:
    identity = RunIdentity(namespace="test", thread_id="proposal", run_id="run")
    phases: tuple[Literal["started", "arguments"], ...] = ("started", "arguments")
    facts = tuple(
        ToolFact(
            source_observation_id=f"proposal-{index}",
            identity=identity,
            occurred_at=NOW + timedelta(seconds=index),
            monotonic_ns=index,
            phase=phase,
            tool_call_id=scope_id("tool", (), "report"),
            source_tool_call_id="report",
            tool_name="deliver_report",
            content=_capture("arguments") if phase == "arguments" else None,
        )
        for index, phase in enumerate(phases, start=1)
    )
    events = tuple(
        TraceEvent(
            event_id=f"event-{index}",
            trace_seq=index,
            generation="generation",
            fact=fact,
            persisted_bytes=1,
        )
        for index, fact in enumerate(facts, start=1)
    )
    for batches in ((events,), tuple((event,) for event in events)):
        revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
        for batch in batches:
            for mutation in graph_node_mutations(batch):
                apply_graph_node_mutation(
                    revisions,
                    mutation,
                    source_events={event.trace_seq: event for event in batch},
                )
        node = effective_graph_nodes(revisions.values(), run_ids=frozenset({"run"}))[0]
        assert node.status == "waiting"
        assert node.started_at == NOW + timedelta(seconds=1)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("same_run", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("batched", [False, True])
async def test_reopened_sql_and_memory_keep_only_current_execution_locators(
    trace_sql_engine: AsyncEngine,
    outcome: Outcome,
    same_run: bool,
    nested: bool,
    batched: bool,
) -> None:
    events = _events(outcome, same_run=same_run, nested=nested, owns_failure=False)
    stores = (InMemoryTraceStore(), SqlAlchemyTraceStore(trace_sql_engine))
    for initial_store in stores:
        store = initial_store
        selected_runs: list[str] = []
        writers: dict[str, TraceWriter] = {}
        async with AsyncExitStack() as cleanup:
            batches = (
                (events[:2], events[2:])
                if batched
                else tuple((event,) for event in events)
            )
            for batch in batches:
                event = batch[-1]
                identity = event.fact.identity
                if identity.run_id not in selected_runs:
                    selected_runs.append(identity.run_id)
                    writer = await store.open_writer(identity)
                    writers[identity.run_id] = writer
                    cleanup.push_async_callback(writer.aclose)
                    await writer.append(
                        (
                            RunFact(
                                source_observation_id=f"run-{identity.run_id}",
                                identity=identity,
                                occurred_at=NOW,
                                monotonic_ns=0,
                                phase="started",
                                input_kind="ordinary",
                            ),
                        )
                    )
                await writers[identity.run_id].append(
                    tuple(item.fact for item in batch)
                )
                if isinstance(store, SqlAlchemyTraceStore):
                    await trace_sql_engine.dispose()
                    store = SqlAlchemyTraceStore(trace_sql_engine)
                snapshot = await store.snapshot(identity.thread)
                page = await store.query_trace_graph(
                    snapshot.key,
                    run_ids=tuple(selected_runs),
                    where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
                    limit=10,
                )
                assert len(page.nodes) == 1
                node = project_trace_graph_node(
                    page.nodes[0],
                    turn_id="turn",
                    parent_subagent_id=None,
                    relationship_missing=False,
                    allowed_run_ids=frozenset(selected_runs),
                )
                if event.trace_seq >= 3:
                    assert node.failure is None
                    assert node.started_at == NOW + timedelta(seconds=3)
                if event.trace_seq == 3:
                    assert node.status == "running"
                    assert node.result is None
                    assert node.completed_at is None
            if not same_run:
                previous = await store.query_trace_graph(
                    snapshot.key,
                    run_ids=("failed",),
                    where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
                    limit=10,
                )
                assert previous.nodes[0].status == "failed"
                assert previous.nodes[0].failure_event is not None
        await store.delete(snapshot.key)
