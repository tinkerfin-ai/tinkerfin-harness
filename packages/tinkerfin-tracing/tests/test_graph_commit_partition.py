"""Tool timing and lifecycle results must not depend on append batch boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    SqlAlchemyTraceStore,
    ToolExecutionFact,
    ToolFact,
    TraceGraphFilter,
    TraceSemanticFact,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.facts import TraceEvent

Scenario = Literal[
    "proposal",
    "execution",
    "relocated-execution",
    "late-arguments",
    "interrupted",
    "recovery",
]


class _Source(TypedDict):
    identity: RunIdentity
    source_observation_id: str
    graph_namespace: tuple[str, ...]
    occurred_at: datetime
    monotonic_ns: int


def _partitions(
    events: tuple[TraceEvent, ...],
) -> Iterator[tuple[tuple[TraceEvent, ...], ...]]:
    for mask in range(1 << (len(events) - 1)):
        ends = [index for index in range(1, len(events)) if mask & (1 << (index - 1))]
        ends.append(len(events))
        start = 0
        batches = []
        for end in ends:
            batches.append(events[start:end])
            start = end
        yield tuple(batches)


def _events(scenario: Scenario, *, thread: str, nested: bool) -> tuple[TraceEvent, ...]:
    identity = RunIdentity(namespace="partition", thread_id=thread, run_id="run")
    namespace = ("tools:parent", "tools:child") if nested else ()
    names = {
        "proposal": ("proposal", "arguments", "result"),
        "execution": ("proposal", "arguments", "started", "completed"),
        "relocated-execution": ("proposal", "arguments", "started", "completed"),
        "late-arguments": ("proposal", "started", "arguments", "completed"),
        "interrupted": ("proposal", "arguments", "started", "interrupted"),
        "recovery": ("started", "failed", "started", "arguments", "completed"),
    }[scenario]
    result = []
    for sequence, name in enumerate(names, 1):
        captured = CapturedValue(
            disposition="inline", value=str(sequence), safe_size_bytes=3
        )
        common = _Source(
            identity=identity,
            source_observation_id=f"observation-{sequence}",
            graph_namespace=namespace,
            occurred_at=datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=sequence),
            monotonic_ns=sequence,
        )
        fact: TraceSemanticFact
        if name in {"proposal", "arguments", "result"}:
            fact = ToolFact(
                **common,
                phase="started" if name == "proposal" else name,
                tool_call_id=scope_id("tool", namespace, "call"),
                source_tool_call_id="call",
                tool_name="deliver_report",
                content=captured if name in {"arguments", "result"} else None,
                result_status="success" if name == "result" else None,
            )
        else:
            execution_common = common.copy()
            if scenario == "relocated-execution":
                execution_common["graph_namespace"] = (*namespace, "attempt:child")
            fact = ToolExecutionFact(
                **execution_common,
                phase=name,
                execution_id=f"execution-{1 if sequence < 3 else 2}",
                source_tool_call_id="call",
                tool_call_namespace=namespace,
                tool_name="deliver_report",
                input=captured if name == "started" else None,
                output=captured if name == "completed" else None,
                error_type="RuntimeError" if name == "failed" else None,
                failure_origin=name == "failed",
            )
        result.append(
            TraceEvent(
                event_id=f"event-{sequence}",
                trace_seq=sequence,
                generation="generation",
                persisted_bytes=1,
                fact=fact,
            )
        )
    return tuple(result)


@pytest.mark.parametrize(
    "scenario",
    [
        "proposal",
        "execution",
        "relocated-execution",
        "late-arguments",
        "interrupted",
        "recovery",
    ],
)
async def test_sql_batches_and_rebuild_keep_identical_tool_records(
    trace_sql_engine: AsyncEngine, scenario: Scenario
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    template = _events(scenario, thread="template", nested=True)
    for partition, _ in enumerate(_partitions(template)):
        events = _events(scenario, thread=f"partition-{partition}", nested=True)
        batches = tuple(_partitions(events))[partition]
        identity = events[0].fact.identity
        writer = await store.open_writer(identity)
        try:
            for batch in batches:
                await writer.append(tuple(event.fact for event in batch))
        finally:
            await writer.aclose()
        snapshot = await store.snapshot(identity.thread)
        before = await store.query_trace_graph(
            snapshot.key, run_ids=("run",), where=TraceGraphFilter(), limit=100
        )
        await store.rebuild_trace_graph(snapshot.key)
        after = await store.query_trace_graph(
            snapshot.key, run_ids=("run",), where=TraceGraphFilter(), limit=100
        )
        assert before.nodes == after.nodes
        assert len(after.nodes) == 1
        expected_start = {
            "proposal": 1,
            "execution": 3,
            "relocated-execution": 3,
            "late-arguments": 2,
            "interrupted": 3,
            "recovery": 3,
        }[scenario]
        assert after.nodes[0].started_seq == expected_start
