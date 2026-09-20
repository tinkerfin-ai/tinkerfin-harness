"""SQLite durable Trace Store sequencing, replay, checkpoints, and borrowed Engine."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from types import SimpleNamespace
from typing import Any, Literal, TypeVar

import pytest
from aiosqlite import Connection as AioSqliteConnection
from pydantic import JsonValue
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import inspect, text
from sqlalchemy.engine import AdaptedConnection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from tests.support.sql_faults import after_sql_command, after_sql_commit

from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_tracing._graph_projection import project_trace_graph_node
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.backend import (
    TraceEventPageRequest,
    TraceLedgerStateRequest,
    TraceStoreOptions,
)
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec, EncodedTracePayload
from tinkerfin_tracing.durable_store import InMemoryTraceStore
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceQuotaExceeded,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
    TraceThreadNotFound,
)
from tinkerfin_tracing.facts import (
    CallTrackingFact,
    ContextContributionFact,
    MessageFact,
    ModelCallFact,
    RunFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceSemanticFact,
    TurnFact,
)
from tinkerfin_tracing.graph import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from tinkerfin_tracing.limits import TraceLimits
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
    _SqlAlchemyTraceLedgerBackend,
)
from tinkerfin_tracing.store import TraceProjectionCheckpoint
from tinkerfin_tracing.tracer import Tracer
from tinkerfin_tracing.writing import TraceBatchWriter, TraceWritePolicy

_WriteResultT = TypeVar("_WriteResultT")


class _SqliteClock:
    """Share controllable UTC time across every SQL clock query on selected engines."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def install(self, engine: AsyncEngine) -> None:
        """Register the clock on each connection before the engine opens it."""

        def connected(dbapi_connection: Any, _connection_record: object) -> None:
            assert isinstance(dbapi_connection, AdaptedConnection)

            async def register(driver_connection: Any) -> None:
                assert isinstance(driver_connection, AioSqliteConnection)
                await driver_connection.create_function("strftime", 2, self._strftime)

            dbapi_connection.run_async(register)

        sqlalchemy_event.listen(engine.sync_engine, "connect", connected)

    def _strftime(self, format_string: str, time_value: str) -> str:
        assert (format_string, time_value) == ("%Y-%m-%d %H:%M:%f", "now")
        return self.now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


async def _wait_for_committed_write(
    writing: asyncio.Task[_WriteResultT], committed: asyncio.Event
) -> None:
    """Observe the committed result or immediately propagate an earlier write failure."""

    observed = asyncio.create_task(committed.wait())
    try:
        done, _ = await asyncio.wait(
            (writing, observed), timeout=3, return_when=asyncio.FIRST_COMPLETED
        )
        if writing in done:
            await writing
            pytest.fail("write completed before the committed-result gate")
        assert observed in done, "write did not reach the committed-result gate"
    finally:
        observed.cancel()
        await asyncio.gather(observed, return_exceptions=True)


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


def test_encrypted_codec_preserves_the_canonical_pre_transform_digest() -> None:
    canonical_codec = CanonicalTracePayloadCodec()
    encrypted_codec = _EncryptedTraceCodec()
    fact = _fact("started")

    canonical_fact = canonical_codec.encode_fact(fact)
    encrypted_fact = encrypted_codec.encode_fact(fact)
    assert encrypted_fact.data != canonical_fact.data
    assert encrypted_fact.digest == canonical_fact.digest
    assert encrypted_codec.digest(encrypted_fact.data) == canonical_fact.digest
    assert encrypted_codec.decode_fact(encrypted_fact.data) == fact

    state = {"cursor": 3, "status": "ready"}
    canonical_state = canonical_codec.encode_json(state)
    encrypted_state = encrypted_codec.encode_json(state)
    assert encrypted_state.data != canonical_state.data
    assert encrypted_state.digest == canonical_state.digest
    assert encrypted_codec.digest(encrypted_state.data) == canonical_state.digest
    assert encrypted_codec.decode_json(encrypted_state.data) == state

    tampered = encrypted_fact.data[:-1] + bytes((encrypted_fact.data[-1] ^ 1,))
    assert encrypted_codec.digest(tampered) != encrypted_fact.digest


async def _ignore_committed(_events: tuple[TraceEvent, ...]) -> None:
    return None


def _backend(store: SqlAlchemyTraceStore) -> _SqlAlchemyTraceLedgerBackend:
    backend = store.backend
    assert isinstance(backend, _SqlAlchemyTraceLedgerBackend)
    return backend


async def test_sql_batch_admission_estimates_before_store_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CanonicalTracePayloadCodec.encode_fact
    encoded_observations: list[str] = []

    def count_encoding(
        codec: CanonicalTracePayloadCodec,
        fact: TraceSemanticFact,
    ) -> EncodedTracePayload:
        encoded_observations.append(fact.source_observation_id)
        return original(codec, fact)

    monkeypatch.setattr(CanonicalTracePayloadCodec, "encode_fact", count_encoding)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'encoding.db'}")
    store = SqlAlchemyTraceStore(engine)
    try:
        writer = TraceBatchWriter(
            await store.open_writer(_identity()),
            policy=TraceWritePolicy(max_batch_delay_seconds=0),
            on_committed=_ignore_committed,
        )
        await writer.submit((_fact("started"), _fact("input")), mandatory=False)
        await writer.force()
        await writer.aclose()
    finally:
        await engine.dispose()

    assert encoded_observations == [
        "observation-run-sql-started",
        "observation-run-sql-input",
        "observation-run-sql-started",
        "observation-run-sql-input",
    ]


async def test_sql_batch_commits_through_the_store_codec(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch-codec.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        codec=_EncryptedTraceCodec(),
    )
    writer = TraceBatchWriter(
        await store.open_writer(_identity()),
        policy=TraceWritePolicy(max_batch_delay_seconds=0),
        on_committed=_ignore_committed,
    )
    try:
        facts = (_fact("started"), _fact("input"))
        await writer.submit(facts, mandatory=False)
        await writer.force()
        snapshot = await store.snapshot(_identity().thread)
        events = await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
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

        assert tuple(event.fact.source_observation_id for event in events) == tuple(
            fact.source_observation_id for fact in facts
        )
        assert len(payloads) == len(facts)
        assert all(not bytes(payload).startswith(b"{") for payload in payloads)
    finally:
        await writer.aclose()
        await engine.dispose()


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


async def test_sqlite_graph_index_filters_without_repeating_fact_payloads(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    marker = "unique-final-request-marker"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                _fact("input"),
                CallTrackingFact(
                    source_observation_id="observation-call-tracking",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                ),
                ModelCallFact(
                    source_observation_id="observation-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    context_started_at=now,
                    call_id="model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                    provider="openai",
                    model="gpt-test",
                    request=_captured({"messages": [{"content": marker}]}),
                ),
                ModelCallFact(
                    source_observation_id="observation-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        snapshot = await store.snapshot(_identity().thread)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )

        graph = await Tracer(store=store).query(_identity().thread)
        assert graph.completeness.call_tracking_missing is False
        assert len(page.nodes) == 1
        node = project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({_identity().run_id}),
        )
        assert node.request == {"messages": [{"content": marker}]}
        rebuilt = await store.rebuild_trace_graph(snapshot.key)
        assert rebuilt == 2
        async with engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(tinkerfin_trace_graph_nodes)")
                    )
                ).all()
            }
            payloads = (
                await connection.execute(
                    text("SELECT payload FROM tinkerfin_trace_events")
                )
            ).scalars()
            indexed = (
                await connection.execute(
                    text(
                        "SELECT namespace_hash, thread_hash, generation, "
                        "run_hash FROM tinkerfin_trace_graph_nodes LIMIT 1"
                    )
                )
            ).one()
            plan = (
                await connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT node_id "
                        "FROM tinkerfin_trace_graph_nodes "
                        "WHERE namespace_hash = :namespace_hash "
                        "AND thread_hash = :thread_hash "
                        "AND generation = :generation "
                        "AND run_hash = :run_hash"
                    ),
                    {
                        "namespace_hash": indexed.namespace_hash,
                        "thread_hash": indexed.thread_hash,
                        "generation": indexed.generation,
                        "run_hash": indexed.run_hash,
                    },
                )
            ).all()
            index_names = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA index_list(tinkerfin_trace_graph_nodes)")
                    )
                ).all()
                if not str(row[1]).startswith("sqlite_autoindex")
            }
        assert not ({"payload", "request", "result"} & columns)
        assert sum(bytes(payload).count(marker.encode()) for payload in payloads) == 1
        assert any("ix_tinkerfin_trace_graph_run" in str(row) for row in plan)
        assert index_names == {"ix_tinkerfin_trace_graph_run"}
    finally:
        await writer.aclose()
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
    subagent_namespace = ("tools:shared",)
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
                        graph_namespace=subagent_namespace,
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
                        graph_namespace=subagent_namespace,
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
                        graph_namespace=subagent_namespace,
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


async def test_graph_store_accepts_64_subagent_levels_and_rejects_level_65(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'graph-scope-depth.db'}"
    )
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    run_id = "graph-scope-depth"
    namespaces = tuple(
        tuple(f"tools:{index}" for index in range(depth)) for depth in range(1, 66)
    )
    try:
        for store in stores:
            writer = await store.open_writer(_identity(run_id))
            await writer.append(
                (
                    _fact("started", run_id=run_id),
                    *(
                        SubagentFact(
                            source_observation_id=f"subagent-depth-{depth}",
                            identity=_identity(run_id),
                            graph_namespace=namespace,
                            occurred_at=datetime(2026, 1, 1, tzinfo=UTC)
                            + timedelta(milliseconds=depth),
                            monotonic_ns=depth + 1,
                            phase="started",
                            subagent_id=scope_id(
                                "subagent",
                                namespace,
                                namespace[-1],
                            ),
                            agent_name=f"agent-{depth}",
                            parent_tool_call_id=f"task-{depth}",
                            model_call_id=f"model-{depth}",
                            status="running",
                        )
                        for depth, namespace in enumerate(namespaces, start=1)
                    ),
                )
            )
            await writer.aclose()

        for store in stores:
            snapshot = await store.snapshot(_identity().thread)
            legal = await store.query_trace_graph(
                snapshot.key,
                run_ids=(run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    graph_namespaces={namespaces[63]},
                ),
                limit=1,
                max_nodes=65,
            )
            assert len(legal.nodes) == 64
            assert len(legal.matched_node_ids) == 1

            with pytest.raises(TraceStoreProtocolError, match="at most 64 levels"):
                await store.query_trace_graph(
                    snapshot.key,
                    run_ids=(run_id,),
                    where=TraceGraphFilter(
                        kinds={TraceGraphNodeKind.SUBAGENT},
                        agent_names={"agent-65"},
                    ),
                    limit=1,
                    max_nodes=65,
                )
    finally:
        await engine.dispose()


async def test_sql_append_reuses_locked_state_and_one_graph_prefetch(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'graph-prefetch.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity("graph-prefetch"))
    identity = _identity("graph-prefetch")
    now = datetime.now(UTC)
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    sqlalchemy_event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        await writer.append(
            tuple(
                ToolExecutionFact(
                    source_observation_id=f"prefetch-start-{index}",
                    identity=identity,
                    occurred_at=now + timedelta(microseconds=index),
                    monotonic_ns=index + 1,
                    phase="started",
                    execution_id=f"execution-{index}",
                    source_tool_call_id=f"call-{index}",
                    tool_name="read_file",
                    input=_captured({"path": str(index)}),
                )
                for index in range(20)
            )
        )
        first_append_statements = tuple(statements)
        assert not any(
            statement.lstrip().upper().startswith("SELECT")
            and "tinkerfin_trace_events" in statement
            for statement in first_append_statements
        )

        statements.clear()
        await writer.append(
            tuple(
                ToolExecutionFact(
                    source_observation_id=f"prefetch-end-{index}",
                    identity=identity,
                    occurred_at=now + timedelta(seconds=1, microseconds=index),
                    monotonic_ns=100 + index,
                    phase="completed",
                    execution_id=f"execution-{index}",
                    source_tool_call_id=f"call-{index}",
                    tool_name="read_file",
                    output=_captured("ok"),
                )
                for index in range(20)
            )
        )
        graph_selects = tuple(
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "tinkerfin_trace_graph_nodes" in statement
        )
        assert len(graph_selects) == 1
    finally:
        sqlalchemy_event.remove(
            engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_graph_lineage_limit_stays_below_the_bind_boundary(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'graph-lineage-limit.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity("lineage-00000"))
    try:
        await writer.append((_fact("started", run_id="lineage-00000"),))
        await writer.aclose()
        snapshot = await store.snapshot(_identity().thread)
        run_ids = tuple(f"lineage-{index:05d}" for index in range(10_000))

        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=run_ids,
            where=TraceGraphFilter(),
            limit=1,
            max_nodes=1,
        )
        assert page.nodes == ()

        with pytest.raises(TraceQuotaExceeded) as captured:
            await store.query_trace_graph(
                snapshot.key,
                run_ids=(*run_ids, "lineage-overflow"),
                where=TraceGraphFilter(),
                limit=1,
                max_nodes=1,
            )
        assert captured.value.context["resource"] == "graph_lineage_runs"
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_tool_lineage_uses_execution_time_and_clears_stale_completion(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tool-lineage-time.db'}"
    )
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    proposal_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    execution_at = proposal_at + timedelta(seconds=1)
    completed_at = proposal_at + timedelta(seconds=2)
    resumed_at = proposal_at + timedelta(seconds=3)
    selected_runs = ("tool-proposal", "tool-execution", "tool-resume")
    try:
        for store in stores:
            proposal = await store.open_writer(_identity("tool-proposal"))
            await proposal.append(
                (
                    _fact("started", run_id="tool-proposal"),
                    ToolFact(
                        source_observation_id="tool-proposal",
                        identity=_identity("tool-proposal"),
                        occurred_at=proposal_at,
                        monotonic_ns=2,
                        phase="started",
                        tool_call_id=scope_id("tool", (), "shared-tool"),
                        source_tool_call_id="shared-tool",
                        parent_call_id="model-call",
                        tool_name="read_file",
                    ),
                )
            )
            await proposal.aclose()

            execution = await store.open_writer(_identity("tool-execution"))
            await execution.append(
                (
                    _fact("started", run_id="tool-execution"),
                    ToolExecutionFact(
                        source_observation_id="tool-execution-start",
                        identity=_identity("tool-execution"),
                        occurred_at=execution_at,
                        monotonic_ns=2,
                        phase="started",
                        execution_id="shared-execution",
                        parent_call_id="model-call",
                        source_tool_call_id="shared-tool",
                        tool_name="read_file",
                        input=_captured({"path": "a"}),
                    ),
                    ToolExecutionFact(
                        source_observation_id="tool-execution-complete",
                        identity=_identity("tool-execution"),
                        occurred_at=completed_at,
                        monotonic_ns=3,
                        phase="completed",
                        execution_id="shared-execution",
                        parent_call_id="model-call",
                        source_tool_call_id="shared-tool",
                        tool_name="read_file",
                        output=_captured("ok"),
                    ),
                )
            )
            await execution.aclose()

            snapshot = await store.snapshot(_identity().thread)
            completed = await store.query_trace_graph(
                snapshot.key,
                run_ids=selected_runs[:2],
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
                limit=10,
            )
            assert completed.nodes[0].started_at == execution_at
            assert completed.nodes[0].completed_at == completed_at
            assert completed.nodes[0].status is TraceGraphNodeStatus.SUCCEEDED

            resumed = await store.open_writer(_identity("tool-resume"))
            await resumed.append(
                (
                    _fact("started", run_id="tool-resume"),
                    ToolExecutionFact(
                        source_observation_id="tool-resume-start",
                        identity=_identity("tool-resume"),
                        occurred_at=resumed_at,
                        monotonic_ns=2,
                        phase="started",
                        execution_id="shared-execution",
                        parent_call_id="model-call",
                        source_tool_call_id="shared-tool",
                        tool_name="read_file",
                        input=_captured({"path": "a"}),
                    ),
                )
            )
            await resumed.aclose()

            snapshot = await store.snapshot(_identity().thread)
            running = await store.query_trace_graph(
                snapshot.key,
                run_ids=selected_runs,
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
                limit=10,
            )
            observed = (
                running.nodes[0].started_at,
                running.nodes[0].completed_at,
                running.nodes[0].status,
            )
            assert observed == (resumed_at, None, TraceGraphNodeStatus.RUNNING)

            await store.rebuild_trace_graph(snapshot.key)
            rebuilt = await store.query_trace_graph(
                snapshot.key,
                run_ids=selected_runs,
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
                limit=10,
            )
            assert (
                rebuilt.nodes[0].started_at,
                rebuilt.nodes[0].completed_at,
                rebuilt.nodes[0].status,
            ) == observed
    finally:
        await engine.dispose()


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "cancelled"])
async def test_subagent_lineage_survives_two_interrupts_and_rebuild(
    tmp_path: Path,
    terminal_status: Literal["succeeded", "failed", "cancelled"],
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'subagent-{terminal_status}.db'}"
    )
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    namespace = ("tools:shared-subagent",)
    subagent_id = scope_id("subagent", namespace, namespace[-1])
    first_start = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    second_start = first_start + timedelta(seconds=2)
    final_start = first_start + timedelta(seconds=4)
    terminal_at = first_start + timedelta(seconds=5)
    run_ids = ("subagent-first", "subagent-second", "subagent-final")
    try:
        for store in stores:
            for run_id, started_at in zip(
                run_ids,
                (first_start, second_start, final_start),
                strict=True,
            ):
                writer = await store.open_writer(_identity(run_id))
                facts: list[TraceSemanticFact] = [
                    _fact("started", run_id=run_id),
                    SubagentFact(
                        source_observation_id=f"{run_id}-start",
                        identity=_identity(run_id),
                        graph_namespace=namespace,
                        occurred_at=started_at,
                        monotonic_ns=2,
                        phase="started",
                        subagent_id=subagent_id,
                        agent_name="researcher",
                        parent_tool_call_id="task-call",
                        parent_execution_id="task-execution",
                        model_call_id="model-call",
                        status="running",
                    ),
                ]
                if run_id != "subagent-final":
                    facts.append(
                        SubagentFact(
                            source_observation_id=f"{run_id}-waiting",
                            identity=_identity(run_id),
                            graph_namespace=namespace,
                            occurred_at=started_at + timedelta(seconds=1),
                            monotonic_ns=3,
                            phase="updated",
                            subagent_id=subagent_id,
                            agent_name="researcher",
                            status="waiting",
                        )
                    )
                else:
                    facts.append(
                        SubagentFact(
                            source_observation_id=f"{run_id}-terminal",
                            identity=_identity(run_id),
                            graph_namespace=namespace,
                            occurred_at=terminal_at,
                            monotonic_ns=3,
                            phase="completed",
                            subagent_id=subagent_id,
                            agent_name="researcher",
                            status=terminal_status,
                        )
                    )
                await writer.append(tuple(facts))
                await writer.aclose()

                if run_id == "subagent-second":
                    interrupted_snapshot = await store.snapshot(_identity().thread)
                    interrupted = await store.query_trace_graph(
                        interrupted_snapshot.key,
                        run_ids=run_ids[:2],
                        where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                        limit=10,
                    )
                    assert interrupted.nodes[0].started_at == second_start
                    assert interrupted.nodes[0].completed_at is None
                    assert interrupted.nodes[0].status is TraceGraphNodeStatus.WAITING

            snapshot = await store.snapshot(_identity().thread)
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=run_ids,
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            expected_status = TraceGraphNodeStatus(terminal_status)
            observed = (
                page.nodes[0].started_at,
                page.nodes[0].completed_at,
                page.nodes[0].status,
            )
            assert observed == (final_start, terminal_at, expected_status)

            await store.rebuild_trace_graph(snapshot.key)
            rebuilt = await store.query_trace_graph(
                snapshot.key,
                run_ids=run_ids,
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            assert (
                rebuilt.nodes[0].started_at,
                rebuilt.nodes[0].completed_at,
                rebuilt.nodes[0].status,
            ) == observed
    finally:
        await engine.dispose()


async def test_memory_and_sql_graph_removal_isolated_to_one_sibling_lineage(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph-remove.db'}")
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    try:
        for store in stores:
            root = await store.open_writer(_identity("remove-root"))
            now = datetime.now(UTC)
            await root.append(
                (
                    _fact("started", run_id="remove-root"),
                    TurnFact(
                        source_observation_id="remove-turn",
                        identity=_identity("remove-root"),
                        occurred_at=now,
                        monotonic_ns=2,
                        turn_id="turn:remove",
                        user_message_id="human-remove",
                    ),
                )
            )
            snapshot = await store.snapshot(_identity().thread)
            root_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root",),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                ),
                limit=10,
            )
            assert len(root_page.nodes) == 1
            human_node_id = root_page.nodes[0].node_id

            removed = await store.open_writer(_identity("remove-branch"))
            await removed.append(
                (
                    _fact("started", run_id="remove-branch"),
                    MessageFact(
                        source_observation_id="remove-message",
                        identity=_identity("remove-branch"),
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="removed",
                        message_id=human_node_id,
                        source_message_id="human-remove",
                        role="other",
                    ),
                )
            )
            sibling = await store.open_writer(_identity("keep-branch"))
            await sibling.append((_fact("started", run_id="keep-branch"),))
            snapshot = await store.snapshot(_identity().thread)

            removed_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "remove-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                ),
                limit=10,
            )
            sibling_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "keep-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                ),
                limit=10,
            )
            assert removed_page.nodes == (), type(store).__name__
            assert tuple(node.node_id for node in sibling_page.nodes) == (
                human_node_id,
            )

            await store.rebuild_trace_graph(snapshot.key)
            rebuilt_removed = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "remove-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                ),
                limit=10,
            )
            assert rebuilt_removed.nodes == ()
            await root.aclose()
            await removed.aclose()
            await sibling.aclose()
    finally:
        await engine.dispose()


async def test_sqlite_graph_clears_a_tool_parent_gap_after_model_completion(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'resolved-tool-parent.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    model_id = "model-call"
    try:
        await writer.append(
            (
                ModelCallFact(
                    source_observation_id="resolved-parent-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=1,
                    phase="started",
                    context_started_at=now,
                    call_id=model_id,
                    parent_call_id="agent-call",
                    request=_captured({"messages": []}),
                    system_message_positions=(),
                    output_message_ids=(),
                ),
                ToolFact(
                    source_observation_id="resolved-parent-tool-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    tool_call_id="tool-call",
                    source_tool_call_id="tool-call",
                    tool_name="read_file",
                ),
            )
        )
        await writer.append(
            (
                ModelCallFact(
                    source_observation_id="resolved-parent-model-complete",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id=model_id,
                    system_message_positions=(),
                    output_message_ids=(),
                    tool_call_ids=("tool-call",),
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.TOOL},
            ),
            limit=10,
        )

        assert len(page.nodes) == 1
        assert page.nodes[0].model_call_id == model_id
        assert page.nodes[0].link_issue is None

        await writer.aclose()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))
        assert await Tracer(store=store).rebuild_graph(_identity().thread) == 3
        rebuilt = await store.query_trace_graph(
            (await store.snapshot(_identity().thread)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.TOOL},
            ),
            limit=10,
        )
        assert rebuilt.nodes[0].model_call_id == model_id
        assert rebuilt.nodes[0].link_issue is None
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_graph_query_reads_details_through_a_custom_encrypted_codec(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'encrypted-graph.db'}"
    )
    store = SqlAlchemyTraceStore(
        engine,
        codec=_EncryptedTraceCodec(),
    )
    writer = await store.open_writer(_identity())
    marker = "encrypted-final-request-marker"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                ModelCallFact(
                    source_observation_id="encrypted-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    context_started_at=now,
                    call_id="encrypted-model-call",
                    system_message_positions=(0,),
                    output_message_ids=(),
                    request=_captured(
                        {"messages": [{"messageType": "system", "content": marker}]}
                    ),
                ),
                ModelCallFact(
                    source_observation_id="encrypted-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id="encrypted-model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))
        assert await Tracer(store=store).rebuild_graph(_identity().thread) == 2
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )
        searched = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.MODEL},
                search="ENCRYPTED-FINAL-REQUEST-MARKER",
            ),
            limit=10,
        )
        context_page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.CONTEXT}),
            limit=10,
        )
        node = project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({_identity().run_id}),
        )
        context = project_trace_graph_node(
            context_page.nodes[0],
            turn_id="turn:test",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({_identity().run_id}),
        )
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

        assert node.request == {
            "messages": [{"messageType": "system", "content": marker}]
        }
        assert context.content == marker
        assert context.request is None
        assert context_page.nodes[0].request_seq == page.nodes[0].request_seq
        assert searched.matched_node_ids == (page.nodes[0].node_id,)
        assert tuple(item.node_id for item in searched.nodes) == (
            page.nodes[0].node_id,
        )
        assert all(marker.encode() not in bytes(payload) for payload in payloads)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_memory_and_sql_search_decoded_content_without_ancestor_pollution(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'content-search.db'}"
    )
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    now = datetime.now(UTC)
    try:
        for store in stores:
            writer = await store.open_writer(_identity())
            await writer.append(
                (
                    _fact("started"),
                    ContextContributionFact(
                        source_observation_id="search-parent-start",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=2,
                        phase="started",
                        contribution_id="search-parent",
                        context_kind="custom",
                        name="parent-context",
                        input=_captured({"note": "ancestor only"}),
                    ),
                    ContextContributionFact(
                        source_observation_id="search-parent-completed",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="completed",
                        contribution_id="search-parent",
                        context_kind="custom",
                        name="parent-context",
                        output=_captured({"result": "parent result"}),
                    ),
                    ContextContributionFact(
                        source_observation_id="search-child-start",
                        identity=_identity(),
                        occurred_at=now + timedelta(seconds=1),
                        monotonic_ns=4,
                        phase="started",
                        contribution_id="search-child",
                        parent_call_id="search-parent",
                        context_kind="custom",
                        name="child-context",
                        input=_captured(
                            {
                                "customer_note": "Child Visible Marker",
                                "sequence": 42,
                            }
                        ),
                    ),
                    ContextContributionFact(
                        source_observation_id="search-child-completed",
                        identity=_identity(),
                        occurred_at=now + timedelta(seconds=2),
                        monotonic_ns=5,
                        phase="completed",
                        contribution_id="search-child",
                        context_kind="custom",
                        name="child-context",
                        output=_captured({"result": "核验完成"}),
                    ),
                )
            )
            snapshot = await store.snapshot(_identity().thread)

            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.CUSTOM},
                    search="CHILD VISIBLE MARKER",
                ),
                limit=10,
            )
            key_match = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.CUSTOM},
                    search="customer_note",
                ),
                limit=10,
            )
            scalar_match = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.CUSTOM},
                    search="42",
                ),
                limit=10,
            )
            unicode_match = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.CUSTOM},
                    search="核验完成",
                ),
                limit=10,
            )

            assert {item.node_id for item in page.nodes} == {"search-child"}
            assert page.matched_node_ids == ("search-child",)
            assert key_match.matched_node_ids == ("search-child",)
            assert scalar_match.matched_node_ids == ("search-child",)
            assert unicode_match.matched_node_ids == ("search-child",)
            await writer.aclose()
    finally:
        await engine.dispose()


async def test_memory_and_sql_graph_paging_share_the_digest_tie_breaker(
    tmp_path: Path,
) -> None:
    """Equal timestamps page identically across the two built-in Stores."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph-order.db'}")
    stores = (
        InMemoryTraceStore(),
        SqlAlchemyTraceStore(engine),
    )
    occurred_at = datetime.now(UTC)
    call_specs = (
        ("model-call-alpha", "model%literal"),
        ("model-call-beta", "modelXliteral"),
    )
    call_ids = tuple(call_id for call_id, _model in call_specs)
    request = _captured({"messages": []})
    try:
        for store in stores:
            writer = await store.open_writer(_identity())
            await writer.append(
                (
                    _fact("started"),
                    *(
                        fact
                        for call_id, model in call_specs
                        for fact in (
                            ModelCallFact(
                                source_observation_id=f"{call_id}-started",
                                identity=_identity(),
                                occurred_at=occurred_at,
                                monotonic_ns=2,
                                phase="started",
                                context_started_at=occurred_at,
                                call_id=call_id,
                                system_message_positions=(),
                                output_message_ids=(),
                                model=model,
                                request=request,
                            ),
                            ModelCallFact(
                                source_observation_id=f"{call_id}-completed",
                                identity=_identity(),
                                occurred_at=occurred_at,
                                monotonic_ns=3,
                                phase="completed",
                                call_id=call_id,
                                system_message_positions=(),
                                output_message_ids=(),
                            ),
                        )
                    ),
                    ToolExecutionFact(
                        source_observation_id="tool-cross-field-started",
                        identity=_identity(),
                        occurred_at=occurred_at,
                        monotonic_ns=4,
                        phase="started",
                        execution_id="tool-execution-cross-field",
                        agent_name="bar",
                        tool_name="foo",
                        input=request,
                    ),
                    ToolExecutionFact(
                        source_observation_id="tool-cross-field-completed",
                        identity=_identity(),
                        occurred_at=occurred_at,
                        monotonic_ns=5,
                        phase="completed",
                        execution_id="tool-execution-cross-field",
                        agent_name="bar",
                        tool_name="foo",
                        output=request,
                    ),
                )
            )
            await writer.append(
                (_fact("terminal"), _fact("closed")),
                mandatory=True,
            )
            await writer.aclose()

        observed_orders: list[tuple[str, ...]] = []
        for store in stores:
            key = (await store.snapshot(_identity().thread)).key
            first = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
                limit=1,
            )
            assert first.has_more is True
            assert first.next_started_at is not None
            assert first.next_node_id is not None
            second = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
                limit=1,
                before_started_at=first.next_started_at,
                before_node_id=first.next_node_id,
            )
            observed_orders.append(
                tuple(node.node_id for node in (*first.nodes, *second.nodes))
            )

        expected = tuple(
            sorted(
                call_ids,
                key=lambda value: hashlib.sha256(value.encode()).hexdigest(),
                reverse=True,
            )
        )
        assert observed_orders == [expected, expected]
        for store in stores:
            key = (await store.snapshot(_identity().thread)).key
            literal = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.MODEL},
                    search="%",
                ),
                limit=10,
            )
            assert tuple(node.node_id for node in literal.nodes) == (
                "model-call-alpha",
            )
            cross_field = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.TOOL},
                    search="foo bar",
                ),
                limit=10,
            )
            assert cross_field.nodes == ()
    finally:
        await engine.dispose()


async def test_sqlite_store_auto_setup_round_trip_and_generation_delete(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    borrowed_pool = engine.pool
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=2,
            writer_heartbeat_interval_seconds=0.5,
            follow_poll_seconds=0.01,
        ),
    )
    try:
        writer = await store.open_writer(_identity())
        ordinary = await writer.append((_fact("started"), _fact("input")))
        terminal = await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        snapshot = await store.snapshot(_identity().thread)
        assert snapshot.as_of_seq == 4
        assert snapshot.active_writers == ()
        assert [event.trace_seq for event in ordinary + terminal] == [1, 2, 3, 4]
        assert (
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=4,
                limit=10,
            )
            == ordinary + terminal
        )
        assert [
            event.trace_seq
            for event in await store.read_events_reverse(
                snapshot.key,
                before_seq=5,
                limit=2,
            )
        ] == [4, 3]

        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="tests.projection",
            run_id="run-sql",
            as_of_seq=4,
            state={"count": 4},
        )
        await store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="tests.projection",
                run_id="run-sql",
                as_of_seq=4,
            )
            == checkpoint
        )
        await store.delete(snapshot.key)
        replacement = await store.open_writer(_identity())
        assert replacement.key.generation != snapshot.key.generation
        await replacement.aclose()
        assert engine.pool is borrowed_pool

        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: tuple(
                    inspect(sync_connection).get_table_names()
                )
            )
        assert set(TRACE_TABLE_NAMES) <= set(table_names)
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
        assert project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({_identity().run_id}),
        ).request == {"messages": [{"content": "rebuild-marker"}]}
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


async def test_sql_setup_retries_after_one_retained_task_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-retry.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    attempts = 0

    async def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            raise TraceStoreError("transient setup failure")
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", fail_once)
    try:
        first_waiter = asyncio.create_task(store.setup())
        await first_attempt_started.wait()
        second_waiter = asyncio.create_task(store.setup())
        await asyncio.sleep(0)
        release_first_attempt.set()
        first, second = await asyncio.gather(
            first_waiter,
            second_waiter,
            return_exceptions=True,
        )
        assert isinstance(first, TraceStoreError)
        assert isinstance(second, TraceStoreError)
        assert attempts == 1

        await store.setup()
        assert attempts == 2
        await store.setup()
        assert attempts == 2
    finally:
        release_first_attempt.set()
        await engine.dispose()


async def test_sql_setup_rejects_partial_existing_trace_schema(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"),))
    await writer.aclose()
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_events")

    replacement = SqlAlchemyTraceStore(engine)
    try:
        with pytest.raises(TraceStoreProtocolError, match="incomplete"):
            await replacement.setup()
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "tinkerfin_trace_events" not in table_names
    finally:
        await engine.dispose()


async def test_sql_setup_rejects_foreign_check_constraint(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'check.db'}")
    await SqlAlchemyTraceStore(engine).setup()
    async with engine.begin() as connection:
        ddl = await connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tinkerfin_trace_threads'"
            )
        )
        assert isinstance(ddl, str)
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_threads")
        await connection.exec_driver_sql(
            ddl.rstrip()[:-1] + ", CHECK (persisted_bytes = 0))"
        )

    try:
        with pytest.raises(TraceStoreProtocolError, match="check constraints"):
            await SqlAlchemyTraceStore(engine).setup()
    finally:
        await engine.dispose()


async def test_sql_setup_caller_cancellation_keeps_the_running_shared_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-cancel.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def delayed_setup() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", delayed_setup)
    caller = asyncio.create_task(store.setup())
    try:
        await started.wait()
        caller.cancel("setup caller stopped")
        await asyncio.sleep(0)
        caller.cancel("repeated setup cancellation")
        await asyncio.sleep(0)
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await caller
        assert captured.value.args == ("setup caller stopped",)
        await store.setup()
        assert attempts == 1
        await store.setup()
        assert attempts == 1
    finally:
        release.set()
        await engine.dispose()


async def test_sql_setup_retries_after_the_retained_task_cancels_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'setup-self-cancel.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    attempts = 0

    async def cancel_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", cancel_once)
    try:
        with pytest.raises(asyncio.CancelledError):
            await store.setup()
        await store.setup()
        assert attempts == 2
    finally:
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
        if engine.dialect.name == "mysql":
            from asyncmy.errors import OperationalError

            cause = OperationalError(2013, "connection lost during COMMIT")
        elif engine.dialect.name == "postgresql":
            from asyncpg.exceptions import ConnectionFailureError

            cause = ConnectionFailureError("connection lost during COMMIT")
        else:
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


async def test_sqlite_controlled_clock_is_shared_by_reads_writes_and_peers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "controlled-clock.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    clock = _SqliteClock()
    clock.advance(0.125)
    clock.install(first_engine)
    clock.install(peer_engine)
    first_store = SqlAlchemyTraceStore(first_engine)
    peer_store = SqlAlchemyTraceStore(peer_engine)
    identity = _identity()
    writer = await first_store.open_writer(identity)
    replacement = None
    try:
        committed = await writer.append((_fact("started"),))
        expected_expiry = clock.now + timedelta(
            seconds=first_store.options.writer_lease_seconds
        )

        async def check_clocks(active_run_ids: tuple[str, ...], fence: int) -> None:
            for store in (first_store, peer_store):
                state = await _backend(store).load_ledger_state(
                    TraceLedgerStateRequest(
                        namespace=_identity().namespace,
                        thread_id=identity.thread_id,
                        run_id=identity.run_id,
                        include_active_writers=True,
                    )
                )
                snapshot = await store.snapshot(identity.thread)
                page = await _backend(store).read_event_page(
                    TraceEventPageRequest(
                        key=writer.key,
                        direction="forward",
                        after_seq=0,
                        as_of_seq=1,
                        limit=10,
                    )
                )
                assert state.observed_at == snapshot.observed_at == clock.now
                assert page.observed_at == clock.now
                assert snapshot.active_run_ids == page.active_run_ids == active_run_ids
                assert state.target_writer is not None
                assert state.target_writer.fence == fence
                assert state.target_writer.lease_expires_at == expected_expiry
                assert [event.event_id for event in page.events] == [
                    event.event_id for event in committed
                ]

        await check_clocks((identity.run_id,), 1)
        clock.advance(first_store.options.writer_lease_seconds + 1)
        await check_clocks((), 1)
        replacement = await peer_store.open_writer(identity)
        expected_expiry = clock.now + timedelta(
            seconds=peer_store.options.writer_lease_seconds
        )
        await check_clocks((identity.run_id,), 2)
    finally:
        try:
            if replacement is not None:
                await replacement.aclose()
        finally:
            try:
                await writer.aclose()
            finally:
                try:
                    await first_engine.dispose()
                finally:
                    await peer_engine.dispose()


@pytest.fixture
def paused_writer_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep lease-recovery tests driven by explicit database time and operations."""

    async def paused_heartbeat(_delay: float) -> None:
        # The writer owns and cancels this wait on close. These cases verify
        # committed-result recovery while no background renewal occurs.
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "tinkerfin_tracing.durable_store.asyncio",
        SimpleNamespace(**{**vars(asyncio), "sleep": paused_heartbeat}),
    )


@pytest.mark.usefixtures("paused_writer_heartbeats")
async def test_sqlite_unknown_writer_open_commit_reuses_owner_token(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown-open.db'}")
    clock = _SqliteClock()
    clock.install(engine)
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    await store.setup()
    faults = ExitStack()
    remaining = 1

    async def fail_after_commit() -> None:
        nonlocal remaining
        if remaining:
            remaining -= 1
            clock.advance(store.options.writer_lease_seconds + 1)
            original_error = sqlite3.OperationalError("disk I/O error")
            original_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise DBAPIError(None, None, original_error, connection_invalidated=True)

    writer = None
    try:
        faults.enter_context(after_sql_commit(engine, fail_after_commit))
        writer = await store.open_writer(_identity())
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread)
        assert snapshot.active_run_ids == (_identity().run_id,)
        assert snapshot.observed_at == clock.now
        assert snapshot.as_of_seq == 1
        assert committed[0].trace_seq == 1
        state = await _backend(store).load_ledger_state(
            TraceLedgerStateRequest(
                namespace=_identity().namespace,
                thread_id=_identity().thread_id,
                run_id=_identity().run_id,
            )
        )
        assert state.target_writer is not None
        assert state.target_writer.fence == 1
        assert state.target_writer.lease_expires_at == clock.now + timedelta(
            seconds=store.options.writer_lease_seconds
        )
    finally:
        faults.close()
        try:
            if writer is not None:
                await writer.aclose()
        finally:
            await engine.dispose()


@pytest.mark.usefixtures("paused_writer_heartbeats")
async def test_sqlite_unknown_append_commit_renews_expired_writer_lease(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'append-lease.db'}")
    clock = _SqliteClock()
    clock.install(engine)
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    faults = ExitStack()
    remaining = 1

    async def fail_after_commit() -> None:
        nonlocal remaining
        if remaining:
            remaining -= 1
            clock.advance(store.options.writer_lease_seconds + 1)
            original_error = sqlite3.OperationalError("disk I/O error")
            original_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise DBAPIError(None, None, original_error, connection_invalidated=True)

    try:
        faults.enter_context(after_sql_commit(engine, fail_after_commit))
        first = await writer.append((_fact("started"),))
        second = await writer.append((_fact("input"),))

        assert [first[0].trace_seq, second[0].trace_seq] == [1, 2]
        snapshot = await store.snapshot(_identity().thread)
        stored = await store.read_events(
            snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=10
        )
        assert snapshot.observed_at == clock.now
        assert snapshot.active_run_ids == (_identity().run_id,)
        assert snapshot.as_of_seq == 2
        assert [event.event_id for event in stored] == [
            first[0].event_id,
            second[0].event_id,
        ]
    finally:
        faults.close()
        try:
            await writer.aclose()
        finally:
            await engine.dispose()


@pytest.mark.usefixtures("paused_writer_heartbeats")
async def test_sqlite_proven_append_survives_peer_takeover_during_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "append-takeover.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    clock = _SqliteClock()
    clock.install(first_engine)
    clock.install(peer_engine)
    options = TraceStoreOptions(
        writer_lease_seconds=0.15,
        writer_heartbeat_interval_seconds=0.14,
        commit_retry_attempts=2,
        commit_retry_delay_seconds=0.001,
    )
    first_store = SqlAlchemyTraceStore(
        first_engine,
        options=options,
    )
    peer_store = SqlAlchemyTraceStore(
        peer_engine,
    )
    identity = _identity()
    writer = await first_store.open_writer(identity)
    peer_backend = _backend(peer_store)
    faults = ExitStack()
    first_commit_finished = asyncio.Event()
    release_retry = asyncio.Event()
    remaining = 1

    async def fail_after_commit() -> None:
        nonlocal remaining
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            # Keep the unknown result pending after SQLite released its write lock,
            # so the peer can take ownership before the append retries.
            async with asyncio.timeout(3):
                await release_retry.wait()
            original_error = sqlite3.OperationalError("disk I/O error")
            original_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise DBAPIError(None, None, original_error, connection_invalidated=True)

    faults.enter_context(after_sql_commit(first_engine, fail_after_commit))
    append = asyncio.create_task(writer.append((_fact("started"),)))
    replacement = None
    try:
        await _wait_for_committed_write(append, first_commit_finished)
        clock.advance(options.writer_lease_seconds + 1)
        state = await peer_backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                generation=writer.key.generation,
                run_id=identity.run_id,
            )
        )
        assert state.observed_at == clock.now
        assert state.target_writer is not None
        assert state.target_writer.lease_expires_at < state.observed_at
        assert (await peer_store.snapshot(identity.thread)).active_run_ids == ()
        assert not append.done()
        replacement = await peer_store.open_writer(identity)
        release_retry.set()
        committed = await append
        state = await peer_backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                generation=replacement.key.generation,
                run_id=identity.run_id,
            )
        )
        snapshot = await peer_store.snapshot(identity.thread)
        stored = await peer_store.read_events(
            replacement.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )

        assert snapshot.as_of_seq == 1
        assert [event.event_id for event in committed] == [
            event.event_id for event in stored
        ]
        assert state.target_writer is not None
        assert state.target_writer.fence == 2
        assert state.target_writer.lease_expires_at > state.observed_at
    finally:
        release_retry.set()
        faults.close()
        if not append.done():
            append.cancel()
        await asyncio.gather(append, return_exceptions=True)
        try:
            # Stop the current owner's heartbeats before closing the stale writer.
            if replacement is not None:
                await replacement.aclose()
        finally:
            try:
                await writer.aclose()
            finally:
                try:
                    await first_engine.dispose()
                finally:
                    await peer_engine.dispose()


async def test_sqlite_retry_exhaustion_uses_stable_store_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    original = AsyncConnection.exec_driver_sql
    original_error = sqlite3.OperationalError("database is locked")
    original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    async def unavailable(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> Any:
        if connection.engine is engine and statement == "BEGIN IMMEDIATE":
            raise DBAPIError(None, None, original_error, False)
        return await original(connection, statement, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncConnection, "exec_driver_sql", unavailable)
        with pytest.raises(TraceStoreTimeout) as captured:
            await writer.append((_fact("started"),))
        assert isinstance(captured.value.cause, DBAPIError)
    finally:
        monkeypatch.setattr(AsyncConnection, "exec_driver_sql", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_store_rejects_corrupt_opaque_payload(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'corrupt.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    try:
        committed = await writer.append((_fact("started"),))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_events SET payload = :payload "
                    "WHERE event_id = :event_id"
                ),
                {"payload": b"{}", "event_id": committed[0].event_id},
            )
        snapshot = await store.snapshot(_identity().thread)
        with pytest.raises(TraceStoreProtocolError, match="digest mismatch"):
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=1,
            )
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_cancelled_lock_wait_releases_connection_for_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cancel.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    blocker_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    contended = asyncio.Event()

    def observe_busy(context: Any) -> None:
        if context.statement == "BEGIN IMMEDIATE":
            contended.set()

    sqlalchemy_event.listen(engine.sync_engine, "handle_error", observe_busy)
    blocker = await blocker_engine.connect()
    try:
        await blocker.exec_driver_sql("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(writer.append((_fact("started"),)))
        await contended.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await blocker.rollback()

        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
        async with engine.connect() as borrowed_again:
            assert await borrowed_again.scalar(text("PRAGMA busy_timeout")) == 30_000
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "handle_error", observe_busy)
        await blocker.close()
        await writer.aclose()
        await engine.dispose()
        await blocker_engine.dispose()


async def test_sqlite_cancelled_transaction_releases_writer_lock_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_closed, release_close = ThreadEvent(), ThreadEvent()
    close_started = asyncio.Event()
    loop = asyncio.get_running_loop()

    class SlowClosingConnection(sqlite3.Connection):
        def close(self) -> None:
            loop.call_soon_threadsafe(close_started.set)
            if not release_close.wait(5):
                raise AssertionError("driver close was not released")
            super().close()
            driver_closed.set()

    database = tmp_path / "cancelled-transaction.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"factory": SlowClosingConnection},
    )
    peer_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}", connect_args={"timeout": 0}
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    faults = ExitStack()
    transaction_started = asyncio.Event()
    release_transaction = asyncio.Event()

    async def held_transaction() -> None:
        transaction_started.set()
        await release_transaction.wait()

    faults.enter_context(after_sql_command(engine, "BEGIN IMMEDIATE", held_transaction))
    writing = asyncio.create_task(writer.append((_fact("started"),)))
    cancellation_requested = False
    try:
        await asyncio.wait_for(transaction_started.wait(), timeout=3)
        cancellation_requested = True
        writing.cancel()
        await close_started.wait()
        assert not writing.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        assert driver_closed.is_set()
        async with peer_engine.connect() as peer:
            await peer.exec_driver_sql("BEGIN IMMEDIATE")
            await peer.rollback()
        faults.close()
        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
        await writer.aclose()
    finally:
        release_close.set()
        release_transaction.set()
        if not writing.done():
            writing.cancel()
        await asyncio.gather(writing, return_exceptions=True)
        faults.close()
        try:
            try:
                if cancellation_requested:
                    assert await asyncio.to_thread(driver_closed.wait, 3)
            finally:
                await writer.aclose()
        finally:
            try:
                await engine.dispose()
            finally:
                await peer_engine.dispose()


async def test_sqlite_zero_event_close_removes_checkpoint_rows(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orphan.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await store.save_projection_checkpoint(
        TraceProjectionCheckpoint(
            key=writer.key,
            projection_name="zero.checkpoint",
            run_id=None,
            as_of_seq=0,
            state={"count": 0},
        ),
        expected_as_of_seq=None,
    )
    try:
        await writer.aclose()
        async with engine.connect() as connection:
            checkpoint_count = await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_trace_projection_checkpoints")
            )
        assert checkpoint_count == 0
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_checkpoint_survives_peer_advancement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "checkpoint-advance.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    options = TraceStoreOptions(
        commit_retry_attempts=2,
        commit_retry_delay_seconds=0.001,
    )
    first = SqlAlchemyTraceStore(
        first_engine,
        options=options,
    )
    peer = SqlAlchemyTraceStore(
        peer_engine,
        options=options,
    )
    writer = await first.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    faults = ExitStack()
    first_commit_finished = asyncio.Event()
    release_retry = asyncio.Event()
    remaining = 1

    async def fail_after_commit() -> None:
        nonlocal remaining
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            async with asyncio.timeout(3):
                await release_retry.wait()
            original_error = sqlite3.OperationalError("disk I/O error")
            original_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise DBAPIError(None, None, original_error, connection_invalidated=True)

    first_checkpoint = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="advancing.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second_checkpoint = first_checkpoint.model_copy(
        update={"as_of_seq": 2, "state": {"count": 2}}
    )
    faults.enter_context(after_sql_commit(first_engine, fail_after_commit))
    saving = asyncio.create_task(
        first.save_projection_checkpoint(
            first_checkpoint,
            expected_as_of_seq=None,
        )
    )
    try:
        await _wait_for_committed_write(saving, first_commit_finished)
        assert (
            await peer.save_projection_checkpoint(
                second_checkpoint,
                expected_as_of_seq=1,
            )
            == second_checkpoint
        )
        release_retry.set()
        assert await saving == first_checkpoint
        assert (
            await peer.load_projection_checkpoint(
                writer.key,
                projection_name=second_checkpoint.projection_name,
                run_id=None,
                as_of_seq=2,
            )
            == second_checkpoint
        )
    finally:
        release_retry.set()
        faults.close()
        if not saving.done():
            saving.cancel()
        await asyncio.gather(saving, return_exceptions=True)
        try:
            await writer.aclose()
        finally:
            try:
                await first_engine.dispose()
            finally:
                await peer_engine.dispose()


async def test_sqlite_rejects_historical_checkpoint_as_new_cas_retry(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'historical.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    first = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="historical.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second = first.model_copy(update={"as_of_seq": 2, "state": {"count": 2}})
    try:
        await store.save_projection_checkpoint(first, expected_as_of_seq=None)
        await store.save_projection_checkpoint(second, expected_as_of_seq=1)
        with pytest.raises(TraceProjectionCheckpointConflict):
            await store.save_projection_checkpoint(first, expected_as_of_seq=None)
    finally:
        await writer.aclose()
        await engine.dispose()
