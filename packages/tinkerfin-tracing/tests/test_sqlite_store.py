"""SQLite durable Trace Store sequencing, replay, checkpoints, and borrowed Engine."""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support.sql_faults import after_sql_commit

from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_tracing._graph_projection import project_trace_graph_node
from tinkerfin_tracing.backend import (
    TraceStoreOptions,
)
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec, EncodedTracePayload
from tinkerfin_tracing.durable_store import InMemoryTraceStore
from tinkerfin_tracing.errors import (
    TraceThreadNotFound,
)
from tinkerfin_tracing.facts import (
    CallTrackingFact,
    ModelCallFact,
    RunFact,
    SubagentFact,
    ToolFact,
    TraceSemanticFact,
    TurnFact,
)
from tinkerfin_tracing.graph import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from tinkerfin_tracing.limits import TraceLimits
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
)
from tinkerfin_tracing.store import TraceProjectionCheckpoint
from tinkerfin_tracing.tracer import Tracer


class _EncryptedTraceCodec(CanonicalTracePayloadCodec):
    """Small reversible codec proving indexed details still use the codec boundary."""

    _key = 0xA5

    @classmethod
    def _transform(cls, value: bytes) -> bytes:
        return bytes(item ^ cls._key for item in value)

    def encode_fact(self, fact: TraceSemanticFact) -> EncodedTracePayload:
        canonical = CanonicalTracePayloadCodec().encode_fact(fact)
        encrypted = self._transform(canonical.data)
        return EncodedTracePayload(
            data=encrypted,
            digest=canonical.digest,
        )

    def decode_fact(self, payload: bytes) -> TraceSemanticFact:
        return CanonicalTracePayloadCodec().decode_fact(self._transform(payload))

    def encode_json(self, value: JsonValue) -> EncodedTracePayload:
        canonical = CanonicalTracePayloadCodec().encode_json(value)
        encrypted = self._transform(canonical.data)
        return EncodedTracePayload(
            data=encrypted,
            digest=canonical.digest,
        )

    def decode_json(self, payload: bytes) -> JsonValue:
        return CanonicalTracePayloadCodec().decode_json(self._transform(payload))

    @staticmethod
    def digest(payload: bytes) -> str:
        canonical = _EncryptedTraceCodec._transform(payload)
        return CanonicalTracePayloadCodec.digest(canonical)


def _identity(run_id: str = "run-sql") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-sql", run_id=run_id)


def _captured(value: JsonValue) -> CapturedValue:
    encoded = CanonicalTracePayloadCodec().encode_json(value)
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(encoded.data),
        value=value,
    )


def _fact(
    phase: Literal["started", "input", "terminal", "closed"],
    *,
    run_id: str = "run-sql",
) -> RunFact:
    captured = (
        CapturedValue(disposition="inline", safe_size_bytes=2, value={})
        if phase == "input"
        else None
    )
    return RunFact(
        source_observation_id=f"observation-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase in {"started", "input"} else None,
        input=captured,
        config=captured,
        outcome="succeeded" if phase in {"terminal", "closed"} else None,
    )


async def test_tracer_uses_the_protected_codec_for_capture_search_and_rebuild(
    trace_sql_engine: AsyncEngine,
) -> None:
    engine = trace_sql_engine
    store = SqlAlchemyTraceStore(engine, codec=_EncryptedTraceCodec())
    tracer = Tracer(store=store)
    marker = "protected-task-marker"
    source = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"id": "user-codec", "role": "user", "content": marker}]},
        config={},
    )
    session = await tracer.open_run(source)
    now = datetime.now(UTC)
    try:
        await session.observe(
            RunStartedObservation(
                identity=source.identity, observed_at=now, monotonic_ns=1
            )
        )
        await session.observe(
            RunInputObservation(
                identity=source.identity, source=source, observed_at=now, monotonic_ns=2
            )
        )
        await session.observe(
            RunTerminalObservation(
                identity=source.identity,
                outcome="succeeded",
                observed_at=now,
                monotonic_ns=3,
            )
        )
        await session.observe(
            RunClosedObservation(
                identity=source.identity,
                outcome="succeeded",
                observed_at=now,
                monotonic_ns=4,
            )
        )
        await session.aclose()
        page = await tracer.query(
            source.identity.thread, where=TraceGraphFilter(search=marker)
        )
        assert [node.content for node in page.nodes] == [marker]
        assert await tracer.rebuild_graph(source.identity.thread) == 1
        rebuilt = await tracer.query(
            source.identity.thread, where=TraceGraphFilter(search=marker)
        )
        assert rebuilt.nodes == page.nodes
        assert (await tracer.get(source.identity.thread)).messages[0].content == marker
        async with engine.connect() as connection:
            payloads = (
                (
                    await connection.execute(
                        text("SELECT payload FROM tinkerfin_trace_events")
                    )
                )
                .scalars()
                .all()
            )
        assert payloads
        assert all(
            not bytes(payload).startswith(b"{")
            and marker.encode() not in bytes(payload)
            for payload in payloads
        )
    finally:
        await session.aclose()
        await engine.dispose()


async def test_sql_graph_query_merges_only_selected_run_revisions(
    trace_sql_engine: AsyncEngine,
) -> None:
    engine = trace_sql_engine
    store = SqlAlchemyTraceStore(engine)
    root = await store.open_writer(_identity("graph-root"))
    branch_a = await store.open_writer(_identity("graph-branch-a"))
    branch_b = await store.open_writer(_identity("graph-branch-b"))
    tool_id = "tool:shared"
    now = datetime.now(UTC)
    try:
        await root.append(
            (
                _fact("started", run_id="graph-root"),
                ToolFact(
                    source_observation_id="graph-root-tool",
                    identity=_identity("graph-root"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                ),
            )
        )
        await branch_a.append(
            (
                _fact("started", run_id="graph-branch-a"),
                ToolFact(
                    source_observation_id="graph-branch-a-tool",
                    identity=_identity("graph-branch-a"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="result",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                    content=CapturedValue(
                        disposition="inline",
                        safe_size_bytes=4,
                        value="ok",
                    ),
                    result_status="success",
                ),
            )
        )
        await branch_b.append(
            (
                _fact("started", run_id="graph-branch-b"),
                ToolFact(
                    source_observation_id="graph-branch-b-tool",
                    identity=_identity("graph-branch-b"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="abandoned",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread)
        only_root = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root",),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )
        selected_a = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root", "graph-branch-a"),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )
        selected_b = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root", "graph-branch-b"),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )

        assert only_root.nodes[0].status.value == "waiting"
        assert selected_a.nodes[0].status.value == "succeeded"
        assert selected_b.nodes[0].status.value == "abandoned"
        assert selected_a.nodes[0].started_seq == only_root.nodes[0].started_seq
        async with engine.connect() as connection:
            physical_rows = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM tinkerfin_trace_graph_nodes "
                    "WHERE kind = 'tool'"
                )
            )
        assert physical_rows == 3
    finally:
        await root.aclose()
        await branch_a.aclose()
        await branch_b.aclose()
        await engine.dispose()


async def test_sql_graph_query_keeps_latest_non_null_lineage_values(
    trace_sql_engine: AsyncEngine,
) -> None:
    engine = trace_sql_engine
    memory = InMemoryTraceStore()
    sql = SqlAlchemyTraceStore(engine)
    stores = (memory, sql)
    origin_started_at = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    later_revision_at = origin_started_at + timedelta(seconds=5)
    subagent_id = "subagent:shared"
    try:
        for store in stores:
            root = await store.open_writer(_identity("graph-parent"))
            child = await store.open_writer(_identity("graph-child"))
            await root.append(
                (
                    _fact("started", run_id="graph-parent"),
                    SubagentFact(
                        source_observation_id="graph-parent-subagent",
                        identity=_identity("graph-parent"),
                        graph_namespace=(),
                        occurred_at=origin_started_at,
                        monotonic_ns=2,
                        phase="started",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        parent_tool_call_id="call-task",
                        input=_captured({"description": "research"}),
                        status="running",
                    ),
                )
            )
            await child.append(
                (
                    _fact("started", run_id="graph-child"),
                    SubagentFact(
                        source_observation_id="graph-child-subagent",
                        identity=_identity("graph-child"),
                        graph_namespace=(),
                        occurred_at=later_revision_at,
                        monotonic_ns=3,
                        phase="completed",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        status="succeeded",
                    ),
                )
            )
            await root.aclose()
            await child.aclose()

        observed = []
        for store in stores:
            snapshot = await store.snapshot(_identity().thread)
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            observed.append(
                (
                    page.nodes[0].parent_subagent_id,
                    page.nodes[0].link_issue,
                    page.nodes[0].started_at,
                    page.nodes[0].status,
                    page.nodes[0].run_id,
                )
            )

        assert observed[0][0] is None
        assert observed == [observed[0], observed[0]]
        assert observed[0][2] == origin_started_at
        assert observed[0][3:] == (TraceGraphNodeStatus.SUCCEEDED, "graph-child")

        for store in stores:
            snapshot = await store.snapshot(_identity().thread)
            exact_unicode = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="Ä",
                    started_after=origin_started_at - timedelta(seconds=1),
                ),
                limit=10,
            )
            different_unicode_case = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="ä",
                ),
                limit=10,
            )
            exact_chinese = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="研究",
                ),
                limit=10,
            )
            exact_kelvin = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="K",
                ),
                limit=10,
            )
            ascii_does_not_fold_kelvin = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="k",
                ),
                limit=10,
            )
            ascii_case_insensitive = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="RESEARCHER",
                ),
                limit=10,
            )
            assert len(exact_unicode.nodes) == 1
            assert different_unicode_case.nodes == ()
            assert len(exact_chinese.nodes) == 1
            assert len(exact_kelvin.nodes) == 1
            assert ascii_does_not_fold_kelvin.nodes == ()
            assert len(ascii_case_insensitive.nodes) == 1

        for store in stores:
            conflicting = await store.open_writer(_identity("graph-conflict"))
            await conflicting.append(
                (
                    _fact("started", run_id="graph-conflict"),
                    SubagentFact(
                        source_observation_id="graph-conflicting-subagent",
                        identity=_identity("graph-conflict"),
                        graph_namespace=(),
                        occurred_at=later_revision_at + timedelta(seconds=1),
                        monotonic_ns=4,
                        phase="completed",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        status="succeeded",
                    ),
                )
            )
            await conflicting.aclose()
            snapshot = await store.snapshot(_identity().thread)
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child", "graph-conflict"),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            assert page.nodes[0].parent_subagent_id is None
    finally:
        await engine.dispose()


async def test_tracer_rebuilds_graph_without_rewriting_ledger(
    trace_sql_engine: AsyncEngine,
) -> None:
    engine = trace_sql_engine
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                _fact("input"),
                CallTrackingFact(
                    source_observation_id="observation-call-tracking-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                ),
                ModelCallFact(
                    source_observation_id="observation-model-start-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    context_started_at=now,
                    call_id="model-call-rebuild",
                    system_message_positions=(),
                    output_message_ids=(),
                    request=_captured({"messages": [{"content": "rebuild-marker"}]}),
                ),
                ModelCallFact(
                    source_observation_id="observation-model-end-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-call-rebuild",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
        await writer.aclose()
        async with engine.begin() as connection:
            ledger_count = await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_trace_events")
            )
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))

        rebuilt = await Tracer(store=store).rebuild_graph(_identity().thread)
        page = await store.query_trace_graph(
            (await store.snapshot(_identity().thread)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT COUNT(*) FROM tinkerfin_trace_events")
                )
                == ledger_count
            )

        assert rebuilt == 2
        assert len(page.nodes) == 1
        node = project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({_identity().run_id}),
        )
        assert node.request_reference is not None
        request = await Tracer(store=store).model_request(
            _identity().thread, reference=node.request_reference
        )
        assert request.request == {"messages": [{"content": "rebuild-marker"}]}
    finally:
        await engine.dispose()


async def test_rebuild_merges_lifecycle_revisions_across_event_pages(
    trace_sql_engine: AsyncEngine,
) -> None:
    engine = trace_sql_engine
    store = SqlAlchemyTraceStore(
        engine,
        limits=TraceLimits(follow_batch_size=1),
    )
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                TurnFact(
                    source_observation_id="page-boundary-turn",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    turn_id="turn:page-boundary",
                    user_message_id="human:page-boundary",
                ),
                ModelCallFact(
                    source_observation_id="page-boundary-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    context_started_at=now,
                    call_id="model-page-boundary",
                    system_message_positions=(),
                    output_message_ids=(),
                    request=_captured({"messages": []}),
                ),
                ModelCallFact(
                    source_observation_id="page-boundary-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-page-boundary",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
        await writer.aclose()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))

        assert await Tracer(store=store).rebuild_graph(_identity().thread) == 3
        page = await store.query_trace_graph(
            (await store.snapshot(_identity().thread)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(),
            limit=10,
        )
        model = next(
            node for node in page.nodes if node.kind is TraceGraphNodeKind.MODEL
        )

        assert len(page.nodes) == 3
        assert model.status is TraceGraphNodeStatus.SUCCEEDED
        assert model.started_seq < model.updated_seq
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sql_unknown_commit_reuses_event_and_checkpoint_evidence(
    trace_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = trace_sql_engine
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    faults = ExitStack()

    def unknown_commit_error() -> DBAPIError:
        cause = sqlite3.OperationalError("disk I/O error")
        cause.sqlite_errorcode = sqlite3.SQLITE_IOERR
        return DBAPIError(None, None, cause, connection_invalidated=True)

    def fail_once_after_commit() -> None:
        remaining = 1

        async def transaction() -> None:
            nonlocal remaining
            if remaining:
                remaining -= 1
                raise unknown_commit_error()

        faults.enter_context(after_sql_commit(engine, transaction))

    try:
        # The injected DBAPI failure happens after the real transaction committed.
        # Public append must prove the first commit by its retained event IDs.
        fail_once_after_commit()
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread)
        assert snapshot.as_of_seq == 1
        assert [
            event.event_id
            for event in await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=10,
            )
        ] == [committed[0].event_id]

        fail_once_after_commit()
        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="unknown.projection",
            run_id="run-sql",
            as_of_seq=1,
            state={"count": 1},
        )
        assert (
            await store.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            )
            == checkpoint
        )
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="unknown.projection",
                run_id="run-sql",
                as_of_seq=1,
            )
            == checkpoint
        )
        await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        fail_once_after_commit()
        await store.delete(snapshot.key)
        with pytest.raises(TraceThreadNotFound):
            await store.snapshot_key(snapshot.key)
    finally:
        faults.close()
        await writer.aclose()
        await engine.dispose()
