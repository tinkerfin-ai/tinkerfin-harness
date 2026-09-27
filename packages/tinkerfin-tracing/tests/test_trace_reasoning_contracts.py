"""Provider reasoning retention, reconciliation, and live projection contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_tracing import ReasoningCapturePolicy, ReasoningFact, RunFact, Tracer


def _context(run_id: str) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(
            namespace="test", thread_id="thread-reasoning", run_id=run_id
        ),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [
                {
                    "role": "user",
                    "id": f"user-{run_id}",
                    "content": "Explain the result",
                }
            ]
        },
        config={},
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
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()


def _reasoning(
    context: RunSourceContext,
    content: str,
    *,
    snapshot: bool,
    monotonic_ns: int,
) -> NativeReasoningObservation:
    return NativeReasoningObservation(
        identity=context.identity,
        graph_namespace=(),
        message_id="assistant-reasoning",
        extractor="deepseek.additional_kwargs.reasoning_content",
        content=content,
        snapshot=snapshot,
        observed_at=datetime.now(UTC),
        monotonic_ns=monotonic_ns,
    )


async def test_default_policy_omits_reasoning_without_a_digest() -> None:
    tracer = Tracer()
    context = _context("default-omit")
    session = await _start(tracer, context)
    await session.observe(
        _reasoning(context, "private delta", snapshot=False, monotonic_ns=3)
    )
    await session.observe(
        _reasoning(context, "private delta 2", snapshot=False, monotonic_ns=4)
    )
    await session.observe(
        _reasoning(context, "private complete", snapshot=True, monotonic_ns=5)
    )
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={"reasoning_content": "business value"},
            observed_at=datetime.now(UTC),
            monotonic_ns=6,
        )
    )
    await _finish(session, context)

    thread = await tracer.get(context.identity.thread)
    page = await thread.events(limit=100)
    encoded = page.model_dump_json(by_alias=True)
    facts = [item.fact for item in page.items if isinstance(item.fact, ReasoningFact)]

    assert [fact.phase for fact in facts] == [
        "content",
        "completed",
    ]
    assert all(
        fact.content is None or fact.content.disposition == "omitted" for fact in facts
    )
    assert "private delta" not in encoded
    assert "private delta 2" not in encoded
    assert "private complete" not in encoded
    assert "digest" not in encoded
    assert thread.state.root["reasoning_content"] == "business value"
    assert thread.reasoning[0].content is None
    assert thread.reasoning[0].content_omitted is True
    assert thread.reasoning[0].status == "completed"


async def test_explicit_content_policy_reconciles_deltas_with_snapshot() -> None:
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    context = _context("content")
    session = await _start(tracer, context)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            graph_namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-reasoning",
                content="",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await session.observe(_reasoning(context, "first", snapshot=False, monotonic_ns=4))
    await session.observe(
        _reasoning(context, " second", snapshot=False, monotonic_ns=5)
    )
    await session.observe(
        _reasoning(context, "first second", snapshot=True, monotonic_ns=6)
    )
    await _finish(session, context)

    thread = await tracer.get(context.identity.thread)
    page = await thread.events(limit=100)
    facts = [item.fact for item in page.items if isinstance(item.fact, ReasoningFact)]
    assistant = next(item for item in thread.messages if item.role == "assistant")
    reasoning = thread.reasoning[0]

    assert [fact.phase for fact in facts] == ["content", "content", "completed"]
    assert reasoning.message_id == assistant.id
    assert reasoning.content == "first second"
    assert reasoning.content_omitted is False
    assert reasoning.status == "completed"


async def test_snapshot_replaces_a_non_matching_reasoning_prefix() -> None:
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    context = _context("reconcile")
    session = await _start(tracer, context)
    await session.observe(
        _reasoning(context, "partial", snapshot=False, monotonic_ns=3)
    )
    await session.observe(
        _reasoning(context, "canonical", snapshot=True, monotonic_ns=4)
    )
    await _finish(session, context)

    thread = await tracer.get(context.identity.thread)
    page = await thread.events(limit=100)
    facts = [item.fact for item in page.items if isinstance(item.fact, ReasoningFact)]

    assert [fact.phase for fact in facts] == [
        "content",
        "reconciled",
        "completed",
    ]
    assert thread.reasoning[0].content == "canonical"


async def test_terminal_completes_open_reasoning_before_the_run_terminal() -> None:
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    context = _context("terminal")
    session = await _start(tracer, context)
    await session.observe(
        _reasoning(context, "unfinished", snapshot=False, monotonic_ns=3)
    )
    await _finish(session, context)

    page = await (await tracer.get(context.identity.thread)).events(limit=100)
    semantic_tail = [
        item.fact
        for item in page.items
        if isinstance(item.fact, ReasoningFact)
        or (isinstance(item.fact, RunFact) and item.fact.phase == "terminal")
    ]

    assert [(fact.kind, fact.phase) for fact in semantic_tail] == [
        ("reasoning", "content"),
        ("reasoning", "completed"),
        ("run", "terminal"),
    ]


async def test_follow_emits_reasoning_entity_deltas() -> None:
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    context = _context("follow")
    session = await _start(tracer, context)
    thread = await tracer.get(context.identity.thread)
    updates = thread.follow()
    pending = asyncio.create_task(anext(updates))
    await asyncio.sleep(0)
    try:
        await session.observe(
            _reasoning(context, "live", snapshot=False, monotonic_ns=3)
        )
        update = await pending
        assert update.reasoning.removes == ()
        assert len(update.reasoning.upserts) == 1
        assert update.reasoning.upserts[0].content == "live"
        assert update.reasoning.upserts[0].status == "streaming"
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await updates.aclose()
        await _finish(session, context)


def test_reasoning_capture_policy_is_a_typed_boundary() -> None:
    invalid: Any = object()

    with pytest.raises(TypeError, match="reasoning_capture_policy must be"):
        Tracer(reasoning_capture_policy=invalid)
