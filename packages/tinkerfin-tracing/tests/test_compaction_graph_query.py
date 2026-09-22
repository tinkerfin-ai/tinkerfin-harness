"""Compaction relationships remain bounded and share one retained event prefix."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    CapturedValue,
    ContextContributionFact,
    InMemoryTraceStore,
    ModelCallFact,
    SqlAlchemyTraceStore,
    SubagentFact,
    TraceGraphFilter,
    TraceQuotaExceeded,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.backend import (
    StoredTraceGraphPage,
    TraceGraphQueryBackend,
    TraceGraphQueryRequest,
)
from tinkerfin_tracing.durable_store import DurableTraceStore
from tinkerfin_tracing.store import TraceThreadKey, TraceWriter


class _Common(TypedDict):
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    in_subagent_scope: bool
    occurred_at: datetime
    monotonic_ns: int


@pytest.fixture(params=["memory", "sqlite"])
async def compaction_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[
    tuple[DurableTraceStore, TraceThreadKey, TraceWriter, ContextContributionFact]
]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    store = (
        InMemoryTraceStore()
        if request.param == "memory"
        else SqlAlchemyTraceStore(engine)
    )
    identity = RunIdentity(namespace="n", thread_id="t", run_id="r")
    namespace = ("tools:child",)
    common: _Common = {
        "identity": identity,
        "graph_namespace": namespace,
        "in_subagent_scope": True,
        "occurred_at": datetime(2026, 9, 22, tzinfo=UTC),
        "monotonic_ns": 0,
    }
    operation = ContextContributionFact(
        **common,
        source_observation_id="action",
        phase="started",
        contribution_id="op",
        context_kind="compaction",
        name="context_compaction",
    )
    try:
        writer = await store.open_writer(identity)
        try:
            await writer.append(
                (
                    SubagentFact(
                        **common,
                        source_observation_id="child",
                        phase="started",
                        subagent_id=scope_id("subagent", namespace, namespace[-1]),
                        agent_name="worker",
                        status="running",
                    ),
                    operation,
                    ModelCallFact(
                        **common,
                        source_observation_id="model",
                        phase="started",
                        call_id="model",
                        contribution_id="op",
                        context_started_at=common["occurred_at"],
                        request=CapturedValue(
                            disposition="omitted", safe_size_bytes=0, reason="test"
                        ),
                        system_message_positions=(),
                        output_message_ids=(),
                        tool_call_ids=(),
                    ),
                    operation.model_copy(
                        update={
                            "source_observation_id": "progress",
                            "phase": "generated",
                            "model_call_ids": ("model",),
                        }
                    ),
                )
            )
            key = (
                await store.snapshot(ThreadIdentity(namespace="n", thread_id="t"))
            ).key
            yield store, key, writer, operation
        finally:
            await writer.aclose()
    finally:
        await engine.dispose()


async def test_compaction_quota_counts_shared_ancestors_once(
    compaction_store: tuple[
        DurableTraceStore, TraceThreadKey, TraceWriter, ContextContributionFact
    ],
) -> None:
    store, key, _, _ = compaction_store
    page = await store.query_trace_graph(
        key, run_ids=("r",), where=TraceGraphFilter(), limit=1, max_nodes=4
    )
    assert len(page.nodes) == 4
    assert len(page.matched_node_ids) == 1
    with pytest.raises(TraceQuotaExceeded):
        await store.query_trace_graph(
            key, run_ids=("r",), where=TraceGraphFilter(), limit=1, max_nodes=3
        )


async def test_concurrent_compaction_completion_reselects_entire_page(
    compaction_store: tuple[
        DurableTraceStore, TraceThreadKey, TraceWriter, ContextContributionFact
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, key, writer, operation = compaction_store
    backend = store._backend
    assert isinstance(backend, TraceGraphQueryBackend)
    query = backend.query_trace_graph
    completed = False

    async def complete_before_relationship(
        request: TraceGraphQueryRequest,
    ) -> StoredTraceGraphPage:
        nonlocal completed
        if request.node_ids is not None and not completed:
            completed = True
            await writer.append(
                (
                    operation.model_copy(
                        update={
                            "source_observation_id": "adopted",
                            "phase": "completed",
                            "model_call_ids": ("model",),
                            "output": CapturedValue(
                                disposition="inline", safe_size_bytes=2, value={}
                            ),
                        }
                    ),
                )
            )
        return await query(request)

    monkeypatch.setattr(backend, "query_trace_graph", complete_before_relationship)
    page = await store.query_trace_graph(
        key, run_ids=("r",), where=TraceGraphFilter(), limit=1, max_nodes=4
    )
    assert completed
    assert page.as_of_seq == 5
    assert all(node.updated_seq <= page.as_of_seq for node in page.nodes)
    assert (
        next(node for node in page.nodes if node.node_id == "op").status == "succeeded"
    )
