"""Public Store lifecycle owners retain every accepted task outcome."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Literal, TypeGuard

import pytest
from sqlalchemy import event, update
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

import tinkerfin_tracing.durable_store as store_module
from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    RunFact,
    SqlAlchemyTraceStore,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.backend import TraceLedgerChange, TraceLedgerCommitResult
from tinkerfin_tracing.sql_schema import writers
from tinkerfin_tracing.store import TraceWriter


class _ProcessStop(BaseException):
    pass


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


def _contains(error: BaseException | None, target: BaseException) -> bool:
    pending = [] if error is None else [error]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if item is target:
            return True
        if id(item) in seen:
            continue
        seen.add(id(item))
        pending.extend(
            value for value in (item.__cause__, item.__context__) if value is not None
        )
        if _is_group(item):
            pending.extend(item.exceptions)
        cause = vars(item).get("cause")
        if isinstance(cause, BaseException):
            pending.append(cause)
    return False


@pytest.mark.parametrize("kind", ["driver", "control"])
async def test_public_setup_shares_failure_with_a_cancelled_waiter(
    trace_sql_engine: AsyncEngine, kind: Literal["driver", "control"]
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = (
        SQLAlchemyError("schema driver failed")
        if kind == "driver"
        else _ProcessStop("schema control")
    )
    cause = OSError("schema original cause")
    failure.__cause__ = cause
    injected = False

    def fail_create(
        connection: Connection, _cursor: Any, statement: str, *_args: Any
    ) -> None:
        nonlocal injected
        if injected or not statement.strip().startswith(
            "CREATE TABLE tinkerfin_trace_"
        ):
            return
        injected = True
        adapted = connection.connection.dbapi_connection
        assert isinstance(adapted, AdaptedConnection)

        async def wait(_driver: Any) -> None:
            entered.set()
            await release.wait()
            raise failure

        adapted.run_async(wait)

    async def attempt() -> BaseException | None:
        try:
            await store.setup()
        except BaseException as error:  # noqa: BLE001 - observe control in the caller, not outside asyncio
            return error
        return None

    event.listen(trace_sql_engine.sync_engine, "before_cursor_execute", fail_create)
    first = asyncio.create_task(attempt())
    second: asyncio.Task[BaseException | None] | None = None
    try:
        await entered.wait()
        second = asyncio.create_task(attempt())
        first.cancel("setup caller cancelled")
        release.set()
        first_error, second_error = await asyncio.gather(first, second)
        assert _contains(first_error, failure) and _contains(first_error, cause)
        assert _contains(second_error, failure) and _contains(second_error, cause)
        if kind == "driver":
            assert isinstance(first_error, asyncio.CancelledError)
            assert isinstance(second_error, TraceStoreError)
        else:
            assert first_error is second_error is failure
    finally:
        release.set()
        await asyncio.gather(
            first, *(() if second is None else (second,)), return_exceptions=True
        )
        event.remove(trace_sql_engine.sync_engine, "before_cursor_execute", fail_create)
    # Failure happened before any DDL. A later explicit call can prepare storage.
    await store.setup()


@pytest.mark.parametrize("kind", ["driver", "control"])
async def test_public_writer_close_shares_failure_with_a_cancelled_waiter(
    trace_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["driver", "control"],
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    writer = await store.open_writer(
        RunIdentity(namespace="default", thread_id="close", run_id="writer")
    )
    entered, release = asyncio.Event(), asyncio.Event()
    failure = (
        TraceStoreError("writer close failed")
        if kind == "driver"
        else _ProcessStop("writer close control")
    )
    cause = OSError("close original cause")
    failure.__cause__ = cause
    original = store.backend.commit_ledger_change
    closes = 0

    async def fail_close(change: TraceLedgerChange):
        nonlocal closes
        if change.kind == "close_writer":
            closes += 1
            entered.set()
            await release.wait()
            await original(change)
            raise failure
        return await original(change)

    async def attempt() -> BaseException | None:
        try:
            await writer.aclose()
        except BaseException as error:  # noqa: BLE001 - observe the original control object in its owning caller
            return error
        return None

    with monkeypatch.context() as patch:
        patch.setattr(store.backend, "commit_ledger_change", fail_close)
        first = asyncio.create_task(attempt())
        second: asyncio.Task[BaseException | None] | None = None
        try:
            await entered.wait()
            second = asyncio.create_task(attempt())
            first.cancel("close caller cancelled")
            release.set()
            first_error, second_error = await asyncio.gather(first, second)
            assert closes == 1
            assert _contains(first_error, failure) and _contains(first_error, cause)
            assert _contains(second_error, failure) and _contains(second_error, cause)
            if kind == "control":
                assert first_error is second_error is failure
            else:
                assert isinstance(first_error, asyncio.CancelledError)
                assert second_error is failure
        finally:
            release.set()
            await asyncio.gather(
                first, *(() if second is None else (second,)), return_exceptions=True
            )
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(ThreadIdentity(namespace="default", thread_id="close"))


async def test_successful_stale_close_preserves_the_replacement_writer(
    trace_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    ticks: list[asyncio.Event] = []
    waiting, failed = asyncio.Event(), asyncio.Event()
    controlled = ModuleType("controlled_heartbeat_clock")
    controlled.__dict__.update(vars(asyncio))

    async def tick(_seconds: float) -> None:
        advance = asyncio.Event()
        ticks.append(advance)
        waiting.set()
        await advance.wait()

    setattr(controlled, "sleep", tick)
    monkeypatch.setattr(store_module, "asyncio", controlled)
    original = store.backend.commit_ledger_change

    async def commit(change: TraceLedgerChange) -> TraceLedgerCommitResult:
        try:
            return await original(change)
        except TraceStoreProtocolError:
            if change.kind == "renew_writer":
                failed.set()
            raise

    monkeypatch.setattr(store.backend, "commit_ledger_change", commit)
    identity = RunIdentity(namespace="stale", thread_id="same", run_id="run")
    old = await store.open_writer(identity)
    replacement: TraceWriter | None = None
    try:
        await old.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id="start",
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=1,
                    phase="started",
                    input_kind="ordinary",
                ),
            )
        )
        await waiting.wait()
        # Move the persisted deadline instead of depending on a real-time delay.
        async with trace_sql_engine.begin() as connection:
            await connection.execute(
                update(writers).values(lease_expires_at=datetime(2000, 1, 1))
            )
        replacement = await store.open_writer(identity)
        for advance in tuple(ticks):
            advance.set()
        await failed.wait()
        await old.aclose()
        snapshot = await store.snapshot(identity.thread)
        assert snapshot.active_run_ids == (identity.run_id,)
        await replacement.append(
            (
                RunFact(
                    identity=identity,
                    source_observation_id="terminal",
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=2,
                    phase="terminal",
                    outcome="succeeded",
                ),
            ),
            mandatory=True,
        )
        assert (await store.snapshot(identity.thread)).as_of_seq == 2
    finally:
        await old.aclose()
        if replacement is not None:
            await replacement.aclose()
