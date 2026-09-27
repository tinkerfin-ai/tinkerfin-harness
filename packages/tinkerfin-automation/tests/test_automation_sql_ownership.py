"""Accepted Automation writes settle atomically before their callers exit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any, TypeVar
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from test_automation_sqlite_store import _taskless_execution
from tests.support.sql_faults import after_sql_commit

from tinkerfin_automation import (
    AutomationExecution,
    AutomationStoreError,
    SqlAlchemyAutomationStore,
)
from tinkerfin_automation.sql_schema import metadata

_T = TypeVar("_T")


class DriverFailure(SQLAlchemyError):
    """A controlled failure after a real database statement."""


class ProcessControl(BaseException):
    """A control outcome that must reach the operation's caller."""


async def _capture(operation: Awaitable[_T]) -> _T | BaseException:
    try:
        return await operation
    except BaseException as error:  # noqa: BLE001 - observe the owning task's result
        return error


def _contains(root: BaseException, target: BaseException) -> bool:
    pending = [root]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if error is target:
            return True
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        cause = vars(error).get("cause")
        if isinstance(cause, BaseException):
            pending.append(cause)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return False


async def _enqueue(
    store: SqlAlchemyAutomationStore, execution: AutomationExecution
) -> AutomationExecution:
    return await store.enqueue_execution(
        execution, occurrence_key="once", request_id="command", input_digest="input"
    )


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("failure_type", [None, DriverFailure, OSError, ProcessControl])
async def test_write_close_and_cancellation_preserve_atomicity(
    automation_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    failure_type: type[BaseException] | None,
) -> None:
    engine = automation_sql_engine
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    execution = _taskless_execution(
        await store.current_time(), execution_id=str(uuid4())
    )
    entered, release = asyncio.Event(), asyncio.Event()
    cause = RuntimeError("original driver cause")
    failure = failure_type("statement outcome") if failure_type is not None else None
    if failure is not None:
        failure.__cause__ = cause
    original_execute = AsyncConnection.execute

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        # SQLAlchemy's untyped execution boundary is passed through unchanged.
        result = await original_execute(connection, statement, *args, **kwargs)
        if connection.engine is engine and str(statement).startswith(
            "INSERT INTO tinkerfin_automation_work_items"
        ):
            entered.set()
            await release.wait()
            if failure is not None:
                raise failure
        return result

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    operation = asyncio.create_task(_capture(_enqueue(store, execution)))
    closer: asyncio.Task[None | BaseException] | None = None
    try:
        await entered.wait()
        if cancel:
            operation.cancel("caller stopped")
        closer = asyncio.create_task(_capture(store.close()))
        # The queued marker runs after close admission, without a timed sleep.
        admitted = asyncio.Event()
        asyncio.get_running_loop().call_soon(admitted.set)
        await admitted.wait()
        with pytest.raises(AutomationStoreError, match="closed"):
            await store.current_time()
        assert not closer.done()
        if cancel:
            operation.cancel("caller stopped again")
            closer.cancel("close waiter stopped")
        release.set()
        result = await operation
        close_result = await closer
        if isinstance(failure, ProcessControl):
            assert result is failure
        elif cancel:
            assert isinstance(result, asyncio.CancelledError)
        elif failure is not None:
            assert isinstance(result, AutomationStoreError)
        else:
            assert result == execution
        if failure is not None:
            assert isinstance(result, BaseException)
            assert _contains(result, failure)
            assert _contains(result, cause)
        if cancel:
            assert isinstance(close_result, asyncio.CancelledError)
        else:
            assert close_result is None
        async with engine.connect() as connection:
            rows = {
                table.name: (await connection.execute(select(table))).all()
                for table in metadata.sorted_tables
            }
            assert await connection.scalar(select(1)) == 1
        if cancel or failure is not None:
            assert all(not values for values in rows.values())
        else:
            assert len(rows["tinkerfin_automation_runs"]) == 1
            assert len(rows["tinkerfin_automation_work_items"]) == 1
            assert len(rows["tinkerfin_automation_operations"]) == 1
    finally:
        release.set()
        await operation
        if closer is not None:
            await closer
        await store.close()


@pytest.mark.parametrize("failure_type", [DriverFailure, OSError])
async def test_lost_commit_acknowledgement_is_not_replayed(
    automation_sql_engine: AsyncEngine,
    failure_type: type[Exception],
) -> None:
    engine = automation_sql_engine
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    execution = _taskless_execution(
        await store.current_time(), execution_id=str(uuid4())
    )
    attempts = 0
    failure = failure_type("commit acknowledgement lost")

    async def fail_acknowledgement() -> None:
        nonlocal attempts
        attempts += 1
        raise failure

    try:
        with after_sql_commit(engine, fail_acknowledgement):
            with pytest.raises(AutomationStoreError) as caught:
                await _enqueue(store, execution)
        assert _contains(caught.value, failure)
        assert attempts == 1
        repeated = await _enqueue(store, replace(execution, execution_id=str(uuid4())))
        assert repeated == execution
        page = await store.list_executions(
            "app", "owner-1", task_id=None, limit=100, cursor=None
        )
        assert page.items == (execution,)
    finally:
        await store.close()
