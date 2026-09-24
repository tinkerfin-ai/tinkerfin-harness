from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_automation import (
    AutomationExecution,
    AutomationStoreError,
    AutomationStoreProtocolError,
    AutomationTask,
    ExecutionLimits,
    ExecutionOrigin,
    ExecutionStatus,
    MisfirePolicy,
    OnceSchedule,
    QueueFullError,
    RequestConflictError,
    SqlAlchemyAutomationStore,
    TaskStatus,
)
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.sql_schema import AUTOMATION_TABLE_NAMES, tasks, work_items
from tinkerfin_automation.store import AutomationStore
from tinkerfin_contracts import RunIdentity


def _task(now: datetime) -> AutomationTask:
    return AutomationTask(
        task_id="00000000-0000-0000-0000-000000000001",
        namespace="app",
        owner_id="owner-1",
        execution_namespace="app",
        name="Daily summary",
        target="summary",
        input={"project_id": "project-1"},
        schedule=OnceSchedule(at=now + timedelta(hours=1)),
        status=TaskStatus.ENABLED,
        revision=1,
        next_run_at=now + timedelta(hours=1),
        misfire_policy=MisfirePolicy(),
        limits=ExecutionLimits(max_queued_runs=2),
        created_at=now,
        updated_at=now,
    )


def _execution(
    task: AutomationTask, now: datetime, *, execution_id: str
) -> AutomationExecution:
    return AutomationExecution(
        execution_id=execution_id,
        task_id=task.task_id,
        namespace=task.namespace,
        owner_id=task.owner_id,
        identity=RunIdentity(
            namespace=task.namespace,
            thread_id=f"thread-{execution_id}",
            run_id=f"run-{execution_id}",
        ),
        target=task.target,
        input=task.input,
        limits=task.limits,
        origin=ExecutionOrigin.MANUAL,
        status=ExecutionStatus.QUEUED,
        attempt=1,
        retry_of=None,
        scheduled_for=None,
        queued_at=now,
        queue_deadline=now + timedelta(hours=1),
        execution_started_at=None,
        execution_deadline=None,
        finished_at=None,
        failure_code=None,
        failure_message=None,
        result=None,
        interrupt_ids=(),
        start_authorized_at=None,
        start_token=None,
        created_at=now,
        updated_at=now,
    )


def _taskless_execution(
    now: datetime,
    *,
    execution_id: str,
    owner_id: str = "owner-1",
    limits: ExecutionLimits | None = None,
) -> AutomationExecution:
    task = replace(_task(now), owner_id=owner_id)
    return replace(
        _execution(task, now, execution_id=execution_id),
        task_id=None,
        origin=ExecutionOrigin.ONE_TIME,
        limits=limits or task.limits,
    )


@pytest.mark.asyncio
async def test_sqlite_schema_store_contract_and_borrowed_engine(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'automation.db'}")
    concrete = SqlAlchemyAutomationStore(engine)
    store: AutomationStore = concrete
    await concrete.setup()
    now = await concrete.current_time()
    task = _task(now)
    try:
        created = await concrete.create_task(
            task, request_id="create-1", input_digest="create-digest"
        )
        repeated = await concrete.create_task(
            task, request_id="create-1", input_digest="create-digest"
        )
        assert repeated == created
        with pytest.raises(RequestConflictError):
            await concrete.create_task(
                task, request_id="create-1", input_digest="different"
            )

        execution = _execution(
            task,
            now,
            execution_id="10000000-0000-0000-0000-000000000001",
        )
        await concrete.enqueue_execution(
            execution,
            occurrence_key="occurrence-1",
            request_id="run-1",
            input_digest="run-digest",
        )
        (claim,) = await concrete.claim_work(
            "app",
            "worker-1",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=16,
        )
        authorization = await concrete.authorize_start(
            claim, execution_timeout=timedelta(minutes=30)
        )
        assert authorization.execution.status is ExecutionStatus.RUNNING
        succeeded = await concrete.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result={"ok": True}
        )
        assert succeeded.status is ExecutionStatus.SUCCEEDED
        assert (
            await concrete.get_execution("app", "owner-1", execution.execution_id)
        ).result == {"ok": True}

        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        assert set(table_names) == set(AUTOMATION_TABLE_NAMES)
    finally:
        await concrete.close()

    assert store is not None
    async with engine.connect() as connection:
        assert await connection.scalar(select(1)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_concurrent_setup_across_store_instances_is_complete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent-setup.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    first = SqlAlchemyAutomationStore(first_engine)
    second = SqlAlchemyAutomationStore(second_engine)
    try:
        await asyncio.gather(first.setup(), first.setup(), second.setup())
        await asyncio.gather(first.setup(), second.setup())

        async with first_engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        assert set(table_names) == set(AUTOMATION_TABLE_NAMES)
    finally:
        await first.close()
        await second.close()
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_setup_failure_is_shared_then_next_call_retries(
    sql_engine_factory: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'setup-retry.db'}")
    )
    original_setup = store._setup_once
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    attempts = 0

    async def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            raise AutomationStoreError("transient setup failure")
        await original_setup()

    monkeypatch.setattr(store, "_setup_once", fail_once)
    first_waiter = asyncio.create_task(store.setup())
    try:
        await first_attempt_started.wait()
        second_waiter = asyncio.create_task(store.setup())
        release_first_attempt.set()
        first_result, second_result = await asyncio.gather(
            first_waiter,
            second_waiter,
            return_exceptions=True,
        )
        assert isinstance(first_result, AutomationStoreError)
        assert isinstance(second_result, AutomationStoreError)
        assert attempts == 1

        await store.setup()
        await store.setup()
        assert attempts == 2
    finally:
        release_first_attempt.set()
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_setup_caller_cancellation_settles_retained_work(
    sql_engine_factory: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'setup-cancel.db'}")
    )
    original_setup = store._setup_once
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()
    attempts = 0

    async def delayed_setup() -> None:
        nonlocal attempts
        attempts += 1
        setup_started.set()
        await release_setup.wait()
        await original_setup()

    monkeypatch.setattr(store, "_setup_once", delayed_setup)
    caller = asyncio.create_task(store.setup())
    try:
        await setup_started.wait()
        caller.cancel("setup caller stopped")
        await asyncio.sleep(0)
        caller.cancel("repeated setup cancellation")
        await asyncio.sleep(0)
        assert not caller.done()

        release_setup.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await caller
        assert caught.value.args == ("setup caller stopped",)
        await store.setup()
        assert attempts == 1
    finally:
        release_setup.set()
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_close_waits_for_shared_setup_before_returning(
    sql_engine_factory: SqlEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'close-during-setup.db'}")
    )
    original_setup = store._setup_once
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()

    async def delayed_setup() -> None:
        setup_started.set()
        await release_setup.wait()
        await original_setup()

    monkeypatch.setattr(store, "_setup_once", delayed_setup)
    setup = asyncio.create_task(store.setup())
    await setup_started.wait()
    first_close = asyncio.create_task(store.close())
    second_close = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not first_close.done()
    assert not second_close.done()

    release_setup.set()
    await setup
    await asyncio.gather(first_close, second_close)
    with pytest.raises(AutomationStoreError, match="closed"):
        await store.current_time()


@pytest.mark.asyncio
async def test_sqlite_setup_rejects_partial_schema_without_repair(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(tasks.create)
    store = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="incomplete"):
            await store.setup()
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        assert set(table_names) == {tasks.name}
    finally:
        await store.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_setup_rejects_unknown_owned_table_without_creating_schema(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown.db'}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE tinkerfin_automation_legacy (id INTEGER PRIMARY KEY)"
        )
    store = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="unknown"):
            await store.setup()
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        assert table_names == ["tinkerfin_automation_legacy"]
    finally:
        await store.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_setup_rejects_stale_nullability(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nullable.db'}")
    initial = SqlAlchemyAutomationStore(engine)
    await initial.setup()
    async with engine.begin() as connection:
        run_ddl = await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'tinkerfin_automation_runs'"
        )
        current_ddl = run_ddl.scalar_one()
        assert isinstance(current_ddl, str)
        await connection.exec_driver_sql("DROP TABLE tinkerfin_automation_runs")
        await connection.exec_driver_sql(
            current_ddl.replace(
                "task_id VARCHAR(36),", "task_id VARCHAR(36) NOT NULL,", 1
            )
        )

    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="nullability"):
            await candidate.setup()
    finally:
        await initial.close()
        await candidate.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_setup_rejects_stale_index(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index.db'}")
    initial = SqlAlchemyAutomationStore(engine)
    await initial.setup()
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP INDEX ix_tinkerfin_automation_tasks_due")
        await connection.exec_driver_sql(
            "CREATE INDEX ix_tinkerfin_automation_tasks_due "
            "ON tinkerfin_automation_tasks(namespace, status, task_id)"
        )

    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match="indexes"):
            await candidate.setup()
    finally:
        await initial.close()
        await candidate.close()
        await engine.dispose()


@pytest.mark.parametrize(
    ("current_fragment", "stale_fragment", "expected_issue"),
    (
        ("revision BIGINT NOT NULL", "revision INTEGER NOT NULL", "type"),
        (
            "status VARCHAR(16) NOT NULL",
            "status VARCHAR(16) DEFAULT 'enabled' NOT NULL",
            "default",
        ),
        (
            "PRIMARY KEY (task_id)",
            "PRIMARY KEY (task_id, namespace)",
            "primary key",
        ),
        (
            "UNIQUE (namespace, owner_id, task_id)",
            "UNIQUE (namespace, task_id)",
            "unique constraints",
        ),
        ("CHECK (revision >= 1)", "CHECK (revision >= 0)", "check constraints"),
    ),
)
@pytest.mark.asyncio
async def test_sqlite_setup_rejects_stale_table_contract(
    tmp_path: Path,
    current_fragment: str,
    stale_fragment: str,
    expected_issue: str,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}")
    initial = SqlAlchemyAutomationStore(engine)
    await initial.setup()
    async with engine.begin() as connection:
        result = await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'tinkerfin_automation_tasks'"
        )
        current_ddl = result.scalar_one()
        assert isinstance(current_ddl, str)
        assert current_fragment in current_ddl
        await connection.exec_driver_sql("DROP TABLE tinkerfin_automation_tasks")
        await connection.exec_driver_sql(
            current_ddl.replace(current_fragment, stale_fragment, 1)
        )

    candidate = SqlAlchemyAutomationStore(engine)
    try:
        with pytest.raises(AutomationStoreProtocolError, match=expected_issue):
            await candidate.setup()
    finally:
        await initial.close()
        await candidate.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_service_automatically_sets_up_sql_store(
    tmp_path: Path, sql_engine_factory: SqlEngineFactory
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'service.db'}")
    )
    service = AutomationService(namespace="app", store=store)
    try:
        execution = await service.execute_once(
            owner_id="owner-1",
            target="summary",
            input={"project_id": "project-1"},
        )
        assert (
            await service.get_execution(
                owner_id="owner-1", execution_id=execution.execution_id
            )
        ) == execution
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_stale_started_claim_becomes_uncertain(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    now = await store.current_time()
    task = _task(now)
    execution = _execution(
        task,
        now,
        execution_id="10000000-0000-0000-0000-000000000002",
    )
    try:
        await store.create_task(task, request_id=None, input_digest="task")
        await store.enqueue_execution(
            execution,
            occurrence_key="occurrence-2",
            request_id=None,
            input_digest="execution",
        )
        (claim,) = await store.claim_work(
            "app",
            "worker-1",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=16,
        )
        await store.authorize_start(claim, execution_timeout=timedelta(minutes=30))
        async with engine.begin() as connection:
            await connection.execute(
                update(work_items)
                .where(work_items.c.work_item_id == claim.work_item_id)
                .values(lease_until=datetime(2000, 1, 1))
            )
        assert not await store.claim_work(
            "app",
            "worker-2",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=16,
        )
        assert (
            await store.get_execution("app", "owner-1", execution.execution_id)
        ).status is ExecutionStatus.NEEDS_ATTENTION
    finally:
        await store.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_taskless_execution_uses_owner_admission_without_task_row(
    sql_engine_factory: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'taskless.db'}")
    )
    await store.setup()
    now = await store.current_time()
    limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=2)
    executions = (
        _taskless_execution(
            now,
            execution_id="20000000-0000-0000-0000-000000000001",
            limits=limits,
        ),
        _taskless_execution(
            now,
            execution_id="20000000-0000-0000-0000-000000000002",
            limits=limits,
        ),
        _taskless_execution(
            now,
            execution_id="20000000-0000-0000-0000-000000000003",
            owner_id="owner-2",
            limits=limits,
        ),
    )
    try:
        for index, execution in enumerate(executions, start=1):
            stored = await store.enqueue_execution(
                execution,
                occurrence_key=f"taskless-{index}",
                request_id="once-1" if index == 1 else None,
                input_digest=f"execution-{index}",
            )
            assert stored.task_id is None
        repeated = await store.enqueue_execution(
            executions[0],
            occurrence_key="taskless-1",
            request_id="once-1",
            input_digest="execution-1",
        )
        assert repeated.execution_id == executions[0].execution_id
        with pytest.raises(RequestConflictError):
            await store.enqueue_execution(
                executions[0],
                occurrence_key="taskless-1",
                request_id="once-1",
                input_digest="different",
            )
        with pytest.raises(QueueFullError):
            await store.enqueue_execution(
                _taskless_execution(
                    now,
                    execution_id="20000000-0000-0000-0000-000000000004",
                    limits=limits,
                ),
                occurrence_key="taskless-4",
                request_id=None,
                input_digest="execution-4",
            )

        assert not (
            await store.list_tasks("app", "owner-1", limit=10, cursor=None)
        ).items
        claims = await store.claim_work(
            "app",
            "worker",
            limit=3,
            lease_duration=timedelta(minutes=1),
            global_concurrency=3,
        )
        assert len(claims) == 2
        claims_by_owner = {
            (
                await store.get_scheduled_execution("app", claim.execution_id)
            ).owner_id: claim
            for claim in claims
        }
        assert set(claims_by_owner) == {"owner-1", "owner-2"}

        await store.finish_execution(
            claims_by_owner["owner-1"],
            status=ExecutionStatus.SUCCEEDED,
            result={"ok": True},
        )
        (next_claim,) = await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=3,
        )
        assert (
            await store.get_scheduled_execution("app", next_claim.execution_id)
        ).owner_id == "owner-1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_concurrent_occurrence_is_one_business_execution(
    sql_engine_factory: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    store = SqlAlchemyAutomationStore(
        sql_engine_factory(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    )
    await store.setup()
    now = await store.current_time()
    task = replace(_task(now), limits=ExecutionLimits(max_queued_runs=10))
    await store.create_task(task, request_id=None, input_digest="task")
    first = _execution(
        task,
        now,
        execution_id="10000000-0000-0000-0000-000000000003",
    )
    second = _execution(
        task,
        now,
        execution_id="10000000-0000-0000-0000-000000000004",
    )
    try:
        results = await asyncio.gather(
            store.enqueue_execution(
                first,
                occurrence_key="same-occurrence",
                request_id=None,
                input_digest="first",
            ),
            store.enqueue_execution(
                second,
                occurrence_key="same-occurrence",
                request_id=None,
                input_digest="second",
            ),
        )
        assert results[0].execution_id == results[1].execution_id
        history = await store.list_executions(
            "app", "owner-1", task_id=task.task_id, limit=10, cursor=None
        )
        assert len(history.items) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_invalid_payload_is_a_store_protocol_error(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'invalid.db'}")
    store = SqlAlchemyAutomationStore(engine)
    await store.setup()
    now = await store.current_time()
    task = _task(now)
    try:
        await store.create_task(task, request_id=None, input_digest="task")
        async with engine.begin() as connection:
            await connection.execute(
                update(tasks).where(tasks.c.task_id == task.task_id).values(payload="{")
            )

        with pytest.raises(AutomationStoreProtocolError) as caught:
            await store.get_task(task.namespace, task.owner_id, task.task_id)
        assert caught.value.diagnostic_context["record_kind"] == "task payload"
    finally:
        await store.close()
        await engine.dispose()
