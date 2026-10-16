"""Fixed-as-of, head selection, follow, and projected status contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    InMemoryTraceStore,
    SqlAlchemyTraceStore,
    Tracer,
    TraceRunNotFound,
    TraceThreadNotFound,
)


def _context(
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
    resume: tuple[RunResumeSummary, ...] = (),
) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(namespace="test", thread_id="thread-query", run_id=run_id),
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "id": f"user-{run_id}",
                    "content": f"request {run_id}",
                }
            ]
        },
        config={},
        resume=resume,
    )


async def _start(
    tracer: Tracer,
    context: RunSourceContext,
) -> RunObservationSession:
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return session


async def _finish(
    session: RunObservationSession,
    context: RunSourceContext,
    *,
    outcome: RunTerminalOutcome = "succeeded",
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
async def test_exact_run_survives_followup_forks_and_store_reopen(
    storage: str, trace_sql_engine: AsyncEngine
) -> None:
    store = (
        InMemoryTraceStore()
        if storage == "memory"
        else SqlAlchemyTraceStore(trace_sql_engine)
    )
    tracer = Tracer(store=store)
    first = _context("first")
    session = await _start(tracer, first)
    active = await tracer.get_run(first.identity)
    assert active.summary.status.execution == "running"
    await _finish(session, first)
    saved = await tracer.get_run(first.identity)
    for run_id in ("second", "fork"):
        context = _context(run_id, parent_run_id="first")
        session = await _start(tracer, context)
        await _finish(session, context, outcome="failed")
        result = await tracer.get_run(context.identity)
        assert [item.content for item in result.messages] == [f"request {run_id}"]
        assert result.summary.status.execution == "failed"
    reopened = Tracer(
        store=(store if storage == "memory" else SqlAlchemyTraceStore(trace_sql_engine))
    )
    result = await reopened.get_run(first.identity)
    assert result.identity == first.identity
    assert result.messages == saved.messages == active.messages
    assert result.generation == saved.generation
    assert result.as_of_seq > saved.as_of_seq
    assert result.summary.status.execution == "succeeded"
    assert result.summary.completeness == saved.summary.completeness
    with pytest.raises(AmbiguousTraceHead):
        await reopened.get(first.identity.thread, head_run_id="first")
    with pytest.raises(TraceRunNotFound):
        await reopened.get_run(_context("missing").identity)
    with pytest.raises(TraceThreadNotFound):
        await reopened.get_run(
            RunIdentity(namespace="other", thread_id="thread-query", run_id="first")
        )
