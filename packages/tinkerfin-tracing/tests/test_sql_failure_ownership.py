"""SQL cleanup and cancellation retain their original diagnostic failures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TypeGuard

import pytest
from sqlalchemy import event
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import SqlAlchemyTraceStore, TraceStoreError, TraceStoreOptions


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


def _failures(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        found.append(item)
        pending.extend(
            value for value in (item.__cause__, item.__context__) if value is not None
        )
        if _is_group(item):
            pending.extend(item.exceptions)
    return found


@pytest.mark.parametrize("operation", ["setup", "registration"])
async def test_real_busy_with_close_failure_does_not_retry_or_lose_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'busy-close.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2, commit_retry_delay_seconds=0.001
        ),
    )
    if operation == "registration":
        await store.setup()
    failed_connections: set[AsyncConnection] = set()
    database_errors: list[DBAPIError] = []
    closed: list[bool] = []
    cleanup = OSError("connection close failed")
    cause = LookupError("close provider cause")
    cleanup.__cause__ = cause
    original_driver = AsyncConnection.exec_driver_sql
    original_close = AsyncConnection.close

    def reject_busy_immediately(
        _connection: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement == "BEGIN IMMEDIATE":
            cursor.execute("PRAGMA busy_timeout = 0")

    event.listen(engine.sync_engine, "before_cursor_execute", reject_busy_immediately)

    async def execute(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> CursorResult[Any]:
        try:
            return await original_driver(connection, statement, *args, **kwargs)
        except DBAPIError as error:
            failed_connections.add(connection)
            database_errors.append(error)
            raise

    async def close(connection: AsyncConnection) -> None:
        await original_close(connection)
        if connection in failed_connections:
            closed.append(connection.closed)
            raise cleanup

    try:
        async with engine.connect() as blocker:
            await blocker.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(AsyncConnection, "exec_driver_sql", execute)
                    patch.setattr(AsyncConnection, "close", close)
                    with pytest.raises(TraceStoreError) as caught:
                        if operation == "setup":
                            await store.setup()
                        else:
                            await store.open_writer(
                                RunIdentity(
                                    namespace="alpha", thread_id="new", run_id="first"
                                )
                            )
                assert len(database_errors) == 1
                failures = _failures(caught.value)
                assert database_errors[0] in failures
                assert cleanup in failures
                assert cause in failures
                assert closed == [True]
            finally:
                await blocker.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
async def test_read_cleanup_control_retains_body_failure_and_original_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'failures.db'}")
    store = SqlAlchemyTraceStore(engine)
    await store.setup()
    body = RuntimeError("read body failed")
    control = failure_type("close control")
    cause = OSError("close original cause")
    control.__cause__ = cause
    original_close = AsyncConnection.close
    closed: list[bool] = []

    async def fail_read(
        _connection: AsyncConnection, *_args: Any, **_kwargs: Any
    ) -> Any:
        raise body

    async def fail_close(connection: AsyncConnection) -> None:
        await original_close(connection)
        closed.append(connection.closed)
        raise control

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", fail_read)
            patch.setattr(AsyncConnection, "close", fail_close)
            with pytest.raises(failure_type) as caught:
                await store.snapshot(
                    ThreadIdentity(namespace="default", thread_id="missing")
                )
        assert caught.value is control
        assert body in _failures(control)
        assert cause in _failures(control)
        assert closed and all(closed)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, RuntimeError])
async def test_cancelled_read_joins_cleanup_once_and_keeps_independent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    store = SqlAlchemyTraceStore(engine)
    await store.setup()
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = failure_type("read settlement")
    cause = OSError("settlement cause")
    failure.__cause__ = cause
    cancellations: list[int] = []
    outcomes: list[BaseException] = []

    async def wait_read(
        _connection: AsyncConnection, *_args: Any, **_kwargs: Any
    ) -> Any:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
            task = asyncio.current_task()
            assert task is not None
            cancellations.append(task.cancelling())
            raise failure

    async def caller() -> None:
        try:
            await store.snapshot(
                ThreadIdentity(namespace="default", thread_id="missing")
            )
        except BaseException as error:  # noqa: BLE001 - capture process control inside its owner
            outcomes.append(error)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", wait_read)
            task = asyncio.create_task(caller())
            try:
                await entered.wait()
                task.cancel("first cancellation")
                await closing.wait()
                task.cancel("repeated cancellation")
            finally:
                release.set()
                await task
        assert cancellations == [1]
        assert len(outcomes) == 1
        selected = outcomes[0]
        if failure_type is RuntimeError:
            assert isinstance(selected, asyncio.CancelledError)
        else:
            assert selected is failure
        assert cause in _failures(selected)
        assert failure in _failures(selected)
        assert any(
            isinstance(item, asyncio.CancelledError) for item in _failures(selected)
        )
        # The same Engine remains usable after the cancelled read releases ownership.
        await SqlAlchemyTraceStore(engine).setup()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure_point", ["busy_commit", "closed_after_commit"])
async def test_commit_failure_does_not_repeat_an_applied_ledger_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    import sqlite3
    from datetime import UTC, datetime

    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    from tinkerfin_contracts import RunIdentity
    from tinkerfin_tracing import RunFact, TraceStoreError, TraceStoreOptions

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'commit.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
            writer_lease_seconds=3600,
            writer_heartbeat_interval_seconds=1800,
        ),
    )
    identity = RunIdentity(namespace="default", thread_id="commit", run_id="first")
    writer = await store.open_writer(identity)
    execute = AsyncConnection.exec_driver_sql
    close = AsyncConnection.close
    attempts = 0
    applied = 0
    acknowledged = False

    def observe_write(
        _connection: Any, _cursor: Any, statement: str, *_args: Any
    ) -> None:
        nonlocal applied
        if statement.startswith("INSERT INTO tinkerfin_trace_events"):
            applied += 1

    def busy() -> DBAPIError:
        cause = sqlite3.OperationalError("database is locked")
        cause.sqlite_errorcode = sqlite3.SQLITE_BUSY
        return DBAPIError(None, None, cause)

    async def commit_observed(
        connection: AsyncConnection, command: str, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal attempts, acknowledged
        if command == "COMMIT":
            attempts += 1
            if failure_point == "busy_commit" and attempts == 1:
                raise busy()
        result = await execute(connection, command, *args, **kwargs)
        if command == "COMMIT":
            acknowledged = True
        return result

    async def close_observed(connection: AsyncConnection) -> None:
        await close(connection)
        if acknowledged and failure_point == "closed_after_commit":
            raise busy()

    event.listen(engine.sync_engine, "after_cursor_execute", observe_write)
    fact = RunFact(
        identity=identity,
        source_observation_id="started",
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "exec_driver_sql", commit_observed)
            patch.setattr(AsyncConnection, "close", close_observed)
            if failure_point == "busy_commit":
                assert (await writer.append((fact,)))[0].trace_seq == 1
            else:
                with pytest.raises(TraceStoreError):
                    await writer.append((fact,))
        assert applied == 1
        assert attempts == (2 if failure_point == "busy_commit" else 1)
        assert (await store.snapshot(identity.thread)).as_of_seq == 1
    finally:
        await writer.aclose()
        await engine.dispose()
