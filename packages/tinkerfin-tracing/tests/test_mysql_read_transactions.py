"""MySQL read snapshots, borrowed connection settings, and full command budgets."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    RunFact,
    SqlAlchemyTraceStore,
    ToolFact,
    Tracer,
    TraceStoreError,
    TraceStoreOptions,
)
from tinkerfin_tracing.backend import TraceEventPageRequest

pytestmark = pytest.mark.docker_integration


@pytest.fixture(
    params=(
        "default",
        "READ COMMITTED",
        "READ UNCOMMITTED",
        "SERIALIZABLE",
        "AUTOCOMMIT",
        "AUTOCOMMIT without rollback",
        "execution:READ COMMITTED",
        "execution:AUTOCOMMIT",
    )
)
async def reader_engine(
    trace_mysql_url: str, request: pytest.FixtureRequest
) -> AsyncIterator[AsyncEngine]:
    mode = request.param
    assert isinstance(mode, str)
    isolation = (
        None
        if mode == "default" or mode.startswith("execution:")
        else mode.removesuffix(" without rollback")
    )
    base = create_async_engine(
        trace_mysql_url,
        pool_size=1,
        max_overflow=0,
        isolation_level=isolation,
        skip_autocommit_rollback=mode.endswith("without rollback"),
    )
    reader = (
        base.execution_options(isolation_level=mode.removeprefix("execution:"))
        if mode.startswith("execution:")
        else base
    )
    try:
        yield reader
    finally:
        await base.dispose()


def _fact(identity: RunIdentity, phase: Literal["started", "terminal"]) -> RunFact:
    return RunFact(
        identity=identity,
        source_observation_id=f"{identity.run_id}-{phase}",
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase == "terminal" else None,
    )


async def _server_commands(observer: AsyncConnection, thread_id: int) -> Counter[str]:
    rows = await observer.execute(
        text(
            "SELECT EVENT_NAME, COUNT_STAR FROM performance_schema."
            "events_statements_summary_by_thread_by_event_name "
            "WHERE THREAD_ID = :thread_id AND COUNT_STAR > 0"
        ),
        {"thread_id": thread_id},
    )
    return Counter({str(row[0]): int(row[1]) for row in rows})


async def _session_settings(reader: AsyncEngine) -> tuple[str, int, int]:
    async with reader.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT @@transaction_isolation, @@autocommit, "
                    "@@transaction_read_only"
                )
            )
        ).one()
        return str(row[0]), int(row[1]), int(row[2])


def _assert_pool_returned(reader: AsyncEngine) -> None:
    pool = reader.sync_engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    assert pool.checkedout() == 0


async def test_mysql_read_snapshot_and_host_settings_survive_concurrent_commit(
    trace_mysql_url: str, reader_engine: AsyncEngine
) -> None:
    producer_engine = create_async_engine(trace_mysql_url)
    producer = SqlAlchemyTraceStore(producer_engine)
    reader = SqlAlchemyTraceStore(reader_engine)
    identity = RunIdentity(namespace="test", thread_id="snapshot", run_id="first")
    writer = await producer.open_writer(identity)
    changed = False

    async def commit_after_metadata() -> None:
        await writer.append((_fact(identity, "terminal"),), mandatory=True)
        await writer.aclose()

    def change_between_reads(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        nonlocal changed
        if changed or "FROM tinkerfin_trace_threads" not in statement:
            return
        changed = True
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)
        adapted.run_async(lambda _driver: commit_after_metadata())

    try:
        await writer.append((_fact(identity, "started"),))
        await reader.setup()
        # Measure the host's configured sessions independently of schema writes.
        await reader_engine.dispose()
        settings = await _session_settings(reader_engine)
        event.listen(
            reader_engine.sync_engine, "after_cursor_execute", change_between_reads
        )
        try:
            snapshot = await reader.snapshot(identity.thread)
        finally:
            event.remove(
                reader_engine.sync_engine, "after_cursor_execute", change_between_reads
            )
        assert changed
        assert snapshot.as_of_seq == 1
        assert [
            (item.run_id, item.committed_events) for item in snapshot.active_writers
        ] == [(identity.run_id, 1)]
        _assert_pool_returned(reader_engine)
        assert await _session_settings(reader_engine) == settings
        async with reader_engine.begin() as connection:
            # A subsequent host write must not inherit READ ONLY or an open snapshot.
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_threads SET next_seq = next_seq "
                    "WHERE namespace = :namespace"
                ),
                {"namespace": json.dumps(identity.namespace, ensure_ascii=False)},
            )
        current = await reader.snapshot(identity.thread)
        assert current.as_of_seq == 2
        assert current.active_writers == ()
    finally:
        await writer.aclose()
        await producer_engine.dispose()


@pytest.mark.parametrize("failure_phase", ("start", "after-start", "read"))
async def test_mysql_failed_reads_return_clean_host_connection(
    trace_mysql_url: str, reader_engine: AsyncEngine, failure_phase: str
) -> None:
    store = SqlAlchemyTraceStore(reader_engine)
    producer_engine = create_async_engine(trace_mysql_url)
    producer = SqlAlchemyTraceStore(producer_engine)
    identity = RunIdentity(namespace="test", thread_id="failure", run_id="first")
    writer = await producer.open_writer(identity)
    failure = SQLAlchemyError("test-owned database command failure")
    failed = False
    event_name = (
        "after_cursor_execute"
        if failure_phase == "after-start"
        else "before_cursor_execute"
    )

    def fail_command(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        nonlocal failed
        target = (
            statement.startswith("START TRANSACTION")
            if failure_phase in {"start", "after-start"}
            else "FROM tinkerfin_trace_threads" in statement
        )
        if target and not failed:
            failed = True
            raise failure

    try:
        await writer.append((_fact(identity, "started"),))
        await store.setup()
        await reader_engine.dispose()
        settings = await _session_settings(reader_engine)
        async with reader_engine.connect() as connection:
            original_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
        event.listen(reader_engine.sync_engine, event_name, fail_command)
        try:
            with pytest.raises(TraceStoreError) as captured:
                await store.snapshot(identity.thread)
            assert captured.value.cause is failure
        finally:
            event.remove(reader_engine.sync_engine, event_name, fail_command)
        assert failed
        _assert_pool_returned(reader_engine)
        assert await _session_settings(reader_engine) == settings
        if failure_phase in {"start", "after-start"}:
            async with reader_engine.connect() as connection:
                replacement_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            assert replacement_id != original_id
        async with reader_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_threads SET next_seq = next_seq "
                    "WHERE namespace = :namespace"
                ),
                {"namespace": json.dumps(identity.namespace, ensure_ascii=False)},
            )
        assert (await store.snapshot(identity.thread)).as_of_seq == 1
    finally:
        await writer.aclose()
        await producer_engine.dispose()


@pytest.mark.parametrize("after_start", (False, True))
async def test_mysql_interrupted_read_start_discards_pending_settings(
    reader_engine: AsyncEngine, after_start: bool
) -> None:
    store = SqlAlchemyTraceStore(reader_engine)
    await store.setup()
    await reader_engine.dispose()
    settings = await _session_settings(reader_engine)
    async with reader_engine.connect() as connection:
        original_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
    entered = asyncio.Event()
    release = asyncio.Event()
    event_name = "after_cursor_execute" if after_start else "before_cursor_execute"

    def delay_start(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if not statement.startswith("START TRANSACTION"):
            return
        entered.set()
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)
        adapted.run_async(lambda _driver: release.wait())

    event.listen(reader_engine.sync_engine, event_name, delay_start)
    pending = asyncio.create_task(
        store.snapshot(ThreadIdentity(namespace="test", thread_id="absent"))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for attempt in range(8):
            if pending.done():
                break
            pending.cancel(f"read setup cancellation {attempt + 1}")
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError) as captured:
            await pending
        assert captured.value.args == ("read setup cancellation 1",)
        _assert_pool_returned(reader_engine)
        assert await _session_settings(reader_engine) == settings
        async with reader_engine.connect() as connection:
            replacement_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
        assert replacement_id != original_id
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        event.remove(reader_engine.sync_engine, event_name, delay_start)


@pytest.mark.parametrize("rollback_fails", (False, True))
async def test_mysql_autocommit_read_cleanup_settles_before_pool_return(
    trace_mysql_url: str, rollback_fails: bool
) -> None:
    reader_engine = create_async_engine(
        trace_mysql_url,
        pool_size=1,
        max_overflow=0,
        isolation_level="AUTOCOMMIT",
        skip_autocommit_rollback=True,
    )
    producer_engine = create_async_engine(trace_mysql_url)
    producer = SqlAlchemyTraceStore(producer_engine)
    store = SqlAlchemyTraceStore(reader_engine)
    identity = RunIdentity(namespace="test", thread_id="cleanup", run_id="first")
    writer = await producer.open_writer(identity)
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = SQLAlchemyError("test-owned rollback failure")

    def delay_or_fail_rollback(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if statement != "ROLLBACK":
            return
        entered.set()
        if rollback_fails:
            raise failure
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)
        adapted.run_async(lambda _driver: release.wait())

    try:
        await writer.append((_fact(identity, "started"),))
        await store.setup()
        await reader_engine.dispose()
        settings = await _session_settings(reader_engine)
        event.listen(
            reader_engine.sync_engine, "before_cursor_execute", delay_or_fail_rollback
        )
        pending = asyncio.create_task(store.snapshot(identity.thread))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            if rollback_fails:
                with pytest.raises(TraceStoreError) as captured:
                    await pending
                assert captured.value.cause is failure
            else:
                for attempt in range(8):
                    pending.cancel(f"read cleanup cancellation {attempt + 1}")
                    await asyncio.sleep(0)
                assert not pending.done()
                release.set()
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await pending
                assert cancelled.value.args == ("read cleanup cancellation 1",)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            event.remove(
                reader_engine.sync_engine,
                "before_cursor_execute",
                delay_or_fail_rollback,
            )
        _assert_pool_returned(reader_engine)
        assert await _session_settings(reader_engine) == settings
        async with reader_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_threads SET next_seq = next_seq "
                    "WHERE namespace = :namespace"
                ),
                {"namespace": json.dumps(identity.namespace, ensure_ascii=False)},
            )
        assert (await store.snapshot(identity.thread)).as_of_seq == 1
    finally:
        await writer.aclose()
        await reader_engine.dispose()
        await producer_engine.dispose()


async def test_mysql_graph_follow_observes_next_remote_run_and_releases_pool(
    trace_mysql_url: str,
) -> None:
    reader_engine = create_async_engine(trace_mysql_url, pool_size=1, max_overflow=0)
    producer_engine = create_async_engine(trace_mysql_url)
    options = TraceStoreOptions(follow_poll_seconds=0.02)
    reader = SqlAlchemyTraceStore(reader_engine, options=options)
    producer = SqlAlchemyTraceStore(producer_engine)
    first = RunIdentity(namespace="test", thread_id="graph", run_id="first")
    writer = await producer.open_writer(first)
    try:
        await writer.append((_fact(first, "started"),))
        await writer.append((_fact(first, "terminal"),), mandatory=True)
        await writer.aclose()
        graph = await Tracer(store=reader).query(first.thread)
        returned = asyncio.Event()

        def observe_pool_return(_connection: object, _entry: object) -> None:
            returned.set()

        event.listen(reader_engine.sync_engine, "checkin", observe_pool_return)
        follower = graph.follow()
        pending = asyncio.create_task(anext(follower))
        try:
            for _ in range(3):
                returned.clear()
                await asyncio.wait_for(returned.wait(), timeout=2)
                _assert_pool_returned(reader_engine)
            assert not pending.done()
            second = RunIdentity(
                namespace="test", thread_id=first.thread_id, run_id="second"
            )
            writer = await producer.open_writer(second)
            await writer.append(
                (
                    _fact(second, "started"),
                    ToolFact(
                        identity=second,
                        source_observation_id="next-run-tool",
                        occurred_at=datetime.now(UTC),
                        monotonic_ns=2,
                        phase="started",
                        tool_call_id="next-run-tool",
                        source_tool_call_id="next-run-tool",
                        tool_name="remote-tool",
                    ),
                )
            )
            delta = await asyncio.wait_for(pending, timeout=2)
            assert delta.as_of_seq == 4
            assert {node.run_id for node in delta.node_upserts} == {second.run_id}
            await follower.aclose()
            _assert_pool_returned(reader_engine)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await follower.aclose()
            event.remove(reader_engine.sync_engine, "checkin", observe_pool_return)
    finally:
        await writer.aclose()
        await reader_engine.dispose()
        await producer_engine.dispose()


async def test_mysql_idle_page_bounds_all_server_commands(trace_mysql_url: str) -> None:
    reader = create_async_engine(
        trace_mysql_url, pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    observer_engine = create_async_engine(trace_mysql_url)
    store = SqlAlchemyTraceStore(reader)
    identity = RunIdentity(namespace="test", thread_id="idle", run_id="first")
    writer = await store.open_writer(identity)
    try:
        await writer.append((_fact(identity, "started"),))
        await writer.append((_fact(identity, "terminal"),), mandatory=True)
        await writer.aclose()
        snapshot = await store.snapshot(identity.thread)
        async with reader.connect() as connection:
            connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            assert isinstance(connection_id, int)
        async with observer_engine.connect() as observer:
            thread_id = await observer.scalar(
                text(
                    "SELECT THREAD_ID FROM performance_schema.threads "
                    "WHERE PROCESSLIST_ID = :connection_id"
                ),
                {"connection_id": connection_id},
            )
            assert isinstance(thread_id, int)
            before = await _server_commands(observer, thread_id)
            for _ in range(4):
                page = await store.backend.read_event_page(
                    TraceEventPageRequest(
                        key=snapshot.key,
                        direction="forward",
                        after_seq=snapshot.as_of_seq,
                        limit=10,
                    )
                )
                assert page.events == page.active_run_ids == ()
                assert page.tail_seq == snapshot.as_of_seq
            commands = (await _server_commands(observer, thread_id)) - before
            assert commands == Counter(
                {
                    "statement/com/Ping": 4,
                    "statement/sql/set_option": 4,
                    "statement/sql/begin": 4,
                    "statement/sql/select": 4,
                    # Each read acknowledges rollback and then returns its checkout
                    # through the host pool's normal reset. Count both commands.
                    "statement/sql/rollback": 8,
                }
            ), dict(commands)
    finally:
        await writer.aclose()
        await reader.dispose()
        await observer_engine.dispose()
