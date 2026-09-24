from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.docker_services import MySQLTestService
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_automation import (
    AutomationExecution,
    AutomationStoreProtocolError,
    AutomationTask,
    ExecutionLimits,
    ExecutionOrigin,
    ExecutionStatus,
    MisfirePolicy,
    OnceSchedule,
    SqlAlchemyAutomationStore,
    TaskStatus,
)
from tinkerfin_contracts import RunIdentity

pytestmark = [pytest.mark.docker_integration, pytest.mark.mysql_integration]


def _task(now: datetime) -> AutomationTask:
    return AutomationTask(
        task_id=str(uuid4()),
        namespace="mysql-test",
        owner_id="owner-1",
        execution_namespace="app",
        name="Concurrent task",
        target="target",
        input={},
        schedule=OnceSchedule(at=now + timedelta(hours=1)),
        status=TaskStatus.ENABLED,
        revision=1,
        next_run_at=now + timedelta(hours=1),
        misfire_policy=MisfirePolicy(),
        limits=ExecutionLimits(max_concurrent_runs=2, max_queued_runs=4),
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
        input={},
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
    owner_id: str,
    limits: ExecutionLimits,
) -> AutomationExecution:
    task = replace(_task(now), owner_id=owner_id, limits=limits)
    return replace(
        _execution(task, now, execution_id=str(uuid4())),
        task_id=None,
        origin=ExecutionOrigin.ONE_TIME,
    )


async def test_mysql_concurrent_first_setup_creates_one_current_schema(
    sql_engine_factory: SqlEngineFactory,
    mysql_test_service: MySQLTestService,
) -> None:
    database_name = f"tinkerfin_automation_{uuid4().hex}"
    admin_engine = create_async_engine(
        mysql_test_service.url("tinkerfin_test_admin"), pool_pre_ping=True
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4")
        )
    first_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    second_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    try:
        await asyncio.gather(
            first_store.setup(), first_store.setup(), second_store.setup()
        )
        await asyncio.gather(first_store.setup(), second_store.setup())
        async with admin_engine.connect() as connection:
            table_names = (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :database_name"
                    ),
                    {"database_name": database_name},
                )
            ).scalars()
            assert set(table_names) == {
                "tinkerfin_automation_tasks",
                "tinkerfin_automation_runs",
                "tinkerfin_automation_work_items",
                "tinkerfin_automation_scopes",
                "tinkerfin_automation_operations",
            }
    finally:
        await first_store.close()
        await second_store.close()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database_name}`"))
        await admin_engine.dispose()


async def test_mysql_setup_rejects_stale_nullability(
    sql_engine_factory: SqlEngineFactory,
    mysql_test_service: MySQLTestService,
) -> None:
    database_name = f"tinkerfin_automation_{uuid4().hex}"
    admin_engine = create_async_engine(
        mysql_test_service.url("tinkerfin_test_admin"), pool_pre_ping=True
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4")
        )
    initial = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    candidate = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    try:
        await initial.setup()
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"ALTER TABLE `{database_name}`.tinkerfin_automation_runs "
                    "MODIFY COLUMN task_id VARCHAR(36) NOT NULL "
                    "COMMENT 'Source task identity; null for a one-time execution'"
                )
            )
        with pytest.raises(AutomationStoreProtocolError, match="nullability"):
            await candidate.setup()
    finally:
        await initial.close()
        await candidate.close()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database_name}`"))
        await admin_engine.dispose()


async def test_mysql_setup_rejects_stale_table_comment(
    sql_engine_factory: SqlEngineFactory,
    mysql_test_service: MySQLTestService,
) -> None:
    database_name = f"tinkerfin_automation_{uuid4().hex}"
    admin_engine = create_async_engine(
        mysql_test_service.url("tinkerfin_test_admin"), pool_pre_ping=True
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4")
        )
    initial = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    candidate = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    try:
        await initial.setup()
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"ALTER TABLE `{database_name}`.tinkerfin_automation_tasks "
                    "COMMENT = 'stale table comment'"
                )
            )
        with pytest.raises(AutomationStoreProtocolError, match="comment"):
            await candidate.setup()
    finally:
        await initial.close()
        await candidate.close()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database_name}`"))
        await admin_engine.dispose()


async def test_mysql_workers_share_global_admission_and_fenced_claims(
    sql_engine_factory: SqlEngineFactory,
    mysql_test_service: MySQLTestService,
) -> None:
    database_name = f"tinkerfin_automation_{uuid4().hex}"
    admin_engine = create_async_engine(
        mysql_test_service.url("tinkerfin_test_admin"), pool_pre_ping=True
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4")
        )
    first_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    second_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    try:
        await first_store.setup()
        now = await first_store.current_time()
        task = _task(now)
        await first_store.create_task(task, request_id=None, input_digest="task")
        for ordinal in (1, 2):
            execution = _execution(task, now, execution_id=str(uuid4()))
            await first_store.enqueue_execution(
                execution,
                occurrence_key=f"occurrence-{ordinal}",
                request_id=None,
                input_digest=f"execution-{ordinal}",
            )

        first_claims, second_claims = await asyncio.gather(
            first_store.claim_work(
                "mysql-test",
                "worker-1",
                limit=1,
                lease_duration=timedelta(minutes=1),
                global_concurrency=1,
            ),
            second_store.claim_work(
                "mysql-test",
                "worker-2",
                limit=1,
                lease_duration=timedelta(minutes=1),
                global_concurrency=1,
            ),
        )
        claims = (*first_claims, *second_claims)
        assert len(claims) == 1
        claim = claims[0]
        owner = first_store if first_claims else second_store
        authorization = await owner.authorize_start(
            claim, execution_timeout=timedelta(minutes=30)
        )
        await owner.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result={"worker": "winner"}
        )
        assert authorization.execution.start_authorized_at is not None

        next_claims = await first_store.claim_work(
            "mysql-test",
            "worker-1",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
        assert len(next_claims) == 1
        assert next_claims[0].fence == 1
    finally:
        await first_store.close()
        await second_store.close()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database_name}`"))
        await admin_engine.dispose()


async def test_mysql_workers_enforce_taskless_concurrency_per_owner(
    sql_engine_factory: SqlEngineFactory,
    mysql_test_service: MySQLTestService,
) -> None:
    database_name = f"tinkerfin_automation_{uuid4().hex}"
    admin_engine = create_async_engine(
        mysql_test_service.url("tinkerfin_test_admin"), pool_pre_ping=True
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4")
        )
    first_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    second_store = SqlAlchemyAutomationStore(
        sql_engine_factory(mysql_test_service.url(database_name))
    )
    try:
        await first_store.setup()
        now = await first_store.current_time()
        limits = ExecutionLimits(max_concurrent_runs=1, max_queued_runs=2)
        owner_id = "o" * 191
        executions = (
            _taskless_execution(now, owner_id=owner_id, limits=limits),
            _taskless_execution(now, owner_id=owner_id, limits=limits),
            _taskless_execution(now, owner_id="owner-2", limits=limits),
        )
        for ordinal, execution in enumerate(executions, start=1):
            await first_store.enqueue_execution(
                execution,
                occurrence_key=f"taskless-occurrence-{ordinal}",
                request_id=None,
                input_digest=f"taskless-execution-{ordinal}",
            )

        first_claims, second_claims = await asyncio.gather(
            first_store.claim_work(
                "mysql-test",
                "worker-1",
                limit=3,
                lease_duration=timedelta(minutes=1),
                global_concurrency=3,
            ),
            second_store.claim_work(
                "mysql-test",
                "worker-2",
                limit=3,
                lease_duration=timedelta(minutes=1),
                global_concurrency=3,
            ),
        )
        claims = (*first_claims, *second_claims)
        claimed_owners = [
            (
                await first_store.get_scheduled_execution(
                    "mysql-test", claim.execution_id
                )
            ).owner_id
            for claim in claims
        ]
        assert sorted(claimed_owners) == sorted((owner_id, "owner-2"))
    finally:
        await first_store.close()
        await second_store.close()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database_name}`"))
        await admin_engine.dispose()
