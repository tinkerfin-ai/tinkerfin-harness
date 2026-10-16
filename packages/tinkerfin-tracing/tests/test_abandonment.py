"""Resolved cancellation input works both on first observation and strict replay."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import JsonValue
from test_query_semantics import _context, _finish, _start

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    NativeToolCall,
    RunResumeSummary,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    RedactionContext,
    ToolFact,
    Tracer,
    TraceRunConflict,
)


@pytest.mark.parametrize("existing_thread", [False, True])
async def test_first_resolved_abandonment_can_start_and_replay(
    existing_thread: bool,
) -> None:
    tracer = Tracer()
    if existing_thread:
        parent = _context("parent")
        await _finish(await _start(tracer, parent), parent)
    context = _context(
        "cancel",
        input_kind="abandon",
        parent_run_id="parent",
        resume=(RunResumeSummary(interrupt_id="review", status="cancelled"),),
    )
    await _finish(await _start(tracer, context), context, outcome="abandoned")
    before = await tracer.store.snapshot(context.identity.thread)
    await _finish(await _start(tracer, context), context, outcome="abandoned")
    after = await tracer.store.snapshot(context.identity.thread)
    assert before.as_of_seq == after.as_of_seq
    with pytest.raises(TraceRunConflict):
        await tracer.open_run(context.model_copy(update={"input_kind": "resume"}))
    with pytest.raises(TraceRunConflict):
        await tracer.open_run(context.model_copy(update={"parent_run_id": "other"}))


@pytest.mark.parametrize("redacted", [False, True])
async def test_incomplete_or_redacted_abandonment_is_not_replayed(
    redacted: bool,
) -> None:
    class HideResume:
        def redact(self, value: JsonValue, *, context: RedactionContext) -> JsonValue:
            return [] if context.component_name == "resume" else value

    tracer = Tracer(redactor=HideResume() if redacted else None)
    context = _context(
        "cancel",
        input_kind="abandon",
        parent_run_id="parent",
        resume=(RunResumeSummary(interrupt_id="review", status="cancelled"),),
    )
    session = await _start(tracer, context)
    if redacted:
        await _finish(session, context, outcome="abandoned")
    else:
        await session.aclose()
    before = await tracer.store.snapshot(context.identity.thread)
    with pytest.raises(TraceRunConflict):
        await tracer.open_run(context)
    assert (
        await tracer.store.snapshot(context.identity.thread)
    ).as_of_seq == before.as_of_seq


async def test_concurrent_abandonments_and_explicit_branches_remain_distinct() -> None:
    tracer = Tracer()
    parent = _context("parent")
    await _finish(await _start(tracer, parent), parent)
    first = _context(
        "first",
        input_kind="abandon",
        parent_run_id="parent",
        resume=(RunResumeSummary(interrupt_id="review", status="cancelled"),),
    )
    second = first.model_copy(
        update={"identity": first.identity.model_copy(update={"run_id": "second"})}
    )
    first_session = await _start(tracer, first)
    second_session = await _start(tracer, second)
    await _finish(second_session, second, outcome="abandoned")
    await _finish(first_session, first, outcome="abandoned")
    with pytest.raises(AmbiguousTraceHead):
        await tracer.get(parent.identity.thread)
    branch = _context("branch", input_kind="branch", parent_run_id="parent")
    await _finish(await _start(tracer, branch), branch)
    result = await tracer.get(parent.identity.thread, head_run_id="branch")
    assert set(result.available_heads) == {"first", "second", "branch"}
    assert [item.source_id for item in result.messages] == [
        "user-parent",
        "user-branch",
    ]


@pytest.mark.parametrize("outcome", ["cancelled", "abandoned"])
async def test_executed_run_settlement_still_deduplicates_replayed_proposals(
    outcome: Literal["cancelled", "abandoned"],
) -> None:
    tracer = Tracer()
    original = _context("executed")
    proposal = NativeMessageRecord(
        message_type="assistant",
        id="proposal",
        content="Review this action",
        tool_calls=(NativeToolCall(id="action", name="save", arguments={}),),
    )
    session = await _start(tracer, original)
    await session.observe(
        NativeMessageObservation(
            identity=original.identity,
            graph_namespace=(),
            message=proposal,
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, original, outcome=outcome)
    before = await tracer.store.snapshot(original.identity.thread)
    continuation = _context(
        "continuation", input_kind="continuation", parent_run_id="executed"
    )
    session = await _start(tracer, continuation)
    await session.observe(
        NativeMessageObservation(
            identity=continuation.identity,
            graph_namespace=(),
            message=proposal,
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, continuation)
    after = await tracer.store.snapshot(original.identity.thread)
    events = await tracer.store.read_events(
        after.key, after_seq=before.as_of_seq, as_of_seq=after.as_of_seq, limit=100
    )
    assert not any(isinstance(event.fact, ToolFact) for event in events)
