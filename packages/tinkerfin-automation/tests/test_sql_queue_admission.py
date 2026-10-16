"""Concurrent queue admission preserves capacity and immutable command results."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sql_test_support import wait_for_lock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from test_automation_sqlite_store import _execution, _task

from tinkerfin_automation import (
    AutomationExecution,
    ExecutionLimits,
    ExecutionOrigin,
    QueueFullError,
    RequestConflictError,
    SqlAlchemyAutomationStore,
)
from tinkerfin_contracts import RunIdentity


@pytest.mark.parametrize(
    "case",
    [
        "taskless",
        "normal",
        "retry",
        "same_command",
        "same_occurrence",
        "conflicting_command",
    ],
)
async def test_waiting_enqueue_observes_capacity_and_committed_identity(
    automation_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite uses one database write transaction instead of row locks")
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    now = await store.current_time()
    task = replace(_task(now), limits=ExecutionLimits(max_queued_runs=1))
    if case in {"normal", "retry"}:
        await store.create_task(task, request_id=None, input_digest="task")
    first = _execution(task, now, execution_id=str(uuid4()))
    if case == "retry":
        first = replace(
            first, origin=ExecutionOrigin.RETRY, retry_of="previous", attempt=2
        )
    elif case != "normal":
        first = replace(first, task_id=None, origin=ExecutionOrigin.ONE_TIME)
    second = replace(
        first,
        execution_id=str(uuid4()),
        identity=RunIdentity(
            namespace=first.namespace, thread_id="second-thread", run_id="second-run"
        ),
    )
    marker = ContextVar("queue_test_contender", default="")
    counted, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_connection: list[int] = []
    original_scalar, original_execute = AsyncConnection.scalar, AsyncConnection.execute

    async def scalar(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original_scalar(connection, statement, *args, **kwargs)
        if (
            connection.engine is engine
            and marker.get() == "first"
            and "count(" in str(statement).lower()
        ):
            counted.set()
            await release.wait()
        return result

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if (
            connection.engine is engine
            and marker.get() == "second"
            and not second_entered.is_set()
        ):
            command = (
                "SELECT pg_backend_pid()"
                if engine.dialect.name == "postgresql"
                else "SELECT CONNECTION_ID()"
            )
            identity = (await original_execute(connection, text(command))).scalar_one()
            assert isinstance(identity, int)
            second_connection.append(identity)
            second_entered.set()
        return await original_execute(connection, statement, *args, **kwargs)

    async def enqueue(execution: AutomationExecution, contender: str) -> object:
        token = marker.set(contender)
        shared_command = case in {"same_command", "conflicting_command"}
        try:
            return await store.enqueue_execution(
                execution,
                occurrence_key="shared"
                if case == "same_occurrence"
                else execution.execution_id,
                request_id="shared" if shared_command else None,
                input_digest=contender if case == "conflicting_command" else "input",
            )
        except (QueueFullError, RequestConflictError) as error:
            return error
        finally:
            marker.reset(token)

    pending: list[asyncio.Task[object]] = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "scalar", scalar)
            patch.setattr(AsyncConnection, "execute", execute)
            pending.append(asyncio.create_task(enqueue(first, "first")))
            async with asyncio.timeout(10):
                await counted.wait()
                pending.append(asyncio.create_task(enqueue(second, "second")))
                await second_entered.wait()
                await wait_for_lock(engine, second_connection[0], pending[1])
                release.set()
                results = await asyncio.gather(*pending)
        assert results[0] == first
        if case in {"same_command", "same_occurrence"}:
            assert results[1] == first
        elif case == "conflicting_command":
            assert isinstance(results[1], RequestConflictError)
        else:
            assert isinstance(results[1], QueueFullError)
        page = await store.list_executions(
            "app", "owner-1", task_id=None, limit=10, cursor=None
        )
        assert page.items == (first,)
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await store.close()
