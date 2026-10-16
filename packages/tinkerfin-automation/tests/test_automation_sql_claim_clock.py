"""Lease and queue decisions stay valid after database contention."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sql_test_support import control_database_clock, wait_for_lock
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from test_automation_sqlite_store import _taskless_execution

from tinkerfin_automation import (
    AutomationExecution,
    ClaimLostError,
    ExecutionLimits,
    ExecutionStatus,
    SqlAlchemyAutomationStore,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.sql_schema import runs, scopes, work_items
from tinkerfin_automation.store import WorkClaimBatch, WorkItemClaim
from tinkerfin_contracts import RunIdentity


async def _enqueue(
    store: SqlAlchemyAutomationStore, execution: AutomationExecution
) -> None:
    await store.enqueue_execution(
        execution,
        occurrence_key=execution.execution_id,
        request_id=None,
        input_digest=execution.execution_id,
    )


@pytest.mark.parametrize(
    ("deadline", "limit"),
    [("lease", 1), ("lease", 0), ("ready", 1), ("queue", 0), ("interrupt", 0)],
)
async def test_claim_batch_reports_deadline_crossed_during_database_read(
    automation_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    deadline: str,
    limit: int,
) -> None:
    engine = automation_sql_engine
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    control_database_clock(engine, clock, monkeypatch)
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    execution = _taskless_execution(clock.now(), execution_id=str(uuid4()))
    if deadline == "ready":
        execution = replace(execution, queued_at=clock.now() + timedelta(seconds=1))
    elif deadline == "queue":
        execution = replace(
            execution, queue_deadline=clock.now() + timedelta(seconds=1)
        )

    async def claim(capacity: int = limit) -> WorkClaimBatch:
        return await store.claim_work(
            "app",
            "worker",
            limit=capacity,
            lease_duration=timedelta(seconds=1),
            global_concurrency=1,
        )

    try:
        await _enqueue(store, execution)
        if deadline in {"lease", "interrupt"}:
            (initial,) = (await claim(1)).claims
            if deadline == "interrupt":
                await store.authorize_start(
                    initial, execution_timeout=timedelta(seconds=1)
                )
                await store.mark_interrupted(initial, interrupt_ids=("review",))

        original_scalar = AsyncConnection.scalar
        advance_after_observation = True

        async def scalar(
            connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal advance_after_observation
            value = await original_scalar(connection, statement, *args, **kwargs)
            if (
                connection.engine is engine
                and isinstance(value, datetime)
                and advance_after_observation
            ):
                # The transaction starts before the deadline and finishes after it.
                # Domain reads and row locks still run against the real database.
                advance_after_observation = False
                await clock.advance(timedelta(seconds=2))
            return value

        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "scalar", scalar)
            batch = await claim()
        assert batch.claims == ()
        assert batch.next_check_after_seconds == 0
        if deadline in {"lease", "ready"}:
            assert len((await claim(1)).claims) == 1
        else:
            assert (await claim()).claims == ()
            current = await store.get_execution(
                "app", "owner-1", execution.execution_id
            )
            assert current.status is ExecutionStatus.TIMED_OUT
    finally:
        await store.close()


async def test_claim_batch_bounds_recheck_for_locked_expired_lease(
    automation_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite has no SKIP LOCKED row ownership")
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    control_database_clock(engine, clock, monkeypatch)
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    execution = _taskless_execution(clock.now(), execution_id=str(uuid4()))

    async def claim() -> WorkClaimBatch:
        return await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(seconds=1),
            global_concurrency=1,
        )

    try:
        await _enqueue(store, execution)
        (initial,) = (await claim()).claims
        await clock.advance(timedelta(seconds=2))
        async with engine.begin() as blocker:
            await blocker.execute(
                select(work_items)
                .where(work_items.c.work_item_id == initial.work_item_id)
                .with_for_update()
            )
            batch = await claim()
            assert batch.claims == ()
            assert batch.next_check_after_seconds == 1
        assert len((await claim()).claims) == 1
    finally:
        await store.close()


@pytest.mark.parametrize(
    "case",
    [
        "renew_work",
        "authorize_work",
        "authorize_run",
        "finish_run",
        "interrupt_run",
        "new_run",
        "new_scope",
        "new_batch",
        "new_batch_timeout",
    ],
)
async def test_lease_decision_after_real_row_lock_wait(
    automation_sql_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    engine = automation_sql_engine
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite serializes writes at BEGIN IMMEDIATE, without row locks")
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    control_database_clock(engine, clock, monkeypatch)
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    execution = _taskless_execution(
        clock.now(),
        execution_id=str(uuid4()),
        limits=ExecutionLimits(max_concurrent_runs=2, max_queued_runs=3),
    )
    if case == "new_batch_timeout":
        execution = replace(
            execution, queue_deadline=clock.now() + timedelta(minutes=1)
        )
    await _enqueue(store, execution)

    async def claim(limit: int = 1) -> tuple[WorkItemClaim, ...]:
        return (
            await store.claim_work(
                "app",
                "worker",
                limit=limit,
                lease_duration=timedelta(minutes=1),
                global_concurrency=2,
            )
        ).claims

    if case == "new_scope":
        (initial,) = await claim()
        await store.finish_execution(initial, status=ExecutionStatus.SUCCEEDED)
        execution = replace(
            execution,
            execution_id=str(uuid4()),
            identity=RunIdentity(
                namespace=execution.namespace,
                thread_id="second-thread",
                run_id="second-run",
            ),
        )
        await _enqueue(store, execution)
    second: AutomationExecution | None = None
    if case.startswith("new_batch"):
        second = replace(
            execution,
            execution_id=str(uuid4()),
            identity=RunIdentity(
                namespace=execution.namespace,
                thread_id="second-thread",
                run_id="second-run",
            ),
            queued_at=clock.now() + timedelta(microseconds=1),
        )
        await _enqueue(store, second)
        await clock.advance(timedelta(seconds=1))
    existing: WorkItemClaim | None = None
    if not case.startswith("new_"):
        (existing,) = await claim()
        if case.startswith(("finish", "interrupt")):
            await store.authorize_start(existing, execution_timeout=timedelta(hours=1))
    if case == "renew_work":
        assert existing is not None
        operation: Awaitable[object] = store.renew_claim(
            existing, lease_duration=timedelta(minutes=1)
        )
    elif case.startswith("authorize"):
        assert existing is not None
        operation = store.authorize_start(
            existing, execution_timeout=timedelta(hours=1)
        )
    elif case == "finish_run":
        assert existing is not None
        operation = store.finish_execution(existing, status=ExecutionStatus.SUCCEEDED)
    elif case == "interrupt_run":
        assert existing is not None
        operation = store.mark_interrupted(existing, interrupt_ids=("review",))
    else:
        operation = claim(2 if second is not None else 1)
    if case.endswith("work"):
        assert existing is not None
        table = work_items
        condition = work_items.c.work_item_id == existing.work_item_id
    elif case == "new_scope":
        table = scopes
        condition = scopes.c.kind == "global"
    else:
        table = runs
        condition = runs.c.execution_id == (second or execution).execution_id
    blocker = await engine.connect()
    await blocker.begin()
    await blocker.execute(select(table).where(condition).with_for_update())
    entered = asyncio.Event()
    identities: list[int] = []
    original_execute = AsyncConnection.execute

    async def execute(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if (
            connection.engine is engine
            and not entered.is_set()
            and table.name in str(statement)
        ):
            command = (
                "SELECT pg_backend_pid()"
                if engine.dialect.name == "postgresql"
                else "SELECT CONNECTION_ID()"
            )
            identity = (await original_execute(connection, text(command))).scalar_one()
            assert isinstance(identity, int)
            identities.append(identity)
            entered.set()
        return await original_execute(connection, statement, *args, **kwargs)

    async def invoke() -> object:
        return await operation

    pending: asyncio.Task[object] | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", execute)
            pending = asyncio.create_task(invoke())
            async with asyncio.timeout(10):
                await entered.wait()
                await wait_for_lock(engine, identities[0], pending)
                await clock.advance(timedelta(minutes=2))
                await blocker.rollback()
                if not case.startswith("new_"):
                    with pytest.raises(ClaimLostError):
                        await pending
                else:
                    result = await pending
                    assert isinstance(result, tuple)
                    if case == "new_batch_timeout":
                        assert result == ()
                        assert second is not None
                        for item in (execution, second):
                            current = await store.get_execution(
                                "app", "owner-1", item.execution_id
                            )
                            assert current.status is ExecutionStatus.TIMED_OUT
                    else:
                        assert len(result) == (2 if second is not None else 1)
                        for item in result:
                            assert isinstance(item, WorkItemClaim)
                            assert item.lease_until == clock.now() + timedelta(
                                minutes=1
                            )
        if existing is not None:
            current = await store.get_execution(
                "app", "owner-1", execution.execution_id
            )
            expected = (
                ExecutionStatus.RUNNING
                if case.startswith(("finish", "interrupt"))
                else ExecutionStatus.QUEUED
            )
            assert current.status is expected
    finally:
        await blocker.rollback()
        await blocker.close()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await store.close()
