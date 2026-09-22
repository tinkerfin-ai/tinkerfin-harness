"""Graph following preserves exact prefixes without rereading unchanged nodes."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CallTrackingFact,
    CapturedValue,
    InMemoryTraceStore,
    InvalidTraceCursor,
    MessageFact,
    NativeExtraFact,
    ReasoningFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    Tracer,
    TraceSemanticFact,
    TraceStoreUpdate,
    TurnFact,
)
from tinkerfin_tracing.backend import (
    StoredTraceGraphPage,
    TraceGraphQueryBackend,
    TraceGraphQueryRequest,
)
from tinkerfin_tracing.graph import (
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphQueryLimits,
)
from tinkerfin_tracing.store import TraceThreadKey, TraceWriter

_IDENTITY = RunIdentity(namespace="test", thread_id="graph-follow", run_id="run")
_NOW = datetime(2026, 9, 21, tzinfo=UTC)


class _Source(TypedDict):
    identity: RunIdentity
    source_observation_id: str
    occurred_at: datetime
    monotonic_ns: int


def _source(sequence: int, identity: RunIdentity = _IDENTITY) -> _Source:
    return {
        "identity": identity,
        "source_observation_id": f"{identity.run_id}-{sequence}",
        "occurred_at": _NOW + timedelta(seconds=sequence),
        "monotonic_ns": sequence,
    }


def _captured(value: JsonValue) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        value=value,
        safe_size_bytes=len(json.dumps(value, separators=(",", ":")).encode()),
    )


def _content(sequence: int) -> MessageFact:
    return MessageFact(
        **_source(sequence),
        phase="content",
        message_id="assistant",
        role="assistant",
        content=_captured("text"),
    )


@asynccontextmanager
async def _recorded_run(store: InMemoryTraceStore) -> AsyncIterator[TraceWriter]:
    writer = await store.open_writer(_IDENTITY)
    try:
        await writer.append(
            (
                RunFact(**_source(1), phase="started", input_kind="ordinary"),
                TurnFact(**_source(2), turn_id="turn", user_message_id="user"),
                MessageFact(
                    **_source(3),
                    phase="started",
                    message_id="assistant",
                    role="assistant",
                ),
            )
        )
        yield writer
    finally:
        await writer.aclose()


def _count_graph_queries(
    store: InMemoryTraceStore, monkeypatch: pytest.MonkeyPatch
) -> list[TraceGraphQueryRequest]:
    backend = store.backend
    assert isinstance(backend, TraceGraphQueryBackend)
    original = backend.query_trace_graph
    requests: list[TraceGraphQueryRequest] = []

    async def query(request: TraceGraphQueryRequest) -> StoredTraceGraphPage:
        requests.append(request)
        return await original(request)

    monkeypatch.setattr(backend, "query_trace_graph", query)
    return requests


@pytest.mark.parametrize(
    "fact",
    (
        _content(4),
        ReasoningFact(
            **_source(4),
            phase="content",
            reasoning_id="reasoning",
            message_id="assistant",
            source_message_id="assistant-source",
            extractor="provider",
            content=_captured("reasoning"),
        ),
        StateRevisionFact(
            **_source(4), revision_id="state", changes=_captured({"state": True})
        ),
        NativeExtraFact(**_source(4), mode="custom", data_type="dict"),
    ),
    ids=("content", "reasoning", "state", "native"),
)
async def test_unchanged_graph_advances_its_cursor_without_an_index_query(
    monkeypatch: pytest.MonkeyPatch, fact: TraceSemanticFact
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    async with _recorded_run(store) as writer:
        first = await tracer.query(_IDENTITY.thread, limit=1)
        assert first.next_cursor is not None
        requests = _count_graph_queries(store, monkeypatch)
        async with first.follow() as updates:
            committed = await writer.append((fact,))
            update = await anext(updates)
        assert requests == []
        assert update.as_of_seq == committed[-1].trace_seq
        assert update.next_cursor is not None
        assert update.next_cursor != first.next_cursor
        assert update.node_upserts == update.node_removes == ()
        assert update.turn_upserts == update.turn_removes == ()
        assert update.ordered_node_ids == first.ordered_node_ids
        assert update.matched_node_ids == first.matched_node_ids
        assert update.completeness == first.completeness

        current = await tracer.query(_IDENTITY.thread, limit=1)
        assert update.next_cursor == current.next_cursor
        assert current.nodes == first.nodes
        older = await tracer.query(_IDENTITY.thread, limit=1, cursor=update.next_cursor)
        assert older.nodes[0].kind is TraceGraphNodeKind.HUMAN_MESSAGE
        with pytest.raises(InvalidTraceCursor):
            await tracer.query(_IDENTITY.thread, limit=1, cursor=first.next_cursor)


@pytest.mark.parametrize(
    "facts",
    (
        (
            _content(4),
            MessageFact(
                **_source(5),
                phase="completed",
                message_id="assistant",
                role="assistant",
                content=_captured("complete body"),
            ),
        ),
        (
            ToolFact(
                **_source(4),
                phase="arguments",
                tool_call_id="tool",
                source_tool_call_id="tool-source",
                tool_name="search",
                content=_captured({"query": "term"}),
            ),
        ),
        (CallTrackingFact(**_source(4)),),
        (RunFact(**_source(4), phase="terminal", outcome="cancelled"),),
        (
            MessageFact(
                **_source(4),
                phase="removed",
                message_id="assistant",
                role="assistant",
            ),
        ),
    ),
    ids=("mixed-content", "tool-arguments", "call-tracking", "terminal", "removed"),
)
async def test_graph_evidence_and_completeness_still_refresh(
    monkeypatch: pytest.MonkeyPatch, facts: tuple[TraceSemanticFact, ...]
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    async with _recorded_run(store) as writer:
        first = await tracer.query(_IDENTITY.thread)
        requests = _count_graph_queries(store, monkeypatch)
        async with first.follow() as updates:
            await writer.append(facts, mandatory=isinstance(facts[-1], RunFact))
            update = await anext(updates)
        assert len(requests) == 1
        last = facts[-1]
        if isinstance(last, CallTrackingFact):
            assert first.completeness.call_tracking_missing
            assert not update.completeness.call_tracking_missing
        elif isinstance(last, ToolFact):
            assert any(
                node.kind is TraceGraphNodeKind.TOOL
                and node.request == {"query": "term"}
                for node in update.node_upserts
            )
        elif isinstance(last, RunFact):
            assert any(
                node.id == "assistant" and node.status is TraceGraphNodeStatus.CANCELLED
                for node in update.node_upserts
            )
        elif isinstance(last, MessageFact) and last.phase == "removed":
            assert update.node_removes == ("assistant",)
        else:
            assert any(node.content == "complete body" for node in update.node_upserts)


async def test_first_evidence_for_an_unseen_run_requires_a_graph_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    async with _recorded_run(store):
        first = await tracer.query(_IDENTITY.thread)
        requests = _count_graph_queries(store, monkeypatch)
        other_identity = RunIdentity(
            namespace=_IDENTITY.namespace, thread_id=_IDENTITY.thread_id, run_id="other"
        )
        other = await store.open_writer(other_identity)
        try:
            async with first.follow() as updates:
                await other.append(
                    (
                        StateRevisionFact(
                            **_source(4, other_identity),
                            revision_id="unknown-run-state",
                            changes=_captured({"state": True}),
                        ),
                    )
                )
                update = await anext(updates)
            assert len(requests) == 1
            assert update.as_of_seq > first.as_of_seq
        finally:
            await other.aclose()


@pytest.mark.parametrize("overlapping_batch", (False, True))
async def test_refresh_reads_ahead_without_requerying_covered_events(
    monkeypatch: pytest.MonkeyPatch, overlapping_batch: bool
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    async with _recorded_run(store) as writer:
        first = await tracer.query(_IDENTITY.thread)
        committed = await writer.append((CallTrackingFact(**_source(4)), _content(5)))
        requests = _count_graph_queries(store, monkeypatch)
        source_closed = False

        async def follow(
            key: TraceThreadKey, *, after_seq: int
        ) -> AsyncGenerator[TraceStoreUpdate, None]:
            nonlocal source_closed
            assert key == first.key and after_seq == first.as_of_seq
            try:
                yield TraceStoreUpdate(
                    as_of_seq=committed[0].trace_seq,
                    events=committed[:1],
                    active_run_ids=(_IDENTITY.run_id,),
                    observed_at=_NOW,
                )
                if not overlapping_batch:
                    yield TraceStoreUpdate(
                        as_of_seq=committed[-1].trace_seq,
                        events=committed[1:],
                        active_run_ids=(_IDENTITY.run_id,),
                        observed_at=_NOW,
                    )
                later = await writer.append((_content(6),))
                yield TraceStoreUpdate(
                    as_of_seq=later[-1].trace_seq,
                    events=(*committed[1:], *later) if overlapping_batch else later,
                    active_run_ids=(_IDENTITY.run_id,),
                    observed_at=_NOW,
                )
            finally:
                source_closed = True

        monkeypatch.setattr(store, "follow", follow)
        async with first.follow() as updates:
            refreshed = await anext(updates)
            assert refreshed.as_of_seq == committed[-1].trace_seq
            assert not refreshed.completeness.call_tracking_missing
            advanced = await anext(updates)
            assert advanced.as_of_seq == refreshed.as_of_seq + 1
            assert advanced.node_upserts == ()
            assert advanced.completeness == refreshed.completeness
        assert len(requests) == 1
        assert source_closed


async def test_prefix_growth_preserves_the_graph_page_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    async with _recorded_run(store) as writer:
        await writer.append(
            (
                MessageFact(
                    **_source(4),
                    phase="reconciled",
                    message_id="assistant",
                    role="assistant",
                    content=_captured("body" * 300),
                ),
                *(
                    NativeExtraFact(**_source(index), mode="custom", data_type="dict")
                    for index in range(5, 10)
                ),
            )
        )
        initial = await Tracer(store=store).query(_IDENTITY.thread, limit=1)
        assert initial.as_of_seq == 9
        budget = len(initial.snapshot.model_dump_json(by_alias=True).encode())
        tracer = Tracer(
            store=store,
            graph_query_limits=TraceGraphQueryLimits(max_page_bytes=budget),
        )
        first = await tracer.query(_IDENTITY.thread, limit=1)
        assert not first.completeness.details_omitted
        requests = _count_graph_queries(store, monkeypatch)
        async with first.follow() as updates:
            await writer.append((_content(10),))
            update = await anext(updates)
        assert requests == []
        assert update.as_of_seq == 10
        assert update.completeness.details_omitted
        assert len(update.model_dump_json(by_alias=True).encode()) <= budget
        assert update.node_upserts[0].content is None
        assert update.node_upserts[0].content_omitted
        current = await tracer.query(_IDENTITY.thread, limit=1)
        assert current.nodes == update.node_upserts
        assert current.completeness == update.completeness
