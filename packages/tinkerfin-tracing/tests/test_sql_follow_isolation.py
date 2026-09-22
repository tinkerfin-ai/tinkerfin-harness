"""Shared follow pages retain SQL instance, cursor, and cancellation isolation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import CapturedValue, RunFact, StateRevisionFact
from tinkerfin_tracing.backend import StoredTraceEventPage, TraceEventPageRequest
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore

pytestmark = [
    pytest.mark.docker_integration,
    pytest.mark.parametrize("trace_sql_engine", ["mysql", "postgresql"], indirect=True),
]

_NOW = datetime(2026, 9, 21, tzinfo=UTC)
_IDENTITY = RunIdentity(namespace="sql-follow", thread_id="thread", run_id="run")


@pytest.fixture
def read_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]
    monkeypatch.setattr("tinkerfin_tracing._follow_reads._monotonic", lambda: clock[0])
    return clock


@pytest.fixture
async def reader_engines(
    trace_sql_engine: AsyncEngine,
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    if trace_sql_engine.dialect.name == "postgresql":
        async with trace_sql_engine.connect() as connection:
            schema = await connection.scalar(text("SELECT current_schema()"))
        assert isinstance(schema, str)
        first = create_async_engine(
            trace_sql_engine.url,
            pool_size=2,
            max_overflow=0,
            connect_args={"server_settings": {"search_path": schema}},
        )
        second = create_async_engine(
            trace_sql_engine.url,
            pool_size=2,
            max_overflow=0,
            connect_args={"server_settings": {"search_path": schema}},
        )
    else:
        first = create_async_engine(trace_sql_engine.url, pool_size=2, max_overflow=0)
        second = create_async_engine(trace_sql_engine.url, pool_size=2, max_overflow=0)
    try:
        yield first, second
    finally:
        await first.dispose()
        await second.dispose()


def _state(sequence: int) -> StateRevisionFact:
    return StateRevisionFact(
        identity=_IDENTITY,
        source_observation_id=f"state-{sequence}",
        occurred_at=_NOW,
        monotonic_ns=sequence,
        revision_id=f"revision-{sequence}",
        changes=CapturedValue(
            disposition="inline", value={"value": sequence}, safe_size_bytes=11
        ),
    )


def _assert_pool_returned(engine: AsyncEngine) -> None:
    pool = engine.sync_engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    assert pool.checkedout() == 0


async def test_sql_shared_pages_keep_remote_commits_and_cursors_independent(
    trace_sql_engine: AsyncEngine,
    reader_engines: tuple[AsyncEngine, AsyncEngine],
    read_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = SqlAlchemyTraceStore(trace_sql_engine)
    reader = SqlAlchemyTraceStore(reader_engines[0])
    independent = SqlAlchemyTraceStore(reader_engines[1])
    writer = await producer.open_writer(_IDENTITY)
    await writer.append(
        (
            RunFact(
                identity=_IDENTITY,
                source_observation_id="start",
                occurred_at=_NOW,
                monotonic_ns=1,
                phase="started",
                input_kind="ordinary",
            ),
            _state(2),
            _state(3),
        )
    )
    reads: list[TraceEventPageRequest] = []
    original = reader.backend.read_event_page

    async def read(request: TraceEventPageRequest) -> StoredTraceEventPage:
        reads.append(request)
        return await original(request)

    monkeypatch.setattr(reader.backend, "read_event_page", read)
    first = reader.follow(writer.key, after_seq=0)
    suffix = reader.follow(writer.key, after_seq=1)
    peer = independent.follow(writer.key, after_seq=2)
    try:
        first_page = await anext(first)
        suffix_page = await anext(suffix)
        peer_page = await anext(peer)
        assert [item.trace_seq for item in first_page.events] == [1, 2, 3]
        assert [item.trace_seq for item in suffix_page.events] == [2, 3]
        assert [item.trace_seq for item in peer_page.events] == [3]
        assert len(reads) == 1
        assert first_page.events[1].fact is not suffix_page.events[0].fact

        await writer.append((_state(4),))
        read_clock[0] += 1.0
        for follower in (first, suffix, peer):
            page = await anext(follower)
            assert [item.trace_seq for item in page.events] == [4]
        assert len(reads) == 2

        await first.aclose()
        await writer.append((_state(5),))
        read_clock[0] += 1.0
        for follower in (suffix, peer):
            page = await anext(follower)
            assert [item.trace_seq for item in page.events] == [5]

        await writer.aclose()
        read_clock[0] += 1.0
        for follower in (suffix, peer):
            ownership = await anext(follower)
            assert ownership.as_of_seq == 5
            assert ownership.events == ownership.active_run_ids == ()
    finally:
        await first.aclose()
        await suffix.aclose()
        await peer.aclose()
        await writer.aclose()
    for engine in (trace_sql_engine, *reader_engines):
        _assert_pool_returned(engine)


async def test_sql_cancelled_follow_releases_connection_without_affecting_peer(
    trace_sql_engine: AsyncEngine,
    reader_engines: tuple[AsyncEngine, AsyncEngine],
    read_clock: list[float],
) -> None:
    producer = SqlAlchemyTraceStore(trace_sql_engine)
    reader = SqlAlchemyTraceStore(reader_engines[0])
    independent = SqlAlchemyTraceStore(reader_engines[1])
    writer = await producer.open_writer(_IDENTITY)
    await writer.append((_state(1),))
    await reader.setup()
    await independent.setup()
    entered, release = asyncio.Event(), asyncio.Event()

    def hold_read(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if entered.is_set() or not statement.startswith(
            "SELECT tinkerfin_trace_threads."
        ):
            return
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)

        async def hold(_driver: object) -> None:
            entered.set()
            await release.wait()

        adapted.run_async(hold)

    event.listen(reader_engines[0].sync_engine, "before_cursor_execute", hold_read)
    first = reader.follow(writer.key, after_seq=0)
    peer = independent.follow(writer.key, after_seq=0)
    pending = asyncio.create_task(anext(first))
    try:
        await entered.wait()
        assert [item.trace_seq for item in (await anext(peer)).events] == [1]
        pending.cancel("subscriber left during SQL read")
        with pytest.raises(asyncio.CancelledError, match="subscriber left"):
            await pending
        _assert_pool_returned(reader_engines[0])

        await writer.append((_state(2),))
        read_clock[0] += 1.0
        assert [item.trace_seq for item in (await anext(peer)).events] == [2]
        restarted = reader.follow(writer.key, after_seq=0)
        try:
            assert [item.trace_seq for item in (await anext(restarted)).events] == [
                1,
                2,
            ]
        finally:
            await restarted.aclose()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await first.aclose()
        await peer.aclose()
        event.remove(reader_engines[0].sync_engine, "before_cursor_execute", hold_read)
        await writer.aclose()
    for engine in (trace_sql_engine, *reader_engines):
        _assert_pool_returned(engine)
