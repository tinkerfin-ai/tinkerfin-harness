"""Observable transaction, cancellation, and pool-reuse guarantees on real SQLite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tinkerfin_sqlalchemy import (
    SqlTransaction,
    database_capabilities,
)


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}",
        pool_size=3,
        max_overflow=0,
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode = WAL")
            await connection.exec_driver_sql(
                "CREATE TABLE records (value INTEGER NOT NULL)"
            )
            await connection.exec_driver_sql("INSERT INTO records VALUES (0)")
        yield instance
    finally:
        await instance.dispose()


async def _value(engine: AsyncEngine) -> object:
    async with SqlTransaction(engine, read_only=True) as connection:
        return await connection.scalar(text("SELECT value FROM records"))


async def test_write_commit_rollback_and_engine_borrowing(engine: AsyncEngine) -> None:
    committed = SqlTransaction(engine)
    async with committed as connection:
        features = database_capabilities(connection)
        assert features.dialect == "sqlite" and not features.row_locks
        await connection.exec_driver_sql("UPDATE records SET value = 1")
    assert committed.committed and not committed.rolled_back
    failed = SqlTransaction(engine)
    failure = RuntimeError("body failure")
    with pytest.raises(RuntimeError) as caught:
        async with failed as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 2")
            raise failure
    assert caught.value is failure
    assert failed.rolled_back and not failed.committed and not failed.commit_uncertain
    assert await _value(engine) == 1


@pytest.mark.parametrize("invalidate_listener_failure", [False, True])
async def test_cancel_before_commit_rolls_back_and_releases_connection(
    engine: AsyncEngine,
    invalidate_listener_failure: bool,
) -> None:
    if invalidate_listener_failure:
        _fail_invalidation_event(engine)
    entered, hold = asyncio.Event(), asyncio.Event()
    transaction = SqlTransaction(engine)
    original_driver: object | None = None

    async def write() -> None:
        nonlocal original_driver
        async with transaction as connection:
            original_driver = (await connection.get_raw_connection()).driver_connection
            await connection.exec_driver_sql("UPDATE records SET value = 7")
            entered.set()
            await hold.wait()

    writer = asyncio.create_task(write())
    try:
        await entered.wait()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
    finally:
        hold.set()
        await asyncio.gather(writer, return_exceptions=True)
    assert not transaction.committed
    assert await _value(engine) == 0
    async with engine.connect() as connection:
        assert (
            await connection.get_raw_connection()
        ).driver_connection is not original_driver


@pytest.mark.parametrize("failure_after_commit", [False, True])
async def test_commit_settles_under_repeated_cancellation_and_records_uncertainty(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failure_after_commit: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.exec_driver_sql
    transaction = SqlTransaction(engine)

    async def execute(
        connection: AsyncConnection,
        statement: str,
        parameters: Sequence[Mapping[str, Any] | tuple[Any, ...]]
        | Mapping[str, Any]
        | tuple[Any, ...]
        | None = None,
        execution_options: Mapping[str, object] | None = None,
    ) -> CursorResult[Any]:
        if statement == "COMMIT":
            entered.set()
            await release.wait()
        result = await original(connection, statement, parameters, execution_options)
        if statement == "COMMIT" and failure_after_commit:
            raise OperationalError(
                statement, None, OSError("commit acknowledgement lost")
            )
        return result

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)

    async def write() -> None:
        async with transaction as connection:
            await connection.exec_driver_sql("UPDATE records SET value = value + 1")

    writer = asyncio.create_task(write())
    try:
        await entered.wait()
        writer.cancel("cancel commit waiter")
        writer.cancel("repeat cancellation")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await writer
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
    assert transaction.committed is not failure_after_commit
    assert transaction.commit_uncertain is failure_after_commit
    assert await _value(engine) == 1
    if failure_after_commit:
        assert caught.value.__cause__ is not None


async def test_rollback_failure_retains_original_and_discards_connection(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AsyncConnection.exec_driver_sql
    rollback_failure = OperationalError("ROLLBACK", None, OSError("rollback failed"))
    body_failure = RuntimeError("body failed")
    transaction = SqlTransaction(engine)

    async def execute(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> CursorResult[Any]:
        if statement == "ROLLBACK":
            raise rollback_failure
        return await original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)
    with pytest.raises(RuntimeError) as caught:
        async with transaction as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 9")
            raise body_failure
    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", original)
    assert caught.value is body_failure and caught.value.__cause__ is rollback_failure
    assert not transaction.rolled_back and not transaction.committed
    assert await _value(engine) == 0


def _fail_invalidation_event(engine: AsyncEngine) -> None:
    def listener(
        _connection: object, _record: object, _error: BaseException | None
    ) -> None:
        raise RuntimeError("host invalidation listener failed")

    event.listen(engine.sync_engine.pool, "invalidate", listener)
