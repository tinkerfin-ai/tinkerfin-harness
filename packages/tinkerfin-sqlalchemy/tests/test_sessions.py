"""Root rollback, cancellation and Engine ownership through ORM sessions."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from tinkerfin_sqlalchemy import SqlSession, SqlTransaction


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    value = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'orm.sqlite'}", connect_args={"timeout": 0}
    )
    try:
        async with SqlTransaction(value) as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE records (value INTEGER NOT NULL)"
            )
        yield value
    finally:
        await value.dispose()


@pytest.mark.parametrize("initial_read", [False, True])
async def test_root_rollback_includes_released_savepoints(
    engine: AsyncEngine, initial_read: bool
) -> None:
    sessions = async_sessionmaker(engine, class_=SqlSession, expire_on_commit=False)
    async with sessions() as session:
        if initial_read:
            await session.execute(text("SELECT 1"))
        async with session.begin_nested():
            await session.execute(text("INSERT INTO records VALUES (1)"))
        await session.rollback()
    async with SqlTransaction(engine, read_only=True) as connection:
        assert await connection.scalar(text("SELECT count(*) FROM records")) == 0


async def test_explicit_commit_retains_data_and_engine_remains_borrowed(
    engine: AsyncEngine,
) -> None:
    async with SqlSession(engine) as session:
        async with session.begin_nested():
            await session.execute(text("INSERT INTO records VALUES (1)"))
        await session.commit()
    async with SqlTransaction(engine) as connection:
        await connection.execute(text("INSERT INTO records VALUES (2)"))
    async with SqlSession(engine) as session:
        assert await session.scalar(text("SELECT sum(value) FROM records")) == 3


async def test_cancelled_body_rolls_back_its_savepoints(engine: AsyncEngine) -> None:
    entered = asyncio.Event()

    async def operation() -> None:
        async with SqlSession(engine) as session:
            async with session.begin_nested():
                await session.execute(text("INSERT INTO records VALUES (1)"))
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(operation())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    async with SqlTransaction(engine, read_only=True) as connection:
        assert await connection.scalar(text("SELECT count(*) FROM records")) == 0


async def test_plain_read_keeps_native_isolation_without_blocking_another_writer(
    engine: AsyncEngine,
) -> None:
    async with SqlSession(engine) as reader:
        assert await reader.scalar(text("SELECT count(*) FROM records")) == 0
        async with SqlSession(engine) as writer:
            await writer.execute(text("INSERT INTO records VALUES (1)"))
            await writer.commit()
    async with SqlTransaction(engine, read_only=True) as connection:
        assert await connection.scalar(text("SELECT count(*) FROM records")) == 1


@pytest.mark.parametrize("body_failure", [False, True])
async def test_factory_transaction_joins_close_before_propagating_cancellation(
    engine: AsyncEngine, body_failure: bool
) -> None:
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ownership_at_return: list[bool] = []

    class ObservedSession(SqlSession):
        async def close(self) -> None:
            entered.set()
            await release.wait()
            await super().close()
            closed.set()

    sessions = async_sessionmaker(engine, class_=ObservedSession)

    async def operation() -> None:
        try:
            async with sessions.begin() as session:
                await session.execute(text("INSERT INTO records VALUES (1)"))
                if body_failure:
                    raise ValueError("The operation failed")
        finally:
            ownership_at_return.append(closed.is_set())

    running = asyncio.create_task(operation())
    try:
        await entered.wait()
        running.cancel()
        running.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert ownership_at_return == [True]
        async with SqlTransaction(engine, read_only=True) as connection:
            assert await connection.scalar(text("SELECT count(*) FROM records")) == (
                0 if body_failure else 1
            )
    finally:
        release.set()
        await closed.wait()
        await asyncio.gather(running, return_exceptions=True)


async def test_factory_commit_failure_closes_the_borrowed_session(
    engine: AsyncEngine,
) -> None:
    failure = ValueError("Commit policy rejected the transaction")
    closed = asyncio.Event()

    class ObservedSession(SqlSession):
        async def close(self) -> None:
            await super().close()
            closed.set()

    def reject_commit(_session: object) -> None:
        raise failure

    sessions = async_sessionmaker(engine, class_=ObservedSession)
    session: ObservedSession | None = None
    try:
        with pytest.raises(ValueError) as caught:
            async with sessions.begin() as session:
                event.listen(session.sync_session, "before_commit", reject_commit)
                await session.execute(text("INSERT INTO records VALUES (1)"))
        assert caught.value is failure
        assert closed.is_set()
        async with SqlTransaction(engine, read_only=True) as connection:
            assert await connection.scalar(text("SELECT count(*) FROM records")) == 0
    finally:
        if session is not None:
            await session.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_factory_entry_failure_releases_connections_before_returning(
    engine: AsyncEngine, cancel: bool
) -> None:
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sessions_opened: list[SqlSession] = []
    returned: list[bool] = []
    failure = ValueError("Transaction startup failed")

    async def suspend(_driver: object) -> None:
        entered.set()
        await release.wait()

    def fail_begin(session: Session, _transaction: object) -> None:
        connection = session.connection()
        connection.execute(text("SELECT 1"))
        if cancel:
            driver = connection.connection.dbapi_connection
            assert isinstance(driver, AdaptedConnection)
            driver.run_async(suspend)
        else:
            raise failure

    class ObservedSession(SqlSession):
        def __init__(self, bind: AsyncEngine, **options: Any) -> None:
            super().__init__(bind, **options)
            sessions_opened.append(self)
            event.listen(self.sync_session, "after_transaction_create", fail_begin)

        async def close(self) -> None:
            await super().close()
            closed.set()

    event.listen(engine.sync_engine, "checkin", lambda *_args: returned.append(True))
    sessions = async_sessionmaker(engine, class_=ObservedSession)

    async def operation() -> None:
        async with sessions.begin():
            raise AssertionError("Failed transaction startup must not enter the body")

    running = asyncio.create_task(operation())
    try:
        if cancel:
            await entered.wait()
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError) as caught:
            await running
        if not cancel:
            assert caught.value is failure
        assert closed.is_set()
        assert returned == [True]
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        for session in sessions_opened:
            await session.close()
