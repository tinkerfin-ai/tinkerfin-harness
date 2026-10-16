"""Schema ownership and fixed UTC lease observations on real SQL databases."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Executable

from tinkerfin_contracts import RunIdentity
from tinkerfin_sqlalchemy import SqlTransaction, engine_dialect
from tinkerfin_tracing import (
    RunFact,
    SqlAlchemyTraceStore,
    TraceStoreOptions,
    TraceStoreProtocolError,
)
from tinkerfin_tracing.backend import TraceLedgerStateRequest
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES, get_trace_store_schema


async def test_offline_ddl_is_accepted_by_automatic_setup(
    trace_sql_engine: AsyncEngine,
) -> None:
    schema = get_trace_store_schema(dialect=engine_dialect(trace_sql_engine))
    async with SqlTransaction(
        trace_sql_engine, schema_lock="trace-schema"
    ) as connection:
        for statement in schema.ddl.strip().removesuffix(";").split(";\n\n"):
            await connection.exec_driver_sql(statement)
    await SqlAlchemyTraceStore(trace_sql_engine).setup()
    async with trace_sql_engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    assert set(tables) == set(TRACE_TABLE_NAMES)


async def test_partial_schema_is_rejected_without_repair(
    trace_sql_engine: AsyncEngine,
) -> None:
    await SqlAlchemyTraceStore(trace_sql_engine).setup()
    async with trace_sql_engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_events")
    with pytest.raises(TraceStoreProtocolError):
        await SqlAlchemyTraceStore(trace_sql_engine).setup()
    async with trace_sql_engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    assert "tinkerfin_trace_events" not in tables


async def test_setup_cancellation_waits_for_complete_schema(
    trace_sql_engine: AsyncEngine,
) -> None:
    first_table, release = asyncio.Event(), asyncio.Event()
    observed = False

    def pause_after_first_table(
        connection: Connection, _cursor: Any, statement: str, *_args: Any
    ) -> None:
        nonlocal observed
        if observed or not statement.strip().startswith(
            "CREATE TABLE tinkerfin_trace_"
        ):
            return
        observed = True
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)

        async def hold(_driver: Any) -> None:
            first_table.set()
            await release.wait()

        adapted.run_async(hold)

    event.listen(
        trace_sql_engine.sync_engine, "after_cursor_execute", pause_after_first_table
    )
    store = SqlAlchemyTraceStore(trace_sql_engine)
    first = asyncio.create_task(store.setup())
    second: asyncio.Task[None] | None = None
    try:
        await first_table.wait()
        second = asyncio.create_task(store.setup())
        first.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await SqlAlchemyTraceStore(trace_sql_engine).setup()
        async with trace_sql_engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
        assert set(tables) == set(TRACE_TABLE_NAMES)
    finally:
        release.set()
        await asyncio.gather(
            first, *(() if second is None else (second,)), return_exceptions=True
        )
        event.remove(
            trace_sql_engine.sync_engine,
            "after_cursor_execute",
            pause_after_first_table,
        )


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "AUTOCOMMIT"])
async def test_read_page_keeps_one_snapshot_across_a_committed_append(
    trace_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, isolation: str
) -> None:
    reader = (
        trace_sql_engine
        if trace_sql_engine.dialect.name == "sqlite"
        else trace_sql_engine.execution_options(isolation_level=isolation)
    )
    store = SqlAlchemyTraceStore(reader)
    identity = RunIdentity(namespace="default", thread_id="snapshot", run_id="run")
    writer = await SqlAlchemyTraceStore(trace_sql_engine).open_writer(identity)
    fact = RunFact(
        identity=identity,
        source_observation_id="start",
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    )
    await writer.append((fact,))
    original = AsyncConnection.execute
    head, release = asyncio.Event(), asyncio.Event()

    async def pause_head(
        connection: AsyncConnection, statement: Executable, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original(connection, statement, *args, **kwargs)
        if not head.is_set() and str(statement).startswith(
            "SELECT tinkerfin_trace_threads."
        ):
            head.set()
            await release.wait()
        return result

    read: asyncio.Task[Any] | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", pause_head)
            read = asyncio.create_task(
                store.read_events(writer.key, after_seq=0, as_of_seq=2, limit=10)
            )
            await head.wait()
            await writer.append(
                (
                    fact.model_copy(
                        update={
                            "source_observation_id": "terminal",
                            "phase": "terminal",
                            "input_kind": None,
                            "outcome": "succeeded",
                        }
                    ),
                ),
                mandatory=True,
            )
            release.set()
            page = await read
        assert [item.trace_seq for item in page] == [1]
        assert (await store.snapshot(identity.thread)).as_of_seq == 2
    finally:
        release.set()
        if read is not None:
            await asyncio.gather(read, return_exceptions=True)
        await writer.aclose()


async def test_database_clock_keeps_utc_and_lease_duration(
    trace_sql_engine: AsyncEngine,
) -> None:
    options = TraceStoreOptions(
        writer_lease_seconds=120, writer_heartbeat_interval_seconds=60
    )
    store = SqlAlchemyTraceStore(trace_sql_engine, options=options)
    identity = RunIdentity(namespace="default", thread_id="utc", run_id="writer")
    before = None
    if trace_sql_engine.dialect.name == "postgresql":
        async with trace_sql_engine.connect() as connection:
            before = await connection.scalar(text("SELECT clock_timestamp()"))
    writer = await store.open_writer(identity)
    try:
        state = await store.backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace="default",
                thread_id=identity.thread_id,
                run_id=identity.run_id,
            )
        )
        assert state.target_writer is not None
        assert state.observed_at.tzinfo is UTC
        if trace_sql_engine.dialect.name == "postgresql":
            async with trace_sql_engine.connect() as connection:
                assert await connection.scalar(text("SHOW timezone")) == "Asia/Shanghai"
                recorded = await connection.execute(
                    text(
                        "SELECT updated_at, lease_expires_at FROM tinkerfin_trace_writers"
                    )
                )
                row = recorded.one()
                assert (row.lease_expires_at - row.updated_at).total_seconds() == 120
                db_now = await connection.scalar(text("SELECT clock_timestamp()"))
                assert isinstance(db_now, datetime)
                # A non-UTC session still produces a real UTC public observation.
                assert state.observed_at <= db_now.astimezone(UTC)
                assert isinstance(before, datetime)
                assert before.astimezone(UTC) <= state.observed_at
    finally:
        await writer.aclose()


async def test_schema_comments_are_validated(trace_sql_engine: AsyncEngine) -> None:
    if trace_sql_engine.dialect.name == "sqlite":
        pytest.skip("SQLite does not store table comments")
    await SqlAlchemyTraceStore(trace_sql_engine).setup()
    command = (
        "ALTER TABLE tinkerfin_trace_writers COMMENT = 'incorrect'"
        if trace_sql_engine.dialect.name == "mysql"
        else "COMMENT ON TABLE tinkerfin_trace_writers IS 'incorrect'"
    )
    async with trace_sql_engine.begin() as connection:
        await connection.exec_driver_sql(command)
    with pytest.raises(TraceStoreProtocolError, match="comment"):
        await SqlAlchemyTraceStore(trace_sql_engine).setup()


async def test_postgresql_lease_samples_after_transaction_wait(
    trace_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    if trace_sql_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL distinguishes statement and transaction clocks")
    store = SqlAlchemyTraceStore(trace_sql_engine)
    await store.setup()
    waiting, release = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute

    async def wait_before_lock(
        connection: AsyncConnection, statement: Executable, *args: Any, **kwargs: Any
    ) -> Any:
        if not waiting.is_set() and "FOR UPDATE" in str(statement):
            # Start the real driver transaction, then delay its first ownership read.
            # A transaction-start timestamp must not be reused for the new lease.
            await connection.scalar(text("SELECT transaction_timestamp()"))
            waiting.set()
            await release.wait()
        return await original(connection, statement, *args, **kwargs)

    identity = RunIdentity(namespace="default", thread_id="wait", run_id="writer")
    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", wait_before_lock)
        pending = asyncio.create_task(store.open_writer(identity))
        try:
            await waiting.wait()
            async with trace_sql_engine.connect() as connection:
                released_at = await connection.scalar(text("SELECT clock_timestamp()"))
            assert isinstance(released_at, datetime)
        finally:
            release.set()
            writer = await pending
    try:
        state = await store.backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace="default",
                thread_id=identity.thread_id,
                run_id=identity.run_id,
            )
        )
        assert state.target_writer is not None
        from datetime import timedelta

        allocated_at = state.target_writer.lease_expires_at - timedelta(
            seconds=store.options.writer_lease_seconds
        )
        assert allocated_at >= released_at.astimezone(UTC)
    finally:
        await writer.aclose()
