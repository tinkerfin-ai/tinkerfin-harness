"""Command identity wins over revisions after concurrent task-row contention."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sql_test_support import control_database_clock
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tinkerfin_automation import (
    AutomationTask,
    RequestConflictError,
    Schedule,
    SqlAlchemyAutomationStore,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.sql_schema import tasks


@pytest.mark.parametrize("different_input", [False, True])
async def test_concurrent_update_checks_committed_receipt_after_waiting_for_task(
    automation_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    different_input: bool,
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite does not provide the task-row lock used by this contract")
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    control_database_clock(engine, clock, monkeypatch)
    store = SqlAlchemyAutomationStore(engine)
    pending: list[asyncio.Task[AutomationTask]] = []
    try:
        async with (
            AutomationService(namespace="scheduler", store=store, clock=clock) as first,
            AutomationService(
                namespace="scheduler", store=store, clock=clock
            ) as second,
        ):
            task = await first.create_task(
                owner_id="owner",
                name="Report",
                target="report",
                execution_namespace="runtime",
                schedule=Schedule.once(at=clock.now() + timedelta(days=1)),
            )
            execute = AsyncConnection.execute
            contending = asyncio.Event()
            connections: set[AsyncConnection] = set()

            async def observe(
                connection: AsyncConnection,
                statement: Any,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                if (
                    connection.engine is engine
                    and isinstance(statement, Select)
                    and tasks in statement.get_final_froms()
                    and "FOR UPDATE" in str(statement)
                ):
                    connections.add(connection)
                    if len(connections) == 2:
                        contending.set()
                return await execute(connection, statement, *args, **kwargs)

            # Hold the real row until both updates attempt to lock it. This does
            # not require a particular receipt-query order or natural scheduling.
            async with engine.begin() as holder:
                await holder.execute(
                    select(tasks.c.task_id)
                    .where(tasks.c.task_id == task.task_id)
                    .with_for_update()
                )
                monkeypatch.setattr(AsyncConnection, "execute", observe)
                for service, name in [
                    (first, "Updated"),
                    (second, "Different" if different_input else "Updated"),
                ]:
                    pending.append(
                        asyncio.create_task(
                            service.update_task(
                                owner_id="owner",
                                task_id=task.task_id,
                                expected_revision=1,
                                name=name,
                                request_id="same-command",
                            )
                        )
                    )
                await contending.wait()
            results = await asyncio.gather(*pending, return_exceptions=True)
            successes = [
                value for value in results if isinstance(value, AutomationTask)
            ]
            if different_input:
                assert len(successes) == 1
                assert (
                    sum(isinstance(value, RequestConflictError) for value in results)
                    == 1
                )
            else:
                assert len(successes) == 2
                assert successes[0] == successes[1]
            saved = await first.get_task(owner_id="owner", task_id=task.task_id)
            assert saved == successes[0]
            assert saved.revision == 2
    finally:
        for operation in pending:
            if not operation.done():
                operation.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await store.close()
