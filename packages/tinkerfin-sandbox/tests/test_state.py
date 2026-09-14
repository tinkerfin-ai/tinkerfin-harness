from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Never, cast

import aiosqlite
import pytest
from sqlalchemy import ExceptionContext, Table, event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql import Executable
from sqlalchemy.sql.dml import Update
from tests.support.sql_engines import SqlEngineFactory
from tests.support.sql_faults import after_sql_commit

import tinkerfin_sandbox
from tinkerfin_sandbox.lifecycle import _sql_transactions


@contextmanager
def _lock_signal(engine: AsyncEngine, command: str) -> Iterator[asyncio.Event]:
    observed = asyncio.Event()

    def failed(context: ExceptionContext) -> None:
        if context.statement == command:
            observed.set()

    event.listen(engine.sync_engine, "handle_error", failed)
    try:
        yield observed
    finally:
        event.remove(engine.sync_engine, "handle_error", failed)


@contextmanager
def _retry_clock(
    monkeypatch: pytest.MonkeyPatch, *, blocked: asyncio.Event | None = None
) -> Iterator[list[float]]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        if blocked is not None:
            blocked.set()
            await asyncio.Event().wait()

    controlled = SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: sum(delays)),
        sleep=sleep,
        CancelledError=asyncio.CancelledError,
    )
    with monkeypatch.context() as patch:
        patch.setattr(_sql_transactions, "asyncio", controlled)
        yield delays


class _StateClock:
    """Advance lease time independently of database or event-loop speed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.current = datetime(2030, 1, 1)
        monkeypatch.setattr(
            tinkerfin_sandbox.SQLAlchemyOpenSandboxState, "_now", staticmethod(self.now)
        )

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _public_type(name: str) -> type:
    value = getattr(tinkerfin_sandbox, name, None)
    assert isinstance(value, type), f"tinkerfin_sandbox.{name} must be public"
    return value


async def test_memory_state_serializes_owner_and_publishes_binding() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()
    await state.start(warm_pool_size=0)

    first = await state.acquire_owner("user-A")
    waiting = asyncio.create_task(state.acquire_owner("user-A"))
    await asyncio.sleep(0)

    assert not waiting.done()
    assert first.binding is None
    assert len(first.owner_digest) == 43
    assert "user-A" not in first.owner_digest

    committed = await state.bind_owner(first, "sandbox-1")
    await state.release_owner(first)
    second = await waiting

    assert committed.sandbox_id == "sandbox-1"
    assert committed.generation == first.generation
    assert second.binding == committed
    await state.release_owner(second)
    assert await state.read_binding("user-A") == committed
    await state.aclose()


async def test_memory_state_rejects_a_released_owner_claim() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    ownership_error = _public_type("OpenSandboxStateOwnershipError")
    state = state_type()
    await state.start(warm_pool_size=0)

    stale = await state.acquire_owner("user-A")
    await state.release_owner(stale)
    current = await state.acquire_owner("user-A")

    with pytest.raises(ownership_error):
        await state.bind_owner(stale, "stale-sandbox")

    await state.release_owner(current)
    assert await state.read_binding("user-A") is None
    await state.aclose()


async def test_memory_state_consumes_one_global_warm_slot_into_owner_binding() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()
    await state.start(warm_pool_size=1)

    warm_claim = await state.claim_warm_slot()
    assert warm_claim is not None
    assert await state.claim_warm_slot() is None

    await state.publish_warm(warm_claim, "warm-sandbox")
    owner_claim = await state.acquire_owner("user-A")
    binding = await state.consume_warm(owner_claim)

    assert binding is not None
    assert binding.sandbox_id == "warm-sandbox"
    await state.release_owner(owner_claim)
    assert await state.read_binding("user-A") == binding

    refill_claim = await state.claim_warm_slot()
    assert refill_claim is not None
    assert refill_claim.slot == warm_claim.slot
    await state.release_warm(refill_claim)
    await state.aclose()


async def test_memory_state_rejects_negative_warm_capacity() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()

    with pytest.raises(ValueError, match="warm_pool_size"):
        await state.start(warm_pool_size=-1)

    await state.aclose()


async def test_memory_state_repeated_start_requires_the_same_capacity() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    configuration_error = _public_type("OpenSandboxStateConfigurationError")
    state = state_type()
    try:
        await state.start(warm_pool_size=1)
        await state.start(warm_pool_size=1)

        with pytest.raises(configuration_error, match="warm_pool_size"):
            await state.start(warm_pool_size=2)
    finally:
        await state.aclose()


async def test_memory_state_cleanup_claim_is_recoverable_until_completed() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()
    await state.start(warm_pool_size=0)

    await state.enqueue_cleanup("orphan-sandbox")
    first = await state.claim_cleanup()

    assert first is not None
    assert first.sandbox_id == "orphan-sandbox"
    assert await state.claim_cleanup() is None

    await state.release_cleanup(first)
    retry = await state.claim_cleanup()
    assert retry is not None
    assert retry.generation > first.generation
    await state.complete_cleanup(retry)

    assert await state.claim_cleanup() is None
    await state.aclose()


async def test_memory_state_unbind_requires_the_current_owner_claim() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()
    await state.start(warm_pool_size=0)

    claim = await state.acquire_owner("user-A")
    await state.bind_owner(claim, "sandbox-1")
    await state.unbind_owner(claim)
    await state.release_owner(claim)

    assert await state.read_binding("user-A") is None
    await state.aclose()


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _immediate_sqlite_url(path: Path) -> str:
    """Disable driver busy waiting so State owns the complete retry budget."""

    return f"{_sqlite_url(path)}?timeout=0"


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        pytest.param("sqlite+aiosqlite:///:memory:", "sqlite", id="sqlite"),
        pytest.param(
            "mysql+asyncmy://user:secret@localhost/database",
            "mysql",
            id="mysql",
        ),
    ],
)
async def test_sqlalchemy_state_boundary_records_trusted_implementation_context(
    sql_engine: SqlEngineFactory,
    url: str,
    dialect: str,
) -> None:
    state = tinkerfin_sandbox.SQLAlchemyOpenSandboxState(engine=sql_engine(url))
    try:
        with pytest.raises(
            tinkerfin_sandbox.OpenSandboxStateError,
            match="has not been started",
        ) as captured:
            await state.read_binding("owner-1")
    finally:
        await state.aclose()

    assert dict(captured.value.context) == {}
    assert dict(captured.value.diagnostic_context) == {
        "implementation": "sqlalchemy",
        "dialect": dialect,
        "operation": "read_binding",
    }
    assert url not in str(captured.value)
    assert "secret" not in str(captured.value)


async def test_sqlalchemy_state_borrows_engine_without_disposing_its_pool(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(_sqlite_url(tmp_path / "borrowed.db"))
    borrowed_pool = engine.pool
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA busy_timeout = 5000")
        await connection.rollback()
    state = tinkerfin_sandbox.SQLAlchemyOpenSandboxState(engine=engine)
    try:
        await state.start(warm_pool_size=0)
        claim = await state.acquire_owner("borrowed-owner")
        await state.release_owner(claim)
        await state.aclose()
        assert engine.pool is borrowed_pool
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            assert (
                await connection.exec_driver_sql("PRAGMA busy_timeout")
            ).scalar_one() == 5000
    finally:
        await engine.dispose()


async def _sqlite_busy_timeout(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        value = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
    if not isinstance(value, int):
        raise TypeError("SQLite busy_timeout must be an integer")
    return value


async def test_borrowed_sqlite_restores_session_setting_after_failure_and_cancel(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        _sqlite_url(tmp_path / "borrowed-settlement.db"),
        pool_size=1,
        max_overflow=0,
    )
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA busy_timeout = 4321")
        await connection.rollback()
    state = tinkerfin_sandbox.SQLAlchemyOpenSandboxState(engine=engine)
    failure = RuntimeError("borrowed operation failed")

    async def fail_operation(_connection: AsyncConnection) -> Never:
        raise failure

    entered = asyncio.Event()

    release_operation = asyncio.Event()

    async def block_operation(_connection: AsyncConnection) -> None:
        entered.set()
        await release_operation.wait()

    try:
        await state.start(warm_pool_size=0)
        with pytest.raises(RuntimeError) as captured:
            await state._run_write_transaction(fail_operation)
        assert captured.value is failure
        assert await _sqlite_busy_timeout(engine) == 4321

        operation = asyncio.create_task(state._run_write_transaction(block_operation))
        await asyncio.wait_for(entered.wait(), timeout=2)
        operation.cancel("borrowed operation cancelled")
        # The transaction owns database work through cancellation and rolls back
        # before COMMIT once that work has completed.
        release_operation.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="borrowed operation cancelled",
        ):
            await operation
        assert await _sqlite_busy_timeout(engine) == 4321
    finally:
        await state.aclose()
        await engine.dispose()


async def test_borrowed_sqlite_invalidates_an_uncertain_commit_connection(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        _sqlite_url(tmp_path / "borrowed-uncertain.db"), pool_size=1, max_overflow=0
    )
    state = tinkerfin_sandbox.SQLAlchemyOpenSandboxState(engine=engine)
    try:
        await state.start(warm_pool_size=0)
        async with engine.connect() as connection:
            original_connection = await connection.run_sync(
                lambda sync: sync.connection.dbapi_connection
            )
        failure = OperationalError("COMMIT", None, RuntimeError("response lost"))

        async def lose_acknowledgement() -> Never:
            raise failure

        with after_sql_commit(engine, lose_acknowledgement):
            with pytest.raises(
                tinkerfin_sandbox.OpenSandboxStateCommitUncertainError
            ) as captured:
                await state.enqueue_cleanup("uncertain-target")
        assert captured.value.cause is failure
        async with engine.connect() as connection:
            replacement_connection = await connection.run_sync(
                lambda sync: sync.connection.dbapi_connection
            )
        assert replacement_connection is not original_connection
        cleanup = await state.claim_cleanup()
        assert cleanup is not None and cleanup.sandbox_id == "uncertain-target"
    finally:
        await state.aclose()
        await engine.dispose()


async def test_borrowed_sqlite_cancelled_failed_commit_invalidates_without_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        _sqlite_url(tmp_path / "borrowed-cancelled-commit.db"),
        pool_size=1,
        max_overflow=0,
    )
    state = tinkerfin_sandbox.SQLAlchemyOpenSandboxState(engine=engine)
    await state.start(warm_pool_size=0)
    async with engine.connect() as connection:
        original_connection = await connection.run_sync(
            lambda sync_connection: sync_connection.connection.dbapi_connection
        )
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    original_exec_driver_sql = AsyncConnection.exec_driver_sql
    loop = asyncio.get_running_loop()
    original_exception_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, object]] = []

    async def fail_commit(
        connection: AsyncConnection,
        statement: str,
    ) -> CursorResult[tuple[object, ...]]:
        if statement == "COMMIT":
            commit_entered.set()
            await release_commit.wait()
            raise OperationalError(
                "COMMIT",
                None,
                RuntimeError("commit response failed"),
                connection_invalidated=False,
            )
        return cast(
            CursorResult[tuple[object, ...]],
            await original_exec_driver_sql(connection, statement),
        )

    async def no_op(_connection: AsyncConnection) -> None:
        return None

    def capture_loop_error(
        _loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        loop_errors.append(context)

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", fail_commit)
    loop.set_exception_handler(capture_loop_error)
    operation = asyncio.create_task(state._run_write_transaction(no_op))
    try:
        await asyncio.wait_for(commit_entered.wait(), timeout=2)
        operation.cancel("caller cancelled during commit")
        release_commit.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="caller cancelled during commit",
        ):
            await operation
        await asyncio.sleep(0)
    finally:
        release_commit.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        loop.set_exception_handler(original_exception_handler)
        monkeypatch.setattr(
            AsyncConnection,
            "exec_driver_sql",
            original_exec_driver_sql,
        )
    async with engine.connect() as connection:
        replacement_connection = await connection.run_sync(
            lambda sync_connection: sync_connection.connection.dbapi_connection
        )
    assert replacement_connection is not original_connection
    assert loop_errors == []
    await state.aclose()
    await engine.dispose()


async def test_sqlalchemy_state_requires_a_borrowed_engine(
    sql_engine: SqlEngineFactory,
) -> None:
    with pytest.raises(TypeError, match="engine"):
        tinkerfin_sandbox.SQLAlchemyOpenSandboxState()  # pyright: ignore[reportCallIssue]
    engine = sql_engine("sqlite+aiosqlite:///:memory:")
    with pytest.raises(TypeError, match="url"):
        tinkerfin_sandbox.SQLAlchemyOpenSandboxState(
            engine=engine,
            url="sqlite+aiosqlite:///:memory:",  # pyright: ignore[reportCallIssue]
        )


async def _hold_sqlite_write_lock(path: Path) -> aiosqlite.Connection:
    """Return a real independent connection holding SQLite's write reservation."""

    connection = await aiosqlite.connect(path, timeout=0)
    await connection.execute("BEGIN IMMEDIATE")
    return connection


def _sqlite_error_code(error: BaseException | None) -> int | None:
    if not isinstance(error, DBAPIError) or not isinstance(error.orig, sqlite3.Error):
        return None
    code = getattr(error.orig, "sqlite_errorcode", None)
    return code if isinstance(code, int) else None


def _sqlite_lock_operational_error(
    statement: str,
    *,
    code: int = sqlite3.SQLITE_BUSY,
) -> OperationalError:
    original = sqlite3.OperationalError("database is locked")
    original.sqlite_errorcode = code
    original.sqlite_errorname = (
        "SQLITE_BUSY" if code == sqlite3.SQLITE_BUSY else "SQLITE_LOCKED"
    )
    return OperationalError(statement, (), original)


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        pytest.param(True, TypeError, id="boolean"),
        pytest.param(-0.1, ValueError, id="negative"),
        pytest.param(float("inf"), ValueError, id="infinite"),
    ],
)
def test_sqlite_state_rejects_an_invalid_retry_timeout(
    sql_engine: SqlEngineFactory,
    value: object,
    error_type: type[Exception],
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")

    with pytest.raises(error_type, match="sqlite_retry_timeout"):
        state_type(
            engine=sql_engine("sqlite+aiosqlite:///:memory:"),
            sqlite_retry_timeout=value,
        )


async def test_sqlite_state_retries_a_short_real_write_lock(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    database_path = tmp_path / "short-write-lock.db"
    url = _immediate_sqlite_url(database_path)
    first = state_type(
        engine=sql_engine(url),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    second = state_type(
        engine=sql_engine(url),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    await first.start(warm_pool_size=0)
    await second.start(warm_pool_size=0)
    holder = await _hold_sqlite_write_lock(database_path)

    async def release_lock(locked: asyncio.Event) -> None:
        await locked.wait()
        await holder.rollback()

    with _lock_signal(second._engine, "BEGIN IMMEDIATE") as locked:
        releasing = asyncio.create_task(release_lock(locked))
        try:
            claim = await asyncio.wait_for(second.acquire_owner("user-A"), timeout=2)
            await second.release_owner(claim)
            await releasing

            probe = await first.acquire_owner("user-B")
            await first.release_owner(probe)
        finally:
            if not releasing.done():
                releasing.cancel()
                await asyncio.gather(releasing, return_exceptions=True)
            await holder.rollback()
            await holder.close()
            await asyncio.gather(first.aclose(), second.aclose())


async def test_sqlite_state_stops_retrying_after_the_write_lock_budget(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "write-lock-budget.db"
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(database_path)),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.08,
    )
    await state.start(warm_pool_size=0)
    holder = await _hold_sqlite_write_lock(database_path)
    original_exec_driver_sql = AsyncConnection.exec_driver_sql
    begin_attempts = 0

    async def count_begin_attempts(
        connection: AsyncConnection,
        statement: str,
    ) -> CursorResult[tuple[object, ...]]:
        nonlocal begin_attempts
        if statement == "BEGIN IMMEDIATE":
            begin_attempts += 1
        return cast(
            CursorResult[tuple[object, ...]],
            await original_exec_driver_sql(connection, statement),
        )

    monkeypatch.setattr(
        AsyncConnection,
        "exec_driver_sql",
        count_begin_attempts,
    )
    try:
        with _retry_clock(monkeypatch) as delays:
            with pytest.raises(state_error, match="SQLite.*lock") as captured:
                await state.acquire_owner("user-A")
        assert sum(delays) == pytest.approx(0.08)
        assert begin_attempts > 1
        assert _sqlite_error_code(captured.value.__cause__) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }
    finally:
        await holder.rollback()
        await holder.close()

    claim = await state.acquire_owner("user-A")
    await state.release_owner(claim)
    await state.aclose()


async def test_sqlite_state_lock_backoff_is_immediately_cancellable(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    database_path = tmp_path / "cancel-write-lock.db"
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(database_path)),
        namespace="test",
        poll_interval=0.2,
        sqlite_retry_timeout=5,
    )
    await state.start(warm_pool_size=0)
    holder = await _hold_sqlite_write_lock(database_path)
    sleeping = asyncio.Event()
    with _retry_clock(monkeypatch, blocked=sleeping):
        acquiring = asyncio.create_task(state.acquire_owner("user-A"))
        try:
            await sleeping.wait()
            acquiring.cancel("caller stopped waiting for SQLite")
            with pytest.raises(
                asyncio.CancelledError, match="caller stopped waiting for SQLite"
            ):
                await asyncio.wait_for(acquiring, timeout=2)
        finally:
            if not acquiring.done():
                acquiring.cancel()
            await asyncio.gather(acquiring, return_exceptions=True)
            await holder.rollback()
            await holder.close()

    claim = await state.acquire_owner("user-A")
    await state.release_owner(claim)
    await state.aclose()


@pytest.mark.parametrize("lock_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
async def test_sqlite_state_retries_only_after_statement_rollback_and_close(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_code: int,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(tmp_path / "statement-lock.db")),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("user-A")
    original_execute = AsyncConnection.execute
    original_rollback = AsyncConnection.rollback
    original_close = AsyncConnection.close
    execute_attempts = 0
    rollback_calls = 0
    close_calls = 0

    async def fail_first_statement(
        connection: AsyncConnection,
        statement: Executable,
    ) -> CursorResult[tuple[object, ...]]:
        nonlocal execute_attempts
        if (
            isinstance(statement, Update)
            and isinstance(statement.table, Table)
            and statement.table.name == "tinkerfin_opensandbox_owners"
        ):
            execute_attempts += 1
            if execute_attempts == 1:
                raise _sqlite_lock_operational_error(
                    "UPDATE owners",
                    code=lock_code,
                )
            assert rollback_calls >= 1
            assert close_calls >= 1
        return cast(
            CursorResult[tuple[object, ...]],
            await original_execute(connection, statement),
        )

    async def observe_rollback(connection: AsyncConnection) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        await original_rollback(connection)

    async def observe_close(connection: AsyncConnection) -> None:
        nonlocal close_calls
        await original_close(connection)
        close_calls += 1

    monkeypatch.setattr(AsyncConnection, "execute", fail_first_statement)
    monkeypatch.setattr(AsyncConnection, "rollback", observe_rollback)
    monkeypatch.setattr(AsyncConnection, "close", observe_close)
    try:
        binding = await state.bind_owner(claim, "sandbox-1")
    finally:
        monkeypatch.setattr(AsyncConnection, "execute", original_execute)
        monkeypatch.setattr(AsyncConnection, "rollback", original_rollback)
        monkeypatch.setattr(AsyncConnection, "close", original_close)

    assert binding.sandbox_id == "sandbox-1"
    assert execute_attempts == 2
    assert rollback_calls >= 1
    assert close_calls >= 2
    await state.release_owner(claim)
    await state.aclose()


async def test_sqlite_state_does_not_retry_when_statement_rollback_fails(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(tmp_path / "rollback-failure.db")),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("user-A")
    original_execute = AsyncConnection.execute
    original_rollback = AsyncConnection.rollback
    execute_attempts = 0
    rollback_failure = RuntimeError("rollback connection was lost")

    async def fail_statement(
        connection: AsyncConnection,
        statement: Executable,
    ) -> Never:
        nonlocal execute_attempts
        del connection, statement
        execute_attempts += 1
        raise _sqlite_lock_operational_error("UPDATE owners")

    async def fail_rollback(connection: AsyncConnection) -> None:
        if execute_attempts == 0:
            await original_rollback(connection)
            return
        raise rollback_failure

    monkeypatch.setattr(AsyncConnection, "execute", fail_statement)
    monkeypatch.setattr(AsyncConnection, "rollback", fail_rollback)
    try:
        state_error = _public_type("UnexpectedOpenSandboxStateError")
        with pytest.raises(state_error, match="settlement failed") as captured:
            await state.bind_owner(claim, "sandbox-1")
    finally:
        monkeypatch.setattr(AsyncConnection, "execute", original_execute)
        monkeypatch.setattr(AsyncConnection, "rollback", original_rollback)

    assert execute_attempts == 1
    assert isinstance(captured.value.cause, OperationalError)
    pending_errors: list[BaseException] = [captured.value.cause]
    retained: set[int] = set()
    while pending_errors:
        error = pending_errors.pop()
        if id(error) in retained:
            continue
        retained.add(id(error))
        pending_errors.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        if isinstance(error, BaseExceptionGroup):
            pending_errors.extend(error.exceptions)
    assert id(rollback_failure) in retained
    assert await state.read_binding("user-A") is None
    await state.release_owner(claim)
    await state.aclose()


async def test_sqlite_state_retries_commit_without_replaying_the_transaction(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    database_path = tmp_path / "commit-lock.db"
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(database_path)),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("user-A")
    original_execute = AsyncConnection.execute
    transaction_body_calls = 0

    async def count_transaction_body(
        connection: AsyncConnection,
        statement: Executable,
    ) -> CursorResult[tuple[object, ...]]:
        nonlocal transaction_body_calls
        if (
            isinstance(statement, Update)
            and isinstance(statement.table, Table)
            and statement.table.name == "tinkerfin_opensandbox_owners"
        ):
            transaction_body_calls += 1
        return cast(
            CursorResult[tuple[object, ...]],
            await original_execute(connection, statement),
        )

    monkeypatch.setattr(AsyncConnection, "execute", count_transaction_body)
    reader = await aiosqlite.connect(database_path, timeout=0)
    await reader.execute("BEGIN")
    cursor = await reader.execute("SELECT sandbox_id FROM tinkerfin_opensandbox_owners")
    await cursor.fetchall()

    async def release_reader(locked: asyncio.Event) -> None:
        await locked.wait()
        await reader.commit()

    with _lock_signal(state._engine, "COMMIT") as locked:
        releasing = asyncio.create_task(release_reader(locked))
        try:
            binding = await asyncio.wait_for(
                state.bind_owner(claim, "sandbox-1"),
                timeout=1,
            )
            await releasing
        finally:
            if not releasing.done():
                releasing.cancel()
                await asyncio.gather(releasing, return_exceptions=True)
            await reader.rollback()
            await reader.close()

    assert binding == tinkerfin_sandbox.OpenSandboxBinding(
        sandbox_id="sandbox-1",
        generation=claim.generation,
    )
    assert transaction_body_calls == 1
    assert await state.read_binding("user-A") == binding
    await state.release_owner(claim)
    await state.aclose()


async def test_sqlite_state_does_not_replay_an_uncertain_commit(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "uncertain-commit.db"
    state = state_type(
        engine=sql_engine(_immediate_sqlite_url(database_path)),
        namespace="test",
        poll_interval=0.01,
        sqlite_retry_timeout=0.5,
    )
    await state.start(warm_pool_size=0)
    claim = await state.acquire_owner("user-A")
    original_exec_driver_sql = AsyncConnection.exec_driver_sql
    committed_responses = 0

    async def commit_then_lose_response(
        connection: AsyncConnection,
        statement: str,
    ) -> CursorResult[tuple[object, ...]]:
        nonlocal committed_responses
        result = cast(
            CursorResult[tuple[object, ...]],
            await original_exec_driver_sql(connection, statement),
        )
        if statement != "COMMIT":
            return result
        committed_responses += 1
        original = sqlite3.OperationalError("commit response was lost")
        original.sqlite_errorcode = sqlite3.SQLITE_IOERR
        original.sqlite_errorname = "SQLITE_IOERR"
        raise OperationalError("COMMIT", (), original)

    monkeypatch.setattr(
        AsyncConnection,
        "exec_driver_sql",
        commit_then_lose_response,
    )
    try:
        with pytest.raises(
            state_error, match="COMMIT outcome is uncertain"
        ) as captured:
            await state.bind_owner(claim, "sandbox-1")
    finally:
        monkeypatch.setattr(
            AsyncConnection,
            "exec_driver_sql",
            original_exec_driver_sql,
        )

    assert committed_responses == 1
    assert isinstance(captured.value.__cause__, OperationalError)
    assert "commit response was lost" in str(captured.value.__cause__)
    assert dict(captured.value.diagnostic_context) == {
        "implementation": "sqlalchemy",
        "dialect": "sqlite",
        "operation": "bind_owner",
    }
    assert await state.read_binding("user-A") == tinkerfin_sandbox.OpenSandboxBinding(
        sandbox_id="sandbox-1",
        generation=claim.generation,
    )
    await state.release_owner(claim)
    await state.aclose()


async def test_sqlite_state_auto_initializes_and_recovers_binding(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "state.db")

    first = state_type(engine=sql_engine(url), namespace="test")
    await first.start(warm_pool_size=0)
    claim = await first.acquire_owner("user-A")
    committed = await first.bind_owner(claim, "sandbox-1")
    await first.release_owner(claim)
    await first.aclose()

    restarted = state_type(engine=sql_engine(url), namespace="test")
    await restarted.start(warm_pool_size=0)

    assert await restarted.read_binding("user-A") == committed
    await restarted.aclose()


async def test_sqlite_state_fences_ready_warm_reconciliation(
    sql_engine: SqlEngineFactory, tmp_path: Path
) -> None:
    """Ready-slot maintenance must preserve the ID until a fenced publish."""

    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state = state_type(
        engine=sql_engine(_sqlite_url(tmp_path / "ready-warm.db")), namespace="test"
    )
    await state.start(warm_pool_size=1)
    try:
        initial = await state.claim_warm_slot()
        assert initial is not None
        await state.publish_warm(initial, "warm-1")
        assert await state.warm_pool_ready()

        ready = await state.claim_ready_warm_slot(exclude_slots=())
        assert ready is not None
        assert ready.sandbox_id == "warm-1"
        assert await state.warm_pool_ready()
        assert await state.claim_ready_warm_slot(exclude_slots=(ready.slot,)) is None

        await state.publish_warm(ready, "warm-1")
        assert await state.warm_pool_ready()
    finally:
        await state.aclose()


async def test_sqlite_state_discards_unusable_warm_id_with_durable_cleanup(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    """Invalidating a ready slot must atomically retain its remote cleanup duty."""

    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state = state_type(
        engine=sql_engine(_sqlite_url(tmp_path / "discard-warm.db")), namespace="test"
    )
    await state.start(warm_pool_size=1)
    try:
        initial = await state.claim_warm_slot()
        assert initial is not None
        await state.publish_warm(initial, "warm-stale")
        ready = await state.claim_ready_warm_slot(exclude_slots=())
        assert ready is not None

        await state.discard_ready_warm_slot(ready)

        assert not await state.warm_pool_ready()
        replacement = await state.claim_warm_slot()
        assert replacement is not None
        await state.release_warm(replacement)
        cleanup = await state.claim_cleanup()
        assert cleanup is not None
        assert cleanup.sandbox_id == "warm-stale"
        await state.complete_cleanup(cleanup)
    finally:
        await state.aclose()


async def test_sqlite_restart_releases_a_dead_worker_warm_claim(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    """A dead worker claim must not leave startup capacity permanently empty."""

    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "dead-warm-claim.db")
    first = state_type(engine=sql_engine(url), namespace="test")
    await first.start(warm_pool_size=1)
    abandoned = await first.claim_warm_slot()
    assert abandoned is not None
    await first.aclose()

    restarted = state_type(engine=sql_engine(url), namespace="test")
    await restarted.start(warm_pool_size=1)
    try:
        replacement = await restarted.claim_warm_slot()
        assert replacement is not None
        assert replacement.slot == abandoned.slot
        assert replacement.generation > abandoned.generation
        await restarted.release_warm(replacement)
    finally:
        await restarted.aclose()


async def test_sqlite_state_repeated_start_requires_the_same_capacity(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    configuration_error = _public_type("OpenSandboxStateConfigurationError")
    state = state_type(engine=sql_engine(_sqlite_url(tmp_path / "repeat-capacity.db")))
    try:
        await state.start(warm_pool_size=1)
        await state.start(warm_pool_size=1)

        with pytest.raises(
            configuration_error,
            match="warm_pool_size",
        ) as captured:
            await state.start(warm_pool_size=2)
    finally:
        await state.aclose()

    assert dict(captured.value.diagnostic_context) == {
        "implementation": "sqlalchemy",
        "dialect": "sqlite",
        "operation": "start",
    }


async def test_sqlite_state_concurrent_start_is_idempotent(
    sql_engine: SqlEngineFactory, tmp_path: Path
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state = state_type(engine=sql_engine(_sqlite_url(tmp_path / "concurrent-start.db")))
    try:
        await asyncio.gather(
            state.start(warm_pool_size=1),
            state.start(warm_pool_size=1),
        )

        claim = await state.acquire_owner("user-A")
        await state.release_owner(claim)
    finally:
        await state.aclose()


async def test_sqlite_state_close_settles_a_concurrent_start(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "start-close.db")
    state = state_type(engine=sql_engine(url), namespace="test")
    connection_closed = asyncio.Event()
    release_close = asyncio.Event()
    original_close = AsyncConnection.close

    async def gated_connection_close(connection: AsyncConnection) -> None:
        await original_close(connection)
        if not connection_closed.is_set():
            connection_closed.set()
            await release_close.wait()

    monkeypatch.setattr(AsyncConnection, "close", gated_connection_close)
    starting = asyncio.create_task(state.start(warm_pool_size=1))
    closing: asyncio.Task[None] | None = None
    try:
        await connection_closed.wait()
        closing = asyncio.create_task(state.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release_close.set()
        await asyncio.gather(starting, closing)
    finally:
        release_close.set()
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        if closing is not None and not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await state.aclose()

    replacement = state_type(engine=sql_engine(url), namespace="test")
    try:
        await replacement.start(warm_pool_size=2)
    finally:
        await replacement.aclose()


async def test_sqlite_state_close_settles_a_cancelled_committed_start(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "cancelled-start-close.db")
    state = state_type(engine=sql_engine(url), namespace="test")
    connection_closed = asyncio.Event()
    release_close = asyncio.Event()
    original_close = AsyncConnection.close

    async def gated_connection_close(connection: AsyncConnection) -> None:
        await original_close(connection)
        if not connection_closed.is_set():
            connection_closed.set()
            await release_close.wait()

    monkeypatch.setattr(AsyncConnection, "close", gated_connection_close)
    starting = asyncio.create_task(state.start(warm_pool_size=1))
    try:
        await connection_closed.wait()
        starting.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        await state.aclose()

        replacement = state_type(engine=sql_engine(url), namespace="test")
        try:
            await replacement.start(warm_pool_size=2)
        finally:
            await replacement.aclose()
    finally:
        release_close.set()
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await state.aclose()


async def test_sqlite_state_close_survives_caller_cancellation(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    database_path = tmp_path / "cancelled-close.db"
    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    await state.start(warm_pool_size=1)
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    original_exec_driver_sql = AsyncConnection.exec_driver_sql

    async def gated_commit(
        connection: AsyncConnection,
        statement: str,
    ) -> CursorResult[tuple[object, ...]]:
        if statement == "COMMIT" and not commit_entered.is_set():
            commit_entered.set()
            await release_commit.wait()
        return cast(
            CursorResult[tuple[object, ...]],
            await original_exec_driver_sql(connection, statement),
        )

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", gated_commit)
    closing = asyncio.create_task(state.aclose())
    try:
        await commit_entered.wait()
        closing.cancel()
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        async with aiosqlite.connect(database_path) as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*)
                FROM tinkerfin_opensandbox_workers
                WHERE namespace = ?
                """,
                ("test",),
            )
            row = await cursor.fetchone()
            worker_count = -1 if row is None else int(row[0])
        assert worker_count == 0
    finally:
        release_commit.set()
        await state.aclose()


async def test_sqlite_state_close_survives_cancellation_during_start(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    database_path = tmp_path / "cancelled-close-during-start.db"
    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    connection_closed = asyncio.Event()
    release_close = asyncio.Event()
    original_close = AsyncConnection.close

    async def gated_connection_close(connection: AsyncConnection) -> None:
        await original_close(connection)
        if not connection_closed.is_set():
            connection_closed.set()
            await release_close.wait()

    monkeypatch.setattr(AsyncConnection, "close", gated_connection_close)
    starting = asyncio.create_task(state.start(warm_pool_size=1))
    closing: asyncio.Task[None] | None = None
    try:
        await connection_closed.wait()
        closing = asyncio.create_task(state.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await starting

        async with aiosqlite.connect(database_path) as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*)
                FROM tinkerfin_opensandbox_workers
                WHERE namespace = ?
                """,
                ("test",),
            )
            row = await cursor.fetchone()
            worker_count = -1 if row is None else int(row[0])
        assert worker_count == 0
    finally:
        release_close.set()
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        if closing is not None and not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await state.aclose()


async def test_sqlite_warm_consume_requires_the_slot_clear_to_commit(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    ownership_error = _public_type("OpenSandboxStateOwnershipError")
    database_path = tmp_path / "warm-clear.db"
    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    await state.start(warm_pool_size=1)
    warm_claim = await state.claim_warm_slot()
    assert warm_claim is not None
    await state.publish_warm(warm_claim, "warm-sandbox")
    async with aiosqlite.connect(database_path) as connection:
        await connection.execute(
            """
            CREATE TRIGGER suppress_warm_slot_clear
            BEFORE UPDATE OF sandbox_id ON tinkerfin_opensandbox_warm_slots
            WHEN OLD.namespace = 'test'
              AND OLD.sandbox_id IS NOT NULL
              AND NEW.sandbox_id IS NULL
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )
        await connection.commit()

    owner_claim = await state.acquire_owner("user-A")
    try:
        with pytest.raises(ownership_error, match="Warm slot"):
            await state.consume_warm(owner_claim)
        assert await state.read_binding("user-A") is None
    finally:
        await state.release_owner(owner_claim)
        await state.aclose()


async def test_sqlite_state_rejects_removed_version_state(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "removed-version-state.db"
    url = _sqlite_url(database_path)

    seeded = state_type(engine=sql_engine(url), namespace="test")
    await seeded.start(warm_pool_size=0)
    await seeded.aclose()

    async with aiosqlite.connect(database_path) as connection:
        await connection.execute(
            """
            CREATE TABLE tinkerfin_opensandbox_schema_versions (
                component VARCHAR(64) PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        await connection.commit()

    current = state_type(engine=sql_engine(url), namespace="test")
    try:
        with pytest.raises(state_error, match="schema"):
            await current.start(warm_pool_size=0)
    finally:
        await current.aclose()


async def test_sqlite_state_rejects_an_incompatible_existing_schema(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "incompatible.db"
    async with aiosqlite.connect(database_path) as connection:
        await connection.execute(
            """
            CREATE TABLE tinkerfin_opensandbox_owners (
                namespace VARCHAR(64) NOT NULL,
                owner_digest VARCHAR(43) NOT NULL,
                PRIMARY KEY (namespace, owner_digest)
            )
            """
        )
        await connection.commit()

    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    try:
        with pytest.raises(state_error, match="schema"):
            await state.start(warm_pool_size=0)
    finally:
        await state.aclose()


async def test_sqlite_state_rejects_an_incorrect_existing_default(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "incorrect-default.db"
    seeded = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    await seeded.start(warm_pool_size=0)
    await seeded.aclose()

    async with aiosqlite.connect(database_path) as connection:
        await connection.execute(
            "ALTER TABLE tinkerfin_opensandbox_owners "
            "RENAME TO tinkerfin_opensandbox_owners_old"
        )
        await connection.execute(
            """
            CREATE TABLE tinkerfin_opensandbox_owners (
                namespace VARCHAR(64) NOT NULL,
                owner_digest VARCHAR(43) NOT NULL,
                sandbox_id VARCHAR(255),
                binding_generation BIGINT DEFAULT 7 NOT NULL,
                generation BIGINT DEFAULT 0 NOT NULL,
                claim_token VARCHAR(32),
                lease_expires_at DATETIME,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (namespace, owner_digest)
            )
            """
        )
        await connection.execute("DROP TABLE tinkerfin_opensandbox_owners_old")
        await connection.execute(
            """
            CREATE INDEX ix_tinkerfin_opensandbox_owners_lease
            ON tinkerfin_opensandbox_owners (namespace, lease_expires_at)
            """
        )
        await connection.commit()

    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    try:
        with pytest.raises(state_error, match="default"):
            await state.start(warm_pool_size=0)
    finally:
        await state.aclose()


async def test_sqlite_state_rejects_a_missing_table_from_current_schema(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    state_error = _public_type("OpenSandboxStateError")
    database_path = tmp_path / "missing-current-table.db"
    seeded = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    await seeded.start(warm_pool_size=0)
    await seeded.aclose()

    async with aiosqlite.connect(database_path) as connection:
        await connection.execute("DROP TABLE tinkerfin_opensandbox_cleanup")
        await connection.commit()

    state = state_type(engine=sql_engine(_sqlite_url(database_path)), namespace="test")
    try:
        with pytest.raises(state_error, match="missing table"):
            await state.start(warm_pool_size=0)
    finally:
        await state.aclose()


async def test_sqlite_states_serialize_the_same_owner_across_instances(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "shared.db")
    first = state_type(engine=sql_engine(url), namespace="test", poll_interval=0.01)
    second = state_type(engine=sql_engine(url), namespace="test", poll_interval=0.01)
    await asyncio.gather(
        first.start(warm_pool_size=0),
        second.start(warm_pool_size=0),
    )

    first_claim = await first.acquire_owner("user-A")
    queried = asyncio.Event()

    def query_started(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if (
            statement.startswith("SELECT")
            and "tinkerfin_opensandbox_owners" in statement
        ):
            queried.set()

    event.listen(second._engine.sync_engine, "before_cursor_execute", query_started)
    waiting = asyncio.create_task(second.acquire_owner("user-A"))
    await queried.wait()
    event.remove(second._engine.sync_engine, "before_cursor_execute", query_started)

    assert not waiting.done()
    committed = await first.bind_owner(first_claim, "sandbox-1")
    await first.release_owner(first_claim)
    second_claim = await asyncio.wait_for(waiting, timeout=1)

    assert second_claim.binding == committed
    await second.release_owner(second_claim)
    await asyncio.gather(first.aclose(), second.aclose())


async def test_sqlite_state_fences_an_expired_owner_claim(
    sql_engine: SqlEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _StateClock(monkeypatch)
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    ownership_error = _public_type("OpenSandboxStateOwnershipError")
    url = _sqlite_url(tmp_path / "fencing.db")
    first = state_type(
        engine=sql_engine(url),
        namespace="test",
        lease_ttl=0.1,
        poll_interval=0.01,
    )
    second = state_type(
        engine=sql_engine(url),
        namespace="test",
        lease_ttl=1.0,
        poll_interval=0.01,
    )
    await asyncio.gather(
        first.start(warm_pool_size=0),
        second.start(warm_pool_size=0),
    )

    stale = await first.acquire_owner("user-A")
    clock.advance(0.15)
    current = await second.acquire_owner("user-A")
    committed = await second.bind_owner(current, "sandbox-current")

    with pytest.raises(ownership_error):
        await first.bind_owner(stale, "sandbox-stale")

    await first.release_owner(stale)
    await second.release_owner(current)
    assert await first.read_binding("user-A") == committed
    await asyncio.gather(first.aclose(), second.aclose())


async def test_sqlite_state_shares_warm_slots_and_cleanup_across_instances(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    url = _sqlite_url(tmp_path / "shared-resources.db")
    first = state_type(engine=sql_engine(url), namespace="test")
    second = state_type(engine=sql_engine(url), namespace="test")
    await asyncio.gather(
        first.start(warm_pool_size=1),
        second.start(warm_pool_size=1),
    )

    warm_claim = await first.claim_warm_slot()
    assert warm_claim is not None
    assert await second.claim_warm_slot() is None
    await first.publish_warm(warm_claim, "warm-sandbox")

    owner_claim = await second.acquire_owner("user-A")
    binding = await second.consume_warm(owner_claim)
    await second.release_owner(owner_claim)
    assert binding is not None
    assert binding.sandbox_id == "warm-sandbox"
    assert await second.read_binding("user-A") == binding

    await first.enqueue_cleanup("orphan-sandbox")
    cleanup = await second.claim_cleanup()
    assert cleanup is not None
    assert cleanup.sandbox_id == "orphan-sandbox"
    await second.release_cleanup(cleanup)
    await asyncio.gather(first.aclose(), second.aclose())

    restarted = state_type(engine=sql_engine(url), namespace="test")
    await restarted.start(warm_pool_size=1)
    retry = await restarted.claim_cleanup()
    assert retry is not None
    await restarted.complete_cleanup(retry)
    assert await restarted.claim_cleanup() is None
    await restarted.aclose()


async def test_sqlite_state_rejects_conflicting_active_warm_capacity(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    configuration_error = _public_type("OpenSandboxStateConfigurationError")
    url = _sqlite_url(tmp_path / "capacity.db")
    first = state_type(engine=sql_engine(url), namespace="test")
    conflicting = state_type(engine=sql_engine(url), namespace="test")
    await first.start(warm_pool_size=1)

    with pytest.raises(configuration_error):
        await conflicting.start(warm_pool_size=2)

    await first.aclose()
    await conflicting.start(warm_pool_size=2)
    await conflicting.aclose()


async def test_sqlite_state_renews_owner_and_warm_claims(
    sql_engine: SqlEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _StateClock(monkeypatch)
    state_type = _public_type("SQLAlchemyOpenSandboxState")
    lease_ttl = 2.0
    state = state_type(
        engine=sql_engine(_sqlite_url(tmp_path / "renew.db")),
        namespace="test",
        lease_ttl=lease_ttl,
    )
    await state.start(warm_pool_size=1)
    try:
        assert state.persistent is True
        assert state.lease_renew_interval == pytest.approx(lease_ttl / 3)

        await state.enqueue_cleanup("orphan-sandbox")
        owner = await state.acquire_owner("user-A")
        warm = await state.claim_warm_slot()
        assert warm is not None
        cleanup = await state.claim_cleanup()
        assert cleanup is not None
        for _ in range(3):
            clock.advance(lease_ttl / 2)
            assert await state.renew_owner(owner) is True
            assert await state.renew_warm(warm) is True
            assert await state.renew_cleanup(cleanup) is True

        assert await state.claim_cleanup() is None
        binding = await state.bind_owner(owner, "bound-sandbox")
        await state.publish_warm(warm, "warm-sandbox")
        await state.complete_cleanup(cleanup)
        await state.release_owner(owner)

        assert binding.sandbox_id == "bound-sandbox"
        assert await state.shutdown_sandbox_ids() == ()
    finally:
        await state.aclose()


async def test_memory_state_reports_process_owned_shutdown_resources() -> None:
    state_type = _public_type("InMemoryOpenSandboxState")
    state = state_type()
    await state.start(warm_pool_size=1)

    assert state.persistent is False
    assert state.lease_renew_interval is None

    owner = await state.acquire_owner("user-A")
    await state.bind_owner(owner, "bound-sandbox")
    assert await state.renew_owner(owner) is True
    await state.release_owner(owner)
    warm = await state.claim_warm_slot()
    assert warm is not None
    assert await state.renew_warm(warm) is True
    await state.publish_warm(warm, "warm-sandbox")
    await state.enqueue_cleanup("orphan-sandbox")
    cleanup = await state.claim_cleanup()
    assert cleanup is not None
    assert await state.renew_cleanup(cleanup) is True
    await state.release_cleanup(cleanup)

    assert set(await state.shutdown_sandbox_ids()) == {
        "bound-sandbox",
        "warm-sandbox",
        "orphan-sandbox",
    }
    await state.aclose()
