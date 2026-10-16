"""Store atomicity, snapshots, cancellation, and close behavior on real databases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.store.base import GetOp, Item, Op, PutOp, Result, SearchItem, SearchOp
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_langgraph_store import (
    SqlAlchemyStore,
    StoreClosedError,
    StoreCorruptionError,
    StoreDriverError,
    StoreSchemaError,
)

pytestmark = pytest.mark.usefixtures("engine")


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_batch_never_commits_after_failure_or_cancellation(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    await store.setup()
    original = AsyncConnection.execute
    entered, release = asyncio.Event(), asyncio.Event()
    inserts = 0

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal inserts
        if str(statement).startswith("INSERT INTO tinkerfin_store_documents"):
            inserts += 1
            if inserts == 2:
                entered.set()
                await release.wait()
                raise OperationalError(
                    "injected write", None, RuntimeError("private driver diagnostic")
                )
        return await original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    task = asyncio.create_task(
        store.abatch([PutOp(("batch",), "first", {}), PutOp(("batch",), "second", {})])
    )
    try:
        await entered.wait()
        if cancel:
            task.cancel()
        release.set()
        with pytest.raises(
            asyncio.CancelledError if cancel else StoreDriverError
        ) as caught:
            await task
        if not cancel:
            assert "private driver diagnostic" not in str(caught.value)
        monkeypatch.setattr(AsyncConnection, "execute", original)
        assert await store.asearch(()) == []
        assert await store.alist_namespaces() == []
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("with_write", [False, True])
async def test_batch_snapshot_survives_a_concurrent_peer_write(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
    with_write: bool,
) -> None:
    peer = SqlAlchemyStore(engine)
    await store.abatch(
        [
            PutOp(("snapshot",), "first", {"n": 0}),
            PutOp(("snapshot",), "second", {"n": 0}),
        ]
    )
    await peer.setup()
    first_read, release = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute
    reader: asyncio.Task[list[Result]] | None = None
    peer_started = asyncio.Event()
    writer: asyncio.Task[None] | None = None

    async def peer_write() -> None:
        peer_started.set()
        await peer.aput(("snapshot",), "second", {"n": 1})

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original(connection, statement, *args, **kwargs)
        if (
            asyncio.current_task() is reader
            and str(statement).startswith("SELECT tinkerfin_store_documents")
            and not first_read.is_set()
        ):
            first_read.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    operations: list[Op] = [
        GetOp(("snapshot",), "first"),
        GetOp(("snapshot",), "second"),
        SearchOp(("snapshot",)),
    ]
    if with_write:
        operations.insert(0, PutOp(("another",), "write", {}))
    reader = asyncio.create_task(store.abatch(operations))
    try:
        await first_read.wait()
        writer = asyncio.create_task(peer_write())
        await peer_started.wait()
        if with_write and engine.dialect.name == "sqlite":
            # SQLite's write snapshot excludes every other writer, even one
            # changing a different namespace. Releasing it permits the peer commit.
            assert not writer.done()
        else:
            await writer
        release.set()
        result = await reader
        await writer
        items = [item for item in result if isinstance(item, Item)]
        assert len(items) == 2 and all(item.value == {"n": 0} for item in items)
        search_result = result[-1]
        assert isinstance(search_result, list) and all(
            isinstance(item, SearchItem) and item.value == {"n": 0}
            for item in search_result
        )
        latest = await store.aget(("snapshot",), "second")
        assert latest is not None and latest.value == {"n": 1}
    finally:
        release.set()
        reader.cancel()
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await asyncio.gather(reader, return_exceptions=True)
        await peer.aclose()


async def test_exact_get_does_not_hide_a_document_without_its_namespace(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
) -> None:
    await store.aput(("damaged",), "key", {"number": 1})
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM tinkerfin_store_namespaces"))
    with pytest.raises(StoreCorruptionError):
        await store.aget(("damaged",), "key")


async def test_overwrite_does_not_erase_invalid_existing_document_evidence(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
) -> None:
    await store.aput(("damaged",), "key", {"number": 1})
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE tinkerfin_store_documents SET value = :value"), {"value": "[]"}
        )
    with pytest.raises(StoreCorruptionError):
        await store.aput(("damaged",), "key", {"replaced": True})
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT value FROM tinkerfin_store_documents"))
            == "[]"
        )


async def test_close_waiter_cancellation_does_not_cancel_accepted_work(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await store.aput(("close",), "key", {"accepted": True})
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(statement).startswith("SELECT tinkerfin_store_documents"):
            entered.set()
            await release.wait()
        return await original(connection, statement, *args, **kwargs)

    async def close() -> None:
        closing.set()
        await store.aclose()

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    reader = asyncio.create_task(store.aget(("close",), "key"))
    closer: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        closer = asyncio.create_task(close())
        await closing.wait()
        with pytest.raises(StoreClosedError):
            await store.aget(("close",), "key")
        closer.cancel("cancel shutdown waiter")
        closer.cancel("repeat shutdown cancellation")
        assert not reader.done()
        release.set()
        item = await reader
        assert item is not None and item.value == {"accepted": True}
        with pytest.raises(asyncio.CancelledError):
            await closer
        await store.aclose()
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        release.set()
        tasks = [reader] if closer is None else [reader, closer]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_unconfirmed_commit_is_reported_without_replaying_the_write(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await store.setup()
    committed = 0
    original_commit, original_execute = (
        AsyncConnection.commit,
        AsyncConnection.exec_driver_sql,
    )
    failure = OperationalError("COMMIT", None, OSError("acknowledgement lost"))

    async def commit(connection: AsyncConnection) -> None:
        nonlocal committed
        await original_commit(connection)
        committed += 1
        raise failure

    async def execute(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal committed
        result = await original_execute(connection, statement, *args, **kwargs)
        if statement == "COMMIT":
            committed += 1
            raise failure
        return result

    if engine.dialect.name == "sqlite":
        monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)
    else:
        monkeypatch.setattr(AsyncConnection, "commit", commit)
    with pytest.raises(StoreDriverError) as caught:
        await store.aput(("commit",), "key", {"once": True})
    assert caught.value.cause is failure and committed == 1
    monkeypatch.setattr(AsyncConnection, "commit", original_commit)
    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", original_execute)
    item = await store.aget(("commit",), "key")
    assert item is not None and item.value == {"once": True}


async def test_close_cancellation_preserves_process_control_and_its_original_cause(
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProcessStop(BaseException):
        pass

    primary = ProcessStop("stop application")
    original_cause = RuntimeError("original shutdown reason")
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = AsyncConnection.execute
    reader: asyncio.Task[Item | None] | None = None

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(statement).startswith("SELECT tinkerfin_store_documents"):
            entered.set()
            await release.wait()
        return await original(connection, statement, *args, **kwargs)

    async def body() -> None:
        nonlocal reader
        async with store:
            reader = asyncio.create_task(store.aget(("control",), "key"))
            await entered.wait()
            stopping.set()
            raise primary from original_cause

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    owner = asyncio.create_task(body())
    try:
        await stopping.wait()
        owner.cancel("cancel close waiter")
        release.set()
        with pytest.raises(ProcessStop) as caught:
            await owner
        assert caught.value is primary and primary.__cause__ is original_cause
        assert isinstance(primary.__context__, asyncio.CancelledError)
        assert reader is not None and await reader is None
    finally:
        release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def test_incomplete_schema_is_rejected_without_recreating_lost_tables(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
) -> None:
    await store.setup()
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE tinkerfin_store_fields"))
    peer = SqlAlchemyStore(engine)
    try:
        with pytest.raises(StoreSchemaError):
            await peer.aput(("schema",), "key", {})
        async with engine.connect() as connection:
            exists = await connection.run_sync(
                lambda sync: sync.dialect.has_table(sync, "tinkerfin_store_fields")
            )
            assert not exists
    finally:
        await peer.aclose()


@pytest.mark.parametrize("failure_kind", [None, "driver", "control"])
async def test_accepted_setup_settles_before_caller_cancellation_and_close(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    failure_kind: str | None,
) -> None:
    class ProcessStop(BaseException):
        pass

    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    driver_error = OperationalError("DDL", None, RuntimeError("schema driver failure"))
    control = ProcessStop("schema process control")
    control_cause = ValueError("original process reason")
    control.__cause__ = control_cause

    def after_statement(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if not statement.lstrip().startswith("CREATE TABLE") or entered.is_set():
            return
        entered.set()
        driver = connection.connection.dbapi_connection
        assert isinstance(driver, AdaptedConnection)
        driver.run_async(lambda _raw: release.wait())
        if failure_kind == "driver":
            raise driver_error
        if failure_kind == "control":
            raise control

    async def close() -> None:
        closing.set()
        await store.aclose()

    event.listen(engine.sync_engine, "after_cursor_execute", after_statement)
    setup = asyncio.create_task(store.setup())
    closer: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        setup.cancel("cancel startup waiter")
        setup.cancel("repeat startup cancellation")
        closer = asyncio.create_task(close())
        await closing.wait()
        assert not setup.done() and not closer.done()
        release.set()
        if failure_kind == "control":
            with pytest.raises(ProcessStop) as caught:
                await setup
            assert caught.value is control and control.__cause__ is control_cause
            assert isinstance(control.__context__, asyncio.CancelledError)
        else:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await setup
            if failure_kind == "driver":
                assert isinstance(cancelled.value.__cause__, StoreDriverError)
                assert cancelled.value.__cause__.cause is driver_error
        await closer
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        if failure_kind is None:
            async with SqlAlchemyStore(engine) as peer:
                await peer.aput(("initialized",), "key", {})
                assert await peer.aget(("initialized",), "key") is not None
    finally:
        release.set()
        await asyncio.gather(setup, return_exceptions=True)
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", after_statement)
