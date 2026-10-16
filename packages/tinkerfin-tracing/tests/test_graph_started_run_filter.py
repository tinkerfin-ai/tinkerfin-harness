"""Start-owner filters retain full lineage updates before applying page limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    SqlAlchemyTraceStore,
    ToolFact,
    TraceGraphFilter,
)


async def test_sql_start_owner_filter_precedes_limit_and_keeps_latest_result(
    trace_sql_engine: AsyncEngine,
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    identity = RunIdentity(namespace="scope", thread_id="start-owner", run_id="old")
    now = datetime(2026, 9, 13, tzinfo=UTC)
    old = await store.open_writer(identity)
    try:
        await old.append(
            (
                ToolFact(
                    identity=identity,
                    source_observation_id="old-start",
                    occurred_at=now,
                    monotonic_ns=1,
                    phase="started",
                    tool_call_id="old-tool",
                    source_tool_call_id="old-tool",
                    tool_name="save_old",
                ),
            )
        )
    finally:
        await old.aclose()
    current_identity = identity.model_copy(update={"run_id": "new"})
    current = await store.open_writer(current_identity)
    try:
        await current.append(
            (
                ToolFact(
                    identity=current_identity,
                    source_observation_id="old-result",
                    occurred_at=now + timedelta(seconds=1),
                    monotonic_ns=2,
                    phase="result",
                    tool_call_id="old-tool",
                    source_tool_call_id="old-tool",
                    tool_name="save_old",
                    result_status="success",
                    content=CapturedValue(
                        disposition="inline", safe_size_bytes=11, value="completed"
                    ),
                ),
                ToolFact(
                    identity=current_identity,
                    source_observation_id="new-start",
                    occurred_at=now + timedelta(seconds=2),
                    monotonic_ns=3,
                    phase="started",
                    tool_call_id="new-tool",
                    source_tool_call_id="new-tool",
                    tool_name="save_new",
                ),
            )
        )
    finally:
        await current.aclose()
    snapshot = await store.snapshot(identity.thread)
    for rebuilt in (False, True):
        if rebuilt:
            await store.rebuild_trace_graph(snapshot.key)
        for selected, expected in (
            (None, "new-tool"),
            (("old",), "old-tool"),
            (("new",), "new-tool"),
            ((), None),
        ):
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("old", "new"),
                started_run_ids=selected,
                where=TraceGraphFilter(),
                limit=1,
            )
            assert len(page.nodes) == (0 if expected is None else 1)
            if expected is not None:
                node = page.nodes[0]
                assert node.node_id.endswith(expected)
                assert node.run_id == "new"
                assert node.started_event.fact.identity.run_id == (
                    "old" if expected == "old-tool" else "new"
                )
                if expected == "old-tool":
                    assert node.status == "succeeded" and node.result_event is not None
        found = await store.query_trace_graph(
            snapshot.key,
            run_ids=("old", "new"),
            started_run_ids=("old",),
            where=TraceGraphFilter(search="completed"),
            limit=1,
        )
        assert len(found.nodes) == 1 and found.nodes[0].node_id.endswith("old-tool")
    await store.delete(snapshot.key)
