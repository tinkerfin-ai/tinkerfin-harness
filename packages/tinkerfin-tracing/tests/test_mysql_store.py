"""Disposable MySQL 8.x Trace Store concurrency, follow, lease, and fencing E2E."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import sys
import warnings
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest
from asyncmy.errors import OperationalError
from pydantic import JsonValue
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SAWarning
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from tests.support.sql_faults import after_sql_commit

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    ModelCallFact,
    RunFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceLimits,
    TraceProjectionCheckpoint,
    Tracer,
    TraceStoreOptions,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing._graph_projection import project_trace_graph_node
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.errors import (
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
)
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
    _SqlAlchemyTraceLedgerBackend,
)

pytestmark = pytest.mark.docker_integration


async def test_mysql_payload_tables_compress_without_changing_retained_content() -> (
    None
):
    engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    store = SqlAlchemyTraceStore(engine)
    try:
        await store.setup()
        async with engine.connect() as connection:
            options = (
                await connection.execute(
                    text(
                        "SELECT TABLE_NAME, ROW_FORMAT FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name IN "
                        "('tinkerfin_trace_events','tinkerfin_trace_projection_checkpoints')"
                    )
                )
            ).all()
        assert len(options) == 2
        assert all(row.ROW_FORMAT == "Compressed" for row in options)
        writer = await store.open_writer(_identity("compressed-content"))
        try:
            values = await writer.append((_fact("compressed-content", "started"),))
            values += await writer.append(
                (
                    _fact("compressed-content", "terminal"),
                    _fact("compressed-content", "closed"),
                ),
                mandatory=True,
            )
            checkpoint = TraceProjectionCheckpoint(
                key=writer.key,
                projection_name="compressed",
                run_id=None,
                as_of_seq=3,
                state={"text": "工具输入与结果" * 128},
            )
            await store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
            async with engine.connect() as connection:
                for table_name in (
                    "tinkerfin_trace_events",
                    "tinkerfin_trace_projection_checkpoints",
                ):
                    await connection.exec_driver_sql(
                        f"ALTER TABLE {table_name} ROW_FORMAT=DYNAMIC, KEY_BLOCK_SIZE=0, ALGORITHM=INPLACE, LOCK=NONE"
                    )
                    await connection.exec_driver_sql(
                        f"ALTER TABLE {table_name} ROW_FORMAT=COMPRESSED, KEY_BLOCK_SIZE=8, ALGORITHM=INPLACE, LOCK=NONE"
                    )
            assert (
                await store.read_events(writer.key, after_seq=0, as_of_seq=3, limit=3)
                == values
            )
            assert (
                await store.load_projection_checkpoint(
                    writer.key, projection_name="compressed", run_id=None, as_of_seq=3
                )
                == checkpoint
            )
        finally:
            await writer.aclose()
    finally:
        await engine.dispose()


def _captured(value: JsonValue) -> CapturedValue:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(encoded),
        value=value,
    )


async def test_mysql_cancelled_reads_return_pool_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    blocker_engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    store = SqlAlchemyTraceStore(
        engine,
    )
    writer = await store.open_writer(_identity("cancelled-read"))
    try:
        await writer.append((_fact("cancelled-read", "started"),))
        snapshot = await store.snapshot(_identity("cancelled-read").thread)
        async with engine.connect() as connection:
            connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
        assert isinstance(connection_id, int)
        blocker = await blocker_engine.connect()
        try:
            await blocker.exec_driver_sql(
                "LOCK TABLES tinkerfin_trace_graph_nodes WRITE"
            )
            caplog.clear()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                query = asyncio.create_task(
                    store.query_trace_graph(
                        snapshot.key,
                        run_ids=("cancelled-read",),
                        where=TraceGraphFilter(),
                        limit=10,
                    )
                )
                await _wait_for_mysql_lock(connection_id)
                assert not query.done()
                for attempt in range(8):
                    if query.done():
                        break
                    query.cancel(f"cancelled read {attempt + 1}")
                    await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError) as captured:
                    await query
                assert captured.value.args == ("cancelled read 1",)

                pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
                assert pool.checkedout() == 0
                gc.collect()
                await asyncio.sleep(0)
            assert not [item for item in caught if issubclass(item.category, SAWarning)]
            assert not [
                record
                for record in caplog.records
                if record.name.startswith("sqlalchemy.pool")
                and record.levelno >= logging.ERROR
            ]
        finally:
            await blocker.exec_driver_sql("UNLOCK TABLES")
            await blocker.close()

        assert (await store.snapshot(snapshot.key.thread)).key == snapshot.key
    finally:
        await writer.aclose()
        await blocker_engine.dispose()
        await engine.dispose()


async def test_mysql_cancelled_writes_preserve_data_and_return_pool_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    blocker_engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    store = SqlAlchemyTraceStore(
        engine,
    )
    writer = await store.open_writer(_identity("cancelled-write"))
    try:
        await writer.append((_fact("cancelled-write", "started"),))
        await writer.append(
            (
                _fact("cancelled-write", "terminal"),
                _fact("cancelled-write", "closed"),
            ),
            mandatory=True,
        )
        await writer.aclose()
        snapshot = await store.snapshot(_identity("cancelled-write").thread)
        async with engine.connect() as connection:
            connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
        assert isinstance(connection_id, int)
        blocker = await blocker_engine.connect()
        try:
            await blocker.exec_driver_sql("LOCK TABLES tinkerfin_trace_threads WRITE")
            caplog.clear()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                deletion = asyncio.create_task(store.delete(snapshot.key))
                await _wait_for_mysql_lock(connection_id)
                assert not deletion.done()
                for attempt in range(8):
                    if deletion.done():
                        break
                    deletion.cancel(f"cancelled write {attempt + 1}")
                    await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError) as captured:
                    await deletion
                assert captured.value.args == ("cancelled write 1",)

                pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
                assert pool.checkedout() == 0
                gc.collect()
                await asyncio.sleep(0)
            assert not [item for item in caught if issubclass(item.category, SAWarning)]
            assert not [
                record
                for record in caplog.records
                if record.name.startswith("sqlalchemy.pool")
                and record.levelno >= logging.ERROR
            ]
        finally:
            await blocker.exec_driver_sql("UNLOCK TABLES")
            await blocker.close()

        assert (await store.snapshot(snapshot.key.thread)).key == snapshot.key
    finally:
        await writer.aclose()
        await blocker_engine.dispose()
        await engine.dispose()


async def test_mysql_graph_query_filters_and_joins_ledger_details() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    namespace = _identity("graph-query").namespace
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity("graph-query"))
    marker = "mysql-final-request"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("graph-query", "started"),
                ModelCallFact(
                    source_observation_id="mysql-model-start",
                    identity=_identity("graph-query"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    context_started_at=now,
                    call_id="mysql-model-call",
                    system_message_positions=(0,),
                    output_message_ids=(),
                    provider="deepseek",
                    model="deepseek-chat",
                    request=_captured(
                        {"messages": [{"messageType": "system", "content": marker}]}
                    ),
                ),
                ModelCallFact(
                    source_observation_id="mysql-model-completed",
                    identity=_identity("graph-query"),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id="mysql-model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        snapshot = await store.snapshot(_identity("graph-query").thread)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-query",),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.MODEL},
                providers={"deepseek"},
                search="deepseek-chat",
            ),
            limit=10,
        )

        assert len(page.nodes) == 1
        start_fact = page.nodes[0].started_event.fact
        assert isinstance(start_fact, ModelCallFact)
        assert start_fact.request is not None
        assert start_fact.request.value == {
            "messages": [{"messageType": "system", "content": marker}]
        }
        context_page = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-query",),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.CONTEXT}),
            limit=10,
        )
        context = project_trace_graph_node(
            context_page.nodes[0],
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"graph-query"}),
        )
        assert context.content == marker
        assert context.request is None
        assert context_page.nodes[0].request_seq == page.nodes[0].request_seq
        literal_wildcard = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-query",),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.MODEL},
                search="%",
            ),
            limit=10,
        )
        assert literal_wildcard.nodes == ()
        async with engine.connect() as connection:
            plan = (
                (
                    await connection.execute(
                        text(
                            "EXPLAIN SELECT node_id "
                            "FROM tinkerfin_trace_graph_nodes "
                            "WHERE namespace_hash = UNHEX(SHA2(:namespace, 256)) "
                            "AND thread_hash = UNHEX(SHA2(:thread_id, 256)) "
                            "AND generation = :generation "
                            "AND run_hash = UNHEX(SHA2(:run_id, 256)) "
                            "AND kind = 'model'"
                        ),
                        {
                            "namespace": namespace,
                            "thread_id": snapshot.key.thread_id,
                            "generation": snapshot.key.generation,
                            "run_id": "graph-query",
                        },
                    )
                )
                .mappings()
                .one()
            )
            index_names = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]
                    for item in inspect(sync_connection).get_indexes(
                        "tinkerfin_trace_graph_nodes"
                    )
                }
            )
        assert plan["key"] in {"PRIMARY", "ix_tinkerfin_trace_graph_run"}
        assert index_names == {"ix_tinkerfin_trace_graph_run"}
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_mysql_graph_scope_closure_accepts_64_levels_and_rejects_65() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    store = SqlAlchemyTraceStore(
        engine,
    )
    identity = _identity("scope-depth")
    writer = await store.open_writer(identity)
    namespaces = tuple(
        tuple(f"tools:{index}" for index in range(depth)) for depth in range(1, 66)
    )
    try:
        await writer.append(
            (
                _fact(identity.run_id, "started"),
                *(
                    SubagentFact(
                        source_observation_id=f"mysql-depth-{depth}",
                        identity=identity,
                        graph_namespace=namespace[:-1],
                        parent_subagent_id=(
                            scope_id(
                                "subagent",
                                namespaces[depth - 2],
                                namespaces[depth - 2][-1],
                            )
                            if depth > 1
                            else None
                        ),
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
        snapshot = await store.snapshot(identity.thread)

        legal = await store.query_trace_graph(
            snapshot.key,
            run_ids=(identity.run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                graph_namespaces={namespaces[63][:-1]},
            ),
            limit=1,
            max_nodes=65,
        )
        assert len(legal.nodes) == 64

        with pytest.raises(TraceStoreProtocolError, match="at most 64 levels"):
            await store.query_trace_graph(
                snapshot.key,
                run_ids=(identity.run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    agent_names={"agent-65"},
                ),
                limit=1,
                max_nodes=65,
            )
        await store.delete(snapshot.key)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_mysql_graph_clears_a_tool_parent_gap_after_model_completion() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    store = SqlAlchemyTraceStore(
        engine,
    )
    identity = _identity("resolved-tool-parent")
    writer = await store.open_writer(identity)
    now = datetime.now(UTC)
    model_id = "mysql-parent-model"
    try:
        await writer.append(
            (
                _fact("resolved-tool-parent", "started"),
                ModelCallFact(
                    source_observation_id="mysql-parent-model-start",
                    identity=identity,
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    context_started_at=now,
                    call_id=model_id,
                    parent_call_id="agent-call",
                    request=_captured({"messages": []}),
                    system_message_positions=(),
                    output_message_ids=(),
                ),
                ToolFact(
                    source_observation_id="mysql-parent-tool-start",
                    identity=identity,
                    occurred_at=now,
                    monotonic_ns=3,
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
                    source_observation_id="mysql-parent-model-complete",
                    identity=identity,
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id=model_id,
                    system_message_positions=(),
                    output_message_ids=(),
                    tool_call_ids=("tool-call",),
                ),
            )
        )
        await writer.append(
            (
                _fact("resolved-tool-parent", "terminal"),
                _fact("resolved-tool-parent", "closed"),
            ),
            mandatory=True,
        )
        await writer.aclose()
        snapshot = await store.snapshot(identity.thread)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(identity.run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )

        assert len(page.nodes) == 1
        assert page.nodes[0].model_call_id == model_id
        assert page.nodes[0].link_issue is None
        await store.delete(snapshot.key)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_mysql_graph_keeps_lineage_parent_and_tool_failure_evidence() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    store = SqlAlchemyTraceStore(
        engine,
    )
    parent_identity = _identity("lineage-parent")
    child_identity = _identity("lineage-child")
    parent = await store.open_writer(parent_identity)
    child = await store.open_writer(child_identity)
    now = datetime.now(UTC)
    try:
        await parent.append(
            (
                _fact(parent_identity.run_id, "started"),
                SubagentFact(
                    source_observation_id="mysql-subagent-start",
                    identity=parent_identity,
                    graph_namespace=(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    subagent_id="mysql-subagent",
                    agent_name="KÄ研究Researcher",
                    parent_tool_call_id="call-task",
                    model_call_id="mysql-model",
                    input=_captured({"description": "research"}),
                    status="running",
                ),
                ToolFact(
                    source_observation_id="mysql-tool-start",
                    identity=parent_identity,
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    tool_call_id="mysql-tool",
                    source_tool_call_id="mysql-tool",
                    parent_call_id="mysql-model",
                    tool_name="read_file",
                ),
                ToolExecutionFact(
                    source_observation_id="mysql-execution-start",
                    identity=parent_identity,
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="started",
                    execution_id="mysql-execution",
                    source_tool_call_id="mysql-tool",
                    tool_call_namespace=(),
                    tool_name="read_file",
                    input=_captured({"path": "missing.txt"}),
                ),
                ToolExecutionFact(
                    source_observation_id="mysql-execution-failed",
                    identity=parent_identity,
                    occurred_at=now,
                    monotonic_ns=5,
                    phase="failed",
                    execution_id="mysql-execution",
                    source_tool_call_id="mysql-tool",
                    tool_call_namespace=(),
                    tool_name="read_file",
                    error_type="FileNotFoundError",
                    error_message=_captured("missing.txt was not found"),
                    failure_origin=True,
                ),
                ToolFact(
                    source_observation_id="mysql-tool-result",
                    identity=parent_identity,
                    occurred_at=now,
                    monotonic_ns=6,
                    phase="result",
                    tool_call_id="mysql-tool",
                    source_tool_call_id="mysql-tool",
                    parent_call_id="mysql-model",
                    tool_name="read_file",
                    content=_captured("tool error result"),
                    result_status="error",
                ),
            )
        )
        await child.append(
            (
                _fact(child_identity.run_id, "started"),
                SubagentFact(
                    source_observation_id="mysql-subagent-completed",
                    identity=child_identity,
                    graph_namespace=(),
                    occurred_at=now + timedelta(seconds=5),
                    monotonic_ns=7,
                    phase="completed",
                    subagent_id="mysql-subagent",
                    agent_name="KÄ研究Researcher",
                    status="succeeded",
                ),
            )
        )
        snapshot = await store.snapshot(parent_identity.thread)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT, TraceGraphNodeKind.TOOL}
            ),
            limit=10,
        )
        subagent = next(
            node for node in page.nodes if node.kind is TraceGraphNodeKind.SUBAGENT
        )
        tool = next(node for node in page.nodes if node.kind is TraceGraphNodeKind.TOOL)
        projected_tool = project_trace_graph_node(
            tool,
            turn_id="turn:mysql",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({parent_identity.run_id, child_identity.run_id}),
        )

        assert subagent.parent_subagent_id is None
        assert subagent.model_call_id == "mysql-model"
        assert subagent.link_issue is None
        assert subagent.started_at == now
        assert projected_tool.result == "tool error result"
        assert projected_tool.failure is not None
        assert projected_tool.failure.error_type == "FileNotFoundError"
        assert projected_tool.failure.message == "missing.txt was not found"
        exact_unicode = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                search="Ä",
            ),
            limit=10,
        )
        different_unicode_case = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                search="ä",
            ),
            limit=10,
        )
        exact_chinese = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                search="研究",
            ),
            limit=10,
        )
        exact_kelvin = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                search="K",
            ),
            limit=10,
        )
        ascii_does_not_fold_kelvin = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.SUBAGENT},
                search="k",
            ),
            limit=10,
        )
        ascii_case_insensitive = await store.query_trace_graph(
            snapshot.key,
            run_ids=(parent_identity.run_id, child_identity.run_id),
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
    finally:
        await parent.aclose()
        await child.aclose()
        await engine.dispose()


def _backend(store: SqlAlchemyTraceStore) -> _SqlAlchemyTraceLedgerBackend:
    backend = store.backend
    assert isinstance(backend, _SqlAlchemyTraceLedgerBackend)
    return backend


@pytest.fixture(autouse=True)
def _use_disposable_mysql(
    monkeypatch: pytest.MonkeyPatch,
    trace_mysql_url: str,
) -> None:
    """Route every case to the repository-owned disposable MySQL 8.4 service."""

    monkeypatch.setenv("TINKERFIN_TRACE_MYSQL_URL", trace_mysql_url)


def _url() -> str:
    value = os.getenv("TINKERFIN_TRACE_MYSQL_URL")
    if not value:
        pytest.skip("TINKERFIN_TRACE_MYSQL_URL is not configured")
    return value


async def _wait_for_mysql_lock(connection_id: int) -> None:
    observer_engine = create_async_engine(_url())
    try:
        async with observer_engine.connect() as observer:
            async with asyncio.timeout(5):
                while True:
                    row = (
                        await observer.execute(
                            text(
                                "SELECT PROCESSLIST_STATE, EXISTS(SELECT 1 FROM "
                                "performance_schema.data_lock_waits w WHERE w.REQUESTING_THREAD_ID = t.THREAD_ID) "
                                "AS row_wait FROM performance_schema.threads t WHERE PROCESSLIST_ID = :id"
                            ),
                            {"id": connection_id},
                        )
                    ).one_or_none()
                    if row is not None and (
                        row.row_wait
                        or (
                            isinstance(row.PROCESSLIST_STATE, str)
                            and "lock" in row.PROCESSLIST_STATE.lower()
                        )
                    ):
                        return
    finally:
        await observer_engine.dispose()


def _identity(run_id: str) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="mysql-trace-thread", run_id=run_id)


def _fact(run_id: str, phase: Literal["started", "terminal", "closed"]) -> RunFact:
    return RunFact(
        source_observation_id=f"mysql-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase != "started" else None,
    )


async def test_mysql_cross_instance_sequence_follow_and_expired_writer_fencing() -> (
    None
):
    first_engine = create_async_engine(_url())
    second_engine = create_async_engine(_url())
    options = TraceStoreOptions(
        writer_lease_seconds=2,
        writer_heartbeat_interval_seconds=0.5,
        follow_poll_seconds=0.01,
    )
    first_store = SqlAlchemyTraceStore(
        first_engine,
        options=options,
    )
    second_store = SqlAlchemyTraceStore(
        second_engine,
        options=options,
    )
    try:
        first = await first_store.open_writer(_identity("run-a"))
        with pytest.raises(TraceRunConflict):
            await second_store.open_writer(_identity("run-a"))
        second = await second_store.open_writer(_identity("run-b"))
        first_batch, second_batch = await asyncio.gather(
            first.append((_fact("run-a", "started"),)),
            second.append((_fact("run-b", "started"),)),
        )
        assert {first_batch[0].trace_seq, second_batch[0].trace_seq} == {1, 2}

        snapshot = await second_store.snapshot(_identity("run-a").thread)
        follower = second_store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
        current = await anext(follower)
        assert current.events == ()
        assert current.active_run_ids == ("run-a", "run-b")
        waiting = asyncio.create_task(anext(follower))
        terminal = await first.append(
            (_fact("run-a", "terminal"), _fact("run-a", "closed")),
            mandatory=True,
        )
        assert (await waiting).events == terminal
        await follower.aclose()

        async with second_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = JSON_QUOTE('run-b') AND active = 1"
                )
            )
        replacement = await first_store.open_writer(_identity("run-b"))
        with pytest.raises(TraceStoreProtocolError):
            await second.append((_fact("run-b", "started"),))
        await replacement.append(
            (_fact("run-b", "terminal"), _fact("run-b", "closed")),
            mandatory=True,
        )
        await replacement.aclose()

        completed = await first_store.open_writer(_identity("run-c"))
        await completed.append(
            (_fact("run-c", "terminal"), _fact("run-c", "closed")),
            mandatory=True,
        )
        async with second_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = JSON_QUOTE('run-c') AND active = 1"
                )
            )
        with pytest.raises(TraceRunConflict):
            await second_store.open_writer(_identity("run-c"))
        await completed.aclose()

        await first.aclose()
        await second.aclose()
        final = await first_store.snapshot(_identity("run-a").thread)
        checkpoint = TraceProjectionCheckpoint(
            key=final.key,
            projection_name="mysql.concurrent-projection",
            run_id=None,
            as_of_seq=final.as_of_seq,
            state={"count": final.as_of_seq},
        )
        assert await asyncio.gather(
            first_store.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            ),
            second_store.save_projection_checkpoint(
                checkpoint.model_copy(deep=True),
                expected_as_of_seq=None,
            ),
        ) == [checkpoint, checkpoint]
        with pytest.raises(TraceStoreProtocolError, match="idempotency"):
            await second_store.save_projection_checkpoint(
                checkpoint.model_copy(update={"state": {"count": -1}}),
                expected_as_of_seq=None,
            )
        await first_store.delete(final.key)
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


async def test_mysql_concurrent_first_setup_reflects_the_only_current_schema() -> None:
    database_name = f"tinkerfin_trace_setup_{uuid4().hex}"
    configured_url = make_url(_url())
    admin_engine = create_async_engine(configured_url.set(database="mysql"))
    first_engine = None
    second_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )
        database_url = configured_url.set(database=database_name)
        first_engine = create_async_engine(database_url)
        second_engine = create_async_engine(database_url)
        first = SqlAlchemyTraceStore(first_engine)
        second = SqlAlchemyTraceStore(second_engine)

        await asyncio.gather(first.setup(), second.setup())

        async with first_engine.connect() as connection:
            reflected = await connection.run_sync(
                lambda sync_connection: {
                    "tables": tuple(inspect(sync_connection).get_table_names()),
                    "events": {
                        item["name"]: item
                        for item in inspect(sync_connection).get_columns(
                            "tinkerfin_trace_events"
                        )
                    },
                    "writers": {
                        item["name"]: item
                        for item in inspect(sync_connection).get_columns(
                            "tinkerfin_trace_writers"
                        )
                    },
                }
            )
        assert set(reflected["tables"]) == set(TRACE_TABLE_NAMES)
        assert type(reflected["events"]["payload"]["type"]).__name__ == "LONGBLOB"
        assert reflected["events"]["payload"]["comment"]
        assert {"terminal_committed", "closed_committed"} <= set(reflected["writers"])
    finally:
        if first_engine is not None:
            await first_engine.dispose()
        if second_engine is not None:
            await second_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"DROP DATABASE IF EXISTS `{database_name}`"
            )
        await admin_engine.dispose()


async def test_mysql_process_crash_exposes_missing_tail_then_allows_takeover() -> None:
    namespace = f"mysql-crash-{uuid4().hex}"
    options = TraceStoreOptions(
        writer_lease_seconds=0.6,
        writer_heartbeat_interval_seconds=0.2,
        follow_poll_seconds=0.01,
    )
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(engine, options=options)
    await store.setup()
    child_source = """
import asyncio
import os
import sys
from datetime import UTC, datetime
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import RunFact, TraceStoreOptions
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore

async def main():
    url, namespace = sys.argv[1:]
    engine = create_async_engine(url)
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.6,
            writer_heartbeat_interval_seconds=0.2,
            follow_poll_seconds=0.01,
        ),
    )
    identity = RunIdentity(namespace=namespace, thread_id="mysql-crash-thread", run_id="mysql-crash-run")
    writer = await store.open_writer(identity)
    await writer.append((RunFact(
        source_observation_id="mysql-crash-started",
        identity=identity,
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    ),))
    os._exit(0)

asyncio.run(main())
"""
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child_source,
            _url(),
            namespace,
        )
        assert await child.wait() == 0
        # Expire the stopped process's persisted lease deterministically.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers SET lease_expires_at = "
                    "UTC_TIMESTAMP(6) - INTERVAL 1 SECOND "
                    "WHERE namespace_hash = UNHEX(SHA2(:namespace, 256))"
                ),
                {"namespace": namespace},
            )

        incomplete = await Tracer(store=store).get(
            ThreadIdentity(namespace=namespace, thread_id="mysql-crash-thread")
        )
        assert incomplete.status.execution == "unknown"
        assert incomplete.completeness.missing_tail is True

        replacement = await store.open_writer(
            RunIdentity(
                namespace=namespace,
                thread_id="mysql-crash-thread",
                run_id="mysql-crash-run",
            )
        )
        replacement_identity = RunIdentity(
            namespace=namespace,
            thread_id="mysql-crash-thread",
            run_id="mysql-crash-run",
        )
        await replacement.append(
            (
                RunFact(
                    source_observation_id="mysql-crash-terminal",
                    identity=replacement_identity,
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=2,
                    phase="terminal",
                    outcome="succeeded",
                ),
                RunFact(
                    source_observation_id="mysql-crash-closed",
                    identity=replacement_identity,
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=3,
                    phase="closed",
                    outcome="succeeded",
                ),
            ),
            mandatory=True,
        )
        await replacement.aclose()

        complete = await Tracer(store=store).get(
            ThreadIdentity(namespace=namespace, thread_id="mysql-crash-thread")
        )
        assert complete.status.execution == "succeeded"
        assert complete.completeness.missing_tail is False
        await store.delete(complete.key)
    finally:
        await engine.dispose()


async def test_mysql_retryable_or_unknown_commit_reuses_event_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_code = 2013
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    identity = _identity(f"retry-{error_code}")
    writer = await store.open_writer(identity)
    faults = ExitStack()
    remaining = 1

    async def fail_after_commit() -> None:
        nonlocal remaining
        if remaining:
            remaining -= 1
            raise DBAPIError(
                None,
                None,
                OperationalError(error_code, "injected transaction outcome"),
                error_code == 2013,
            )

    try:
        faults.enter_context(after_sql_commit(engine, fail_after_commit))
        committed = await writer.append((_fact(f"retry-{error_code}", "started"),))
        snapshot = await store.snapshot(identity.thread)
        stored = await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
        assert snapshot.as_of_seq == 1
        assert tuple(event.event_id for event in stored) == (committed[0].event_id,)
    finally:
        faults.close()
        await writer.aclose()
        snapshot = await store.snapshot(identity.thread)
        await store.delete(snapshot.key)
        await engine.dispose()


async def test_mysql_longblob_round_trips_event_above_generic_blob_limit() -> None:
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
    )
    identity = _identity("longblob")
    writer = await store.open_writer(identity)
    try:
        content = "x" * (128 * 1024)
        fact = MessageFact(
            source_observation_id="mysql-longblob-message",
            identity=identity,
            occurred_at=datetime.now(UTC),
            monotonic_ns=1,
            phase="reconciled",
            message_id="message:mysql-longblob",
            source_message_id="mysql-longblob",
            role="assistant",
            content=CapturedValue(
                disposition="inline",
                safe_size_bytes=len(
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ),
                value=content,
            ),
        )
        committed = await writer.append((fact,))
        assert committed[0].persisted_bytes > 65_535
        await writer.append(
            (_fact("longblob", "terminal"), _fact("longblob", "closed")),
            mandatory=True,
        )
        await writer.aclose()

        snapshot = await store.snapshot(identity.thread)
        stored = await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
        assert stored[0].fact == fact
        await store.delete(snapshot.key)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_mysql_expired_incomplete_writer_keeps_terminal_reserve() -> None:
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
        limits=TraceLimits(
            max_event_bytes=1024,
            max_thread_events=16,
            max_thread_bytes=64 * 1024,
            max_tracer_threads=1,
            max_tracer_bytes=64 * 1024,
            terminal_reserve_events_per_run=2,
            terminal_reserve_bytes_per_run=2048,
        ),
        options=TraceStoreOptions(
            writer_lease_seconds=10,
            writer_heartbeat_interval_seconds=5,
        ),
    )
    first = await store.open_writer(_identity("reserve-a"))
    second = await store.open_writer(_identity("reserve-b"))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = JSON_QUOTE('reserve-a') AND active = 1"
                )
            )
        await second.append(
            tuple(_fact("reserve-b", "started") for _index in range(12))
        )
        with pytest.raises(TraceQuotaExceeded, match="event quota"):
            await second.append((_fact("reserve-b", "started"),))

        replacement = await store.open_writer(_identity("reserve-a"))
        with pytest.raises(TraceStoreProtocolError):
            await first.append((_fact("reserve-a", "started"),))
        await replacement.append(
            (_fact("reserve-a", "terminal"), _fact("reserve-a", "closed")),
            mandatory=True,
        )
        await second.append(
            (_fact("reserve-b", "terminal"), _fact("reserve-b", "closed")),
            mandatory=True,
        )
        await replacement.aclose()
        await second.aclose()
        await first.aclose()
        snapshot = await store.snapshot(_identity("reserve-a").thread)
        assert snapshot.as_of_seq == 16
        await store.delete(snapshot.key)
    finally:
        await first.aclose()
        await second.aclose()
        await engine.dispose()


async def test_mysql_backend_satisfies_public_cross_instance_verifier() -> None:
    first_engine = create_async_engine(_url(), pool_pre_ping=True)
    second_engine = create_async_engine(_url(), pool_pre_ping=True)
    namespace = f"mysql-backend-contract-{uuid4().hex}"
    options = TraceStoreOptions()
    try:
        await verify_trace_ledger_backend(
            _SqlAlchemyTraceLedgerBackend(
                first_engine,
                options=options,
            ),
            _SqlAlchemyTraceLedgerBackend(
                second_engine,
                options=options,
            ),
            namespace=namespace,
            options=options,
        )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


async def test_mysql_writer_lease_starts_after_namespace_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    identity = _identity("lease-clock")
    namespace = identity.namespace
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=1,
            writer_heartbeat_interval_seconds=0.9,
            commit_retry_attempts=5,
            commit_retry_delay_seconds=0.001,
        ),
    )
    initial = await store.open_writer(identity)
    await initial.aclose()
    writer = None
    open_writer = None
    connection_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    execute = AsyncConnection.execute

    async def observe_owner(connection, statement, *args, **kwargs):
        if not connection_ready.done() and str(statement).startswith(
            "INSERT INTO tinkerfin_trace_namespaces"
        ):
            connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            assert isinstance(connection_id, int)
            connection_ready.set_result(connection_id)
        return await execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", observe_owner)
    try:
        async with engine.connect() as blocker:
            async with blocker.begin():
                await blocker.execute(
                    text(
                        "SELECT namespace_hash FROM tinkerfin_trace_namespaces "
                        "WHERE namespace = :namespace FOR UPDATE"
                    ),
                    {"namespace": json.dumps(namespace, ensure_ascii=False)},
                )
                open_writer = asyncio.create_task(store.open_writer(identity))
                await _wait_for_mysql_lock(await connection_ready)
                released_at = await blocker.scalar(text("SELECT UTC_TIMESTAMP(6)"))
                assert isinstance(released_at, datetime)
                assert not open_writer.done()
        writer = await open_writer
        async with engine.connect() as connection:
            expiry = await connection.scalar(
                text(
                    "SELECT lease_expires_at FROM tinkerfin_trace_writers "
                    "WHERE namespace_hash = UNHEX(SHA2(:namespace, 256)) AND run_id = :run_id"
                ),
                {
                    "namespace": namespace,
                    "run_id": json.dumps(identity.run_id, ensure_ascii=False),
                },
            )
        assert isinstance(expiry, datetime)
        assert (
            expiry - timedelta(seconds=store.options.writer_lease_seconds)
            >= released_at
        )
    finally:
        if open_writer is not None and not open_writer.done():
            open_writer.cancel()
            await asyncio.gather(open_writer, return_exceptions=True)
        if writer is not None:
            await writer.aclose()
        await engine.dispose()
