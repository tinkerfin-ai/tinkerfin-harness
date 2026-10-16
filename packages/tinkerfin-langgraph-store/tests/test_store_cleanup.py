"""A retryable database conflict cannot hide a failed connection settlement."""

import sqlite3
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_langgraph_store import SqlAlchemyStore, StoreDriverError


@pytest.mark.parametrize("failure_at", [None, "rollback", "restore", "return"])
async def test_conflict_retries_only_after_clean_settlement(
    engine: AsyncEngine,
    store: SqlAlchemyStore,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str | None,
) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("This fault uses SQLite BUSY and query_only restoration")
    await store.setup()
    execute = AsyncConnection.execute
    driver_sql = AsyncConnection.exec_driver_sql
    close = AsyncConnection.close
    attempts = 0
    failures = 0
    cleanup_error = OSError("synthetic cleanup failure")

    async def query(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal attempts
        if connection.engine is engine and str(statement).startswith(
            "SELECT tinkerfin_store_documents"
        ):
            attempts += 1
            if attempts == 1:
                original = sqlite3.OperationalError("synthetic SQLITE_BUSY")
                original.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise OperationalError("select document", None, original)
        return await execute(connection, statement, *args, **kwargs)

    def fail_once(connection: AsyncConnection, location: str) -> None:
        nonlocal failures
        if (
            connection.engine is engine
            and attempts
            and not failures
            and failure_at == location
        ):
            failures += 1
            raise cleanup_error

    async def command(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> Any:
        if statement == "ROLLBACK":
            fail_once(connection, "rollback")
        elif statement == "PRAGMA query_only = 0":
            fail_once(connection, "restore")
        return await driver_sql(connection, statement, *args, **kwargs)

    async def return_connection(connection: AsyncConnection) -> None:
        fail_once(connection, "return")
        await close(connection)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", query)
        patch.setattr(AsyncConnection, "exec_driver_sql", command)
        patch.setattr(AsyncConnection, "close", return_connection)
        if failure_at is None:
            assert await store.aget(("files",), "missing") is None
            assert attempts == 2
        else:
            with pytest.raises(StoreDriverError) as caught:
                await store.aget(("files",), "missing")
            assert attempts == 1
            pending: list[BaseException] = [caught.value]
            seen: set[int] = set()
            found = False
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                found = found or error is cleanup_error
                pending.extend(
                    item
                    for item in (error.__cause__, error.__context__)
                    if item is not None
                )
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
            assert found
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.checkedout() == 0
    assert await store.aget(("files",), "missing") is None
