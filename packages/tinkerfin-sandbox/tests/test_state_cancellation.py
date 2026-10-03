"""Repeated caller cancellation must settle real SQLite driver resources."""

from __future__ import annotations

import asyncio
import gc
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal

import aiosqlite
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_sandbox import SQLAlchemyOpenSandboxState


def _hold_writer(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=0.1, check_same_thread=False)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def _release_writer(connection: sqlite3.Connection) -> None:
    connection.rollback()
    connection.close()


def _check_database(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=0.1)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("cancellations", [1, 2, 3])
async def test_repeated_cancellation_during_sqlite_lock_wait_settles_resources(
    tmp_path: Path, cancellations: int
) -> None:
    path = tmp_path / "state.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    state = SQLAlchemyOpenSandboxState(engine=engine, namespace="cancel-test")
    initial_tasks = set(asyncio.all_tasks())
    reached = asyncio.Event()

    def before_execute(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if statement == "BEGIN IMMEDIATE":
            reached.set()

    operation: asyncio.Task[object] | None = None
    blocker: sqlite3.Connection | None = None
    try:
        await state.start(warm_pool_size=0)
        event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
        blocker = await asyncio.to_thread(_hold_writer, path)
        operation = asyncio.create_task(state.acquire_owner("owner"))
        await reached.wait()
        for number in range(cancellations):
            operation.cancel(f"caller cancellation {number}")
            await asyncio.sleep(0)
        await asyncio.to_thread(_release_writer, blocker)
        blocker = None
        with pytest.raises(asyncio.CancelledError):
            await operation
        await state.aclose()
        await asyncio.sleep(0)
        assert not [
            task for task in asyncio.all_tasks() - initial_tasks if not task.done()
        ]
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
        await asyncio.to_thread(_check_database, path)
    finally:
        if blocker is not None:
            await asyncio.to_thread(_release_writer, blocker)
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await state.aclose()
        await engine.dispose()


async def test_cancel_during_pool_return_preserves_borrowed_engine_capacity(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    state = SQLAlchemyOpenSandboxState(engine=engine, namespace="pool-cancel")
    operation: asyncio.Task[object] | None = None
    reset_seen = asyncio.Event()
    loop = asyncio.get_running_loop()

    def reset(_connection: object, _record: object, _reset_state: object) -> None:
        if operation is not None and not reset_seen.is_set():
            reset_seen.set()
            loop.call_soon(operation.cancel, "cancel during pool return")

    try:
        await state.start(warm_pool_size=0)
        event.listen(engine.sync_engine, "reset", reset)
        operation = asyncio.create_task(state.acquire_owner("first"))
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert reset_seen.is_set()
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        second = await state.acquire_owner("second")
        await state.release_owner(second)
        await state.aclose()
        assert engine.pool.checkedout() == 0
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        event.remove(engine.sync_engine, "reset", reset)
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await state.aclose()
        await engine.dispose()


def _owner_snapshot(path: Path, owner_digest: str) -> tuple[object, ...]:
    connection = sqlite3.connect(path, timeout=0.2)
    try:
        row = connection.execute(
            "SELECT sandbox_id, binding_generation, generation, claim_token "
            "FROM tinkerfin_opensandbox_owners WHERE owner_digest = ?",
            (owner_digest,),
        ).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        connection.close()


def _commit_external_write(path: Path, marker: int) -> None:
    """A live SELECT can permit BEGIN and INSERT while still blocking COMMIT."""
    connection = sqlite3.connect(path, timeout=0.2)
    try:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO cancellation_probe (marker) VALUES (?)", (marker,)
        )
        connection.commit()
        assert connection.execute(
            "SELECT marker FROM cancellation_probe WHERE marker = ?", (marker,)
        ).fetchone() == (marker,)
    finally:
        connection.close()


class _ExecutedStatementGate:
    """Pause a real driver result before SQLAlchemy can consume its cursor rows."""

    def __init__(self, statement_prefix: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self._statement_prefix = statement_prefix
        self._monkeypatch = monkeypatch
        self.armed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def connect(self, connection: object, _record: object) -> None:
        assert isinstance(connection, AdaptedConnection)
        connection.run_async(self._install)

    async def _install(self, driver: object) -> None:
        assert isinstance(driver, aiosqlite.Connection)
        original_execute = driver._execute

        async def execute(
            operation: Callable[..., object], *args: object, **kwargs: object
        ) -> object:
            # The real sqlite3 operation has completed in aiosqlite's worker.
            # For SELECT this precedes SQLAlchemy's separate fetchall call, as
            # specified by AsyncAdapt_aiosqlite_cursor.execute in SQLAlchemy 2.0.52.
            result = await original_execute(operation, *args, **kwargs)
            if (
                self.armed
                and args
                and isinstance(args[0], str)
                and args[0].startswith(self._statement_prefix)
            ):
                self.armed = False
                self.reached.set()
                await self.release.wait()
            return result

        self._monkeypatch.setattr(driver, "_execute", execute)


@pytest.mark.parametrize("cancellations", [1, 2])
@pytest.mark.parametrize(
    "operation_name",
    ["bind_owner", "acquire_owner", "read_availability", "get_holder_updates"],
)
async def test_cancel_after_sqlite_execute_rolls_back_writes_and_releases_read_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: Literal[
        "bind_owner", "acquire_owner", "read_availability", "get_holder_updates"
    ],
    cancellations: int,
) -> None:
    """Retained cancellation tracebacks cannot keep a reader or a write alive."""
    path = tmp_path / "executed-cursor.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.3,
    )
    borrowed_pool = engine.pool
    state = SQLAlchemyOpenSandboxState(engine=engine, namespace="executed-cursor")
    prefix = (
        "UPDATE tinkerfin_opensandbox_owners SET"
        if operation_name == "bind_owner"
        else "SELECT tinkerfin_opensandbox_owners."
        if operation_name == "acquire_owner"
        else "SELECT tinkerfin_opensandbox_availability."
    )
    gate = _ExecutedStatementGate(prefix, monkeypatch)
    event.listen(engine.sync_engine, "connect", gate.connect)
    initial_tasks = set(asyncio.all_tasks())
    operation: asyncio.Task[object] | None = None
    gc_was_enabled = gc.isenabled()
    try:
        await state.start(warm_pool_size=0)
        async with engine.begin() as connection:
            assert (
                await connection.scalar(text("PRAGMA journal_mode=DELETE")) == "delete"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE cancellation_probe (marker INTEGER PRIMARY KEY)"
            )
        seed = await state.acquire_owner("owner")
        binding = await state.bind_owner(seed, "original-sandbox", purpose="commands")
        availability = await state.register_holder(seed, "manager")
        await state.release_owner(seed)
        claim = (
            await state.acquire_owner("owner")
            if operation_name == "bind_owner"
            else None
        )
        before = await asyncio.to_thread(_owner_snapshot, path, seed.owner_digest)

        # Keep both the CancelledError and its traceback alive through independent
        # commits, pool reuse, and State shutdown. No garbage collection or frame
        # clearing may be needed to finalize a SQLite cursor.
        gc.disable()
        gate.armed = True
        if operation_name == "bind_owner":
            assert claim is not None
            operation = asyncio.create_task(
                state.bind_owner(claim, "cancelled-sandbox", purpose="commands")
            )
        elif operation_name == "acquire_owner":
            operation = asyncio.create_task(state.acquire_owner("owner"))
        elif operation_name == "read_availability":
            operation = asyncio.create_task(state.read_availability("owner"))
        else:
            operation = asyncio.create_task(state.get_holder_updates("manager"))
        await gate.reached.wait()
        for number in range(cancellations):
            operation.cancel(f"{operation_name} cancellation {number}")
            await asyncio.sleep(0)
        # Cancellation is a request to settle the active driver operation. The
        # test releases its own gate; it never waits for driver cancellation.
        gate.release.set()
        retained: asyncio.CancelledError | None = None
        try:
            await operation
        except asyncio.CancelledError as error:
            retained = error
        assert retained is not None
        assert retained.args == (f"{operation_name} cancellation 0",)
        assert retained.__traceback__ is not None
        assert not gc.isenabled()

        await asyncio.to_thread(_commit_external_write, path, 1)
        assert (
            await asyncio.to_thread(_owner_snapshot, path, seed.owner_digest) == before
        )
        assert await state.read_binding("owner") == binding
        assert await state.read_availability("owner") == availability
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        if claim is not None:
            await state.release_owner(claim)
        successor = await state.acquire_owner("owner")
        await state.release_owner(successor)
        await state.aclose()
        assert engine.pool is borrowed_pool
        assert engine.pool.checkedout() == 0
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT COUNT(*) FROM cancellation_probe"))
                == 1
            )
        await asyncio.to_thread(_commit_external_write, path, 2)
        await asyncio.sleep(0)
        assert not [
            task for task in asyncio.all_tasks() - initial_tasks if not task.done()
        ]
        assert retained.__traceback__ is not None
        assert retained.args == (f"{operation_name} cancellation 0",)
        assert not gc.isenabled()
    finally:
        gate.release.set()
        try:
            if operation is not None and not operation.done():
                operation.cancel("test cleanup")
                await asyncio.gather(operation, return_exceptions=True)
        finally:
            try:
                await state.aclose()
            finally:
                try:
                    event.remove(engine.sync_engine, "connect", gate.connect)
                    await engine.dispose()
                finally:
                    if gc_was_enabled:
                        gc.enable()
