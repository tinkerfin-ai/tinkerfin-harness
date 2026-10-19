"""Model relationships survive independent Tool lifecycle and request updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_contracts import ObservationClock, RunIdentity
from tinkerfin_tracing import (
    CallTrackingFact,
    CapturedValue,
    InMemoryTraceStore,
    ModelCallFact,
    RunFact,
    SqlAlchemyTraceStore,
    ToolExecutionFact,
    ToolFact,
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphStore,
    Tracer,
    TraceSemanticFact,
    TraceStore,
    TraceStoreProtocolError,
    TurnFact,
)
from tinkerfin_tracing._graph_reducer import (
    apply_graph_events,
    apply_graph_node_mutation,
    graph_node_mutations,
)
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec
from tinkerfin_tracing.facts import TraceEvent


class _Source(TypedDict):
    identity: RunIdentity
    source_observation_id: str
    occurred_at: datetime
    monotonic_ns: int


def _source(identity: RunIdentity, sequence: int) -> _Source:
    return {
        "identity": identity,
        "source_observation_id": f"source-{sequence}",
        "occurred_at": datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=sequence),
        "monotonic_ns": sequence,
    }


def _captured(value: JsonValue) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(json.dumps(value, separators=(",", ":")).encode()),
        value=value,
    )


def _facts(
    identity: RunIdentity,
    *,
    execution_namespace: tuple[str, ...] = (),
) -> tuple[
    tuple[TraceSemanticFact, ...], ModelCallFact, ToolExecutionFact, ToolExecutionFact
]:
    initial: tuple[TraceSemanticFact, ...] = (
        RunFact(**_source(identity, 1), phase="started", input_kind="ordinary"),
        TurnFact(**_source(identity, 2), turn_id="local-turn"),
        RunFact(
            **_source(identity, 3),
            phase="input",
            input_kind="ordinary",
            input=_captured({}),
            config=_captured({}),
        ),
        CallTrackingFact(**_source(identity, 4)),
        ModelCallFact(
            **_source(identity, 5),
            phase="started",
            call_id="model-call",
            context_started_at=_source(identity, 3)["occurred_at"],
            request=_captured({"messages": []}),
            system_message_positions=(),
            output_message_ids=(),
        ),
        ToolFact(
            **_source(identity, 6),
            phase="started",
            tool_call_id="local-tool",
            source_tool_call_id="local-tool",
            tool_name="local_tool",
        ),
        ToolFact(
            **_source(identity, 7),
            phase="arguments",
            tool_call_id="local-tool",
            source_tool_call_id="local-tool",
            tool_name="local_tool",
            content=_captured({"value": "proposed"}),
        ),
    )
    model_completed = ModelCallFact(
        **_source(identity, 8),
        phase="completed",
        call_id="model-call",
        tool_call_ids=("local-tool",),
        system_message_positions=(),
        output_message_ids=(),
    )
    execution_started = ToolExecutionFact(
        **_source(identity, 9),
        graph_namespace=execution_namespace,
        phase="started",
        execution_id="local-execution",
        source_tool_call_id="local-tool",
        tool_call_namespace=(),
        tool_name="local_tool",
        parent_call_id=None,
        input=_captured({"value": "edited"}),
    )
    execution_completed = ToolExecutionFact(
        **_source(identity, 10),
        graph_namespace=execution_namespace,
        phase="completed",
        execution_id="local-execution",
        source_tool_call_id="local-tool",
        tool_call_namespace=(),
        tool_name="local_tool",
        parent_call_id=None,
        output=_captured({"result": "done"}),
    )
    return initial, model_completed, execution_started, execution_completed


@pytest.fixture(params=("memory", "sqlite"))
async def graph_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[TraceStore, AsyncEngine | None]]:
    if request.param == "memory":
        yield InMemoryTraceStore(), None
        return
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'model-evidence.db'}", hide_parameters=True
    )
    try:
        yield SqlAlchemyTraceStore(engine), engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("same_commit", (False, True))
@pytest.mark.parametrize("execution_namespace", ((), ("attempt:child",)))
@pytest.mark.parametrize("renamed", (False, True))
async def test_tool_execution_retains_model_proof_and_actual_input(
    graph_store: tuple[TraceStore, AsyncEngine | None],
    same_commit: bool,
    execution_namespace: tuple[str, ...],
    renamed: bool,
) -> None:
    store, _ = graph_store
    assert isinstance(store, TraceGraphStore)
    identity = RunIdentity(namespace="test", thread_id="model-evidence", run_id="run")
    tracer = Tracer(store=store)
    initial, model_completed, execution_started, execution_completed = _facts(
        identity, execution_namespace=execution_namespace
    )
    execution_name = "approved_tool" if renamed else "local_tool"
    execution_started = execution_started.model_copy(
        update={"tool_name": execution_name}
    )
    execution_completed = execution_completed.model_copy(
        update={"tool_name": execution_name}
    )
    writer = await store.open_writer(identity)
    try:
        await writer.append(initial)
        if not same_commit:
            await writer.append((model_completed,))
        before = await tracer.query(identity.thread)
        follow = before.follow()
        pending = asyncio.create_task(anext(follow))
        try:
            batch = (
                (model_completed, execution_started)
                if same_commit
                else (execution_started,)
            )
            committed = await writer.append(batch)
            assert committed[-1].fact == execution_started
            delta = await pending
            assert any(
                node.kind is TraceGraphNodeKind.TOOL
                and node.model_call_id == model_completed.call_id
                and node.status is TraceGraphNodeStatus.RUNNING
                for node in delta.node_upserts
            )
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await follow.aclose()

        current = await tracer.query(identity.thread)
        tools = tuple(
            node for node in current.nodes if node.kind is TraceGraphNodeKind.TOOL
        )
        assert len(tools) == 1
        tool = tools[0]
        assert tool.graph_namespace == ()
        assert tool.name == execution_name
        assert tool.request == {"value": "edited"}
        assert tool.started_at == execution_started.occurred_at
        assert tool.model_call_id == model_completed.call_id
        assert not current.completeness.relationship_evidence_missing
        history = await tracer.get(identity.thread)
        assert history.graph.nodes == current.nodes
        snapshot = await store.snapshot(identity.thread)
        records = await store.query_trace_graph(
            snapshot.key,
            run_ids=(identity.run_id,),
            where=TraceGraphFilter(),
            limit=100,
        )
        record = next(
            node for node in records.nodes if node.kind is TraceGraphNodeKind.TOOL
        )
        assert record.model_call_event is not None
        assert record.model_call_event.fact == model_completed
        assert record.model_call_seq == record.model_call_event.trace_seq
        assert record.request_event is not None
        assert record.request_event.fact == execution_started

        await writer.append(
            (
                execution_completed,
                ToolFact(
                    **_source(identity, 11),
                    phase="result",
                    tool_call_id="local-tool",
                    source_tool_call_id="local-tool",
                    tool_name="local_tool",
                    content=_captured({"result": "done"}),
                    result_status="success",
                ),
            )
        )
        await writer.append(
            (
                RunFact(**_source(identity, 12), phase="terminal", outcome="succeeded"),
                RunFact(**_source(identity, 13), phase="closed", outcome="succeeded"),
            ),
            mandatory=True,
        )
        complete = await tracer.query(identity.thread)
        finished_tool = next(
            node for node in complete.nodes if node.kind is TraceGraphNodeKind.TOOL
        )
        assert finished_tool.model_call_id == model_completed.call_id
        assert finished_tool.name == execution_name
        assert finished_tool.request == {"value": "edited"}
        assert finished_tool.result == {"result": "done"}
        assert finished_tool.status is TraceGraphNodeStatus.SUCCEEDED
        await tracer.rebuild_graph(identity.thread)
        assert (await tracer.query(identity.thread)).snapshot == complete.snapshot
    finally:
        await writer.aclose()


async def test_source_clock_roundtrips_and_survives_graph_rebuild(
    graph_store: tuple[TraceStore, AsyncEngine | None],
) -> None:
    store, _engine = graph_store
    identity = RunIdentity(namespace="test", thread_id="source-clock", run_id="run")
    initial, model, started, completed = _facts(identity)
    clock = ObservationClock(
        namespace="producer",
        thread_id="source",
        run_id="execution",
        sequence=1,
        observed_at=datetime(2040, 1, 1, tzinfo=UTC),
        monotonic_ns=10,
    )
    end = clock.model_copy(update={"sequence": 2, "monotonic_ns": 20})
    started = started.model_copy(update={"source_clock": clock})
    completed = completed.model_copy(update={"source_clock": end})
    codec = CanonicalTracePayloadCodec()
    assert codec.decode_fact(codec.encode_fact(started).data) == started
    writer = await store.open_writer(identity)
    try:
        await writer.append((*initial, model, started, completed))
        await writer.append(
            (
                RunFact(**_source(identity, 12), phase="terminal", outcome="succeeded"),
                RunFact(**_source(identity, 13), phase="closed", outcome="succeeded"),
            ),
            mandatory=True,
        )
        tracer = Tracer(store=store)
        for _query in range(2):
            graph = await tracer.query(identity.thread)
            tool = next(
                node for node in graph.nodes if node.kind is TraceGraphNodeKind.TOOL
            )
            assert tool.source_started == clock
            assert tool.source_completed == end
            assert tool.started_at == started.occurred_at
            assert tool.completed_at == completed.occurred_at
            await tracer.rebuild_graph(identity.thread)
    finally:
        await writer.aclose()


def test_tool_rename_requires_the_exact_execution_name() -> None:
    identity = RunIdentity(namespace="test", thread_id="evidence", run_id="run")
    initial, _, execution, _ = _facts(identity)
    events = tuple(
        TraceEvent(
            event_id=f"event-{sequence}",
            trace_seq=sequence,
            generation="generation",
            persisted_bytes=1,
            fact=fact,
        )
        for sequence, fact in enumerate((*initial, execution), 1)
    )
    revisions = {}
    apply_graph_events(revisions, events[:-1])
    mutation = graph_node_mutations((events[-1],))[0]
    with pytest.raises(TraceStoreProtocolError, match="name lacks matching evidence"):
        apply_graph_node_mutation(
            revisions,
            replace(mutation, name="unproven_tool"),
            source_events={event.trace_seq: event for event in events},
        )


@pytest.mark.parametrize("bad_sequence", (None, 5, 9, 1000))
async def test_sql_rejects_missing_wrong_or_unavailable_model_proof(
    graph_store: tuple[TraceStore, AsyncEngine | None], bad_sequence: int | None
) -> None:
    store, engine = graph_store
    if engine is None:
        pytest.skip("SQL corruption requires the SQL fixture")
    identity = RunIdentity(
        namespace="test", thread_id="model-evidence-corruption", run_id="run"
    )
    initial, model_completed, execution_started, _ = _facts(identity)
    writer = await store.open_writer(identity)
    tracer = Tracer(store=store)
    try:
        await writer.append((*initial, model_completed, execution_started))
        good = await tracer.query(identity.thread)
        tool = next(node for node in good.nodes if node.kind is TraceGraphNodeKind.TOOL)
        snapshot = await store.snapshot(identity.thread)
        original_events = await store.read_events(
            snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_graph_nodes SET model_call_seq = :sequence "
                    "WHERE generation = :generation AND node_id = :node_id"
                ),
                {
                    "sequence": bad_sequence,
                    "generation": snapshot.key.generation,
                    "node_id": json.dumps(tool.id, ensure_ascii=False),
                },
            )
        with pytest.raises(TraceStoreProtocolError):
            await tracer.query(identity.thread)
        assert (
            await store.read_events(
                snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
            )
            == original_events
        )
    finally:
        await writer.aclose()
