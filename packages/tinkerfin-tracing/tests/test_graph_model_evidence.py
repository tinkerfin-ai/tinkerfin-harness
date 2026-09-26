"""Model relationships survive independent Tool lifecycle and request updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_contracts import RunIdentity
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


@pytest.fixture(
    params=(
        "memory",
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    )
)
async def graph_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[TraceStore, AsyncEngine | None]]:
    if request.param == "memory":
        yield InMemoryTraceStore(), None
        return
    async with AsyncExitStack() as databases:
        if request.param == "postgresql":
            engine = await databases.enter_async_context(
                request.getfixturevalue("trace_postgresql_database")()
            )
            assert isinstance(engine, AsyncEngine)
            yield SqlAlchemyTraceStore(engine), engine
            return
        url = (
            await databases.enter_async_context(
                request.getfixturevalue("trace_mysql_database")()
            )
            if request.param == "mysql"
            else f"sqlite+aiosqlite:///{tmp_path / 'model-evidence.db'}"
        )
        assert isinstance(url, str)
        engine = create_async_engine(url, hide_parameters=True)
        try:
            yield (
                SqlAlchemyTraceStore(engine),
                engine,
            )
        finally:
            await engine.dispose()


@pytest.mark.parametrize("same_commit", (False, True))
@pytest.mark.parametrize("execution_namespace", ((), ("attempt:child",)))
async def test_tool_execution_retains_model_proof_and_actual_input(
    graph_store: tuple[TraceStore, AsyncEngine | None],
    same_commit: bool,
    execution_namespace: tuple[str, ...],
) -> None:
    store, _ = graph_store
    assert isinstance(store, TraceGraphStore)
    identity = RunIdentity(namespace="test", thread_id="model-evidence", run_id="run")
    tracer = Tracer(store=store)
    initial, model_completed, execution_started, execution_completed = _facts(
        identity, execution_namespace=execution_namespace
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

        await writer.append((execution_completed,))
        await writer.append(
            (
                RunFact(**_source(identity, 11), phase="terminal", outcome="succeeded"),
                RunFact(**_source(identity, 12), phase="closed", outcome="succeeded"),
            ),
            mandatory=True,
        )
        complete = await tracer.query(identity.thread)
        finished_tool = next(
            node for node in complete.nodes if node.kind is TraceGraphNodeKind.TOOL
        )
        assert finished_tool.model_call_id == model_completed.call_id
        assert finished_tool.request == {"value": "edited"}
        assert finished_tool.result == {"result": "done"}
        assert finished_tool.status is TraceGraphNodeStatus.SUCCEEDED
        await tracer.rebuild_graph(identity.thread)
        assert (await tracer.query(identity.thread)).snapshot == complete.snapshot
    finally:
        await writer.aclose()


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


@pytest.mark.docker_integration
async def test_mysql_graph_cache_rebuild_preserves_ledger_and_core_checkpoints(
    mysql_sandbox_url: str,
) -> None:
    engine = create_async_engine(mysql_sandbox_url, hide_parameters=True)
    store = SqlAlchemyTraceStore(engine)
    tracer = Tracer(store=store)
    identity = RunIdentity(namespace="test", thread_id="cache-rebuild", run_id="run")
    initial, model_completed, execution_started, execution_completed = _facts(identity)
    writer = await store.open_writer(identity)
    try:
        await writer.append(
            (*initial, model_completed, execution_started, execution_completed)
        )
        await writer.append(
            (
                RunFact(**_source(identity, 11), phase="terminal", outcome="succeeded"),
                RunFact(**_source(identity, 12), phase="closed", outcome="succeeded"),
            ),
            mandatory=True,
        )
        await writer.aclose()
        before = await tracer.query(identity.thread)
        await tracer.get(identity.thread)
        snapshot = await store.snapshot(identity.thread)
        original_events = await store.read_events(
            snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
        )
        async with engine.connect() as connection:
            original_checkpoints = (
                await connection.execute(
                    text("SELECT * FROM tinkerfin_trace_projection_checkpoints")
                )
            ).all()
        assert original_checkpoints

        # This test owns the complete disposable database. Recreate exactly the
        # operator's ADD COLUMN state, where retained Graph rows have no locator yet.
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_trace_graph_nodes DROP COLUMN model_call_seq"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE tinkerfin_trace_graph_nodes ADD COLUMN model_call_seq "
                "BIGINT NULL COMMENT 'Ledger sequence proving the emitting Model relationship'"
            )
        fresh_store = SqlAlchemyTraceStore(engine)
        await fresh_store.setup()
        fresh_tracer = Tracer(store=fresh_store)
        with pytest.raises(TraceStoreProtocolError):
            await fresh_tracer.query(identity.thread)
        await fresh_tracer.rebuild_graph(identity.thread)
        assert (await fresh_tracer.query(identity.thread)).snapshot == before.snapshot
        assert (await fresh_tracer.get(identity.thread)).graph.nodes == before.nodes
        assert (
            await fresh_store.read_events(
                snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
            )
            == original_events
        )
        async with engine.connect() as connection:
            rebuilt_checkpoints = (
                await connection.execute(
                    text("SELECT * FROM tinkerfin_trace_projection_checkpoints")
                )
            ).all()
        assert rebuilt_checkpoints == original_checkpoints
    finally:
        await writer.aclose()
        await engine.dispose()
