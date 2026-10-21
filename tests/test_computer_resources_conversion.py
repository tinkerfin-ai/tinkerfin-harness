"""Verify the offline cutover against disposable MySQL tables and actual DDL."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, event, func, insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)

from tests.support.docker_services import owned_database
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices.models import Device, DeviceExecutionProfile
from tinkerfin_studio.devices.profiles import DeviceProfiles
from tinkerfin_studio.infrastructure.database import Base
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.skills.repository import SkillRepository

conversion = importlib.import_module(
    "apps.studio-server.tools.convert_computer_resources"
)
pytestmark = pytest.mark.docker_integration
_ROOT = Path(__file__).resolve().parents[1]
_TABLES = (
    "conversation_threads",
    "conversation_run_registrations",
    "device_execution_profiles",
)


async def _schema(connection: AsyncConnection, database: str) -> dict[str, object]:
    definitions: dict[str, object] = {}
    for table in _TABLES:
        columns = await connection.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
                "COLUMN_COMMENT, EXTRA, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=:database "
                "AND TABLE_NAME=:table ORDER BY COLUMN_NAME"
            ),
            {"database": database, "table": table},
        )
        indexes = await connection.execute(
            text(
                "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=:database "
                "AND TABLE_NAME=:table ORDER BY INDEX_NAME, SEQ_IN_INDEX"
            ),
            {"database": database, "table": table},
        )
        definitions[table] = (columns.all(), indexes.all())
    return definitions


async def test_conversion_preserves_data_and_converges_to_fresh_schema(tmp_path):
    async with owned_database(tmp_path, "mysql") as url:
        engine = create_async_engine(url, hide_parameters=True)
        try:
            sql = await asyncio.to_thread(
                (_ROOT / "apps/studio-server/database/mysql/schema.sql").read_text
            )
            async with engine.begin() as connection:
                for statement in sql.split(";\n"):
                    if statement.strip():
                        await connection.execute(text(statement.strip()))
                fresh = await _schema(connection, "contract")
                await connection.execute(text("CREATE DATABASE orm_contract"))
                metadata = MetaData(schema="orm_contract")
                for table in _TABLES:
                    Base.metadata.tables[table].to_metadata(metadata)
                await connection.run_sync(metadata.create_all)
                assert await _schema(connection, "orm_contract") == fresh
                await connection.execute(
                    text(
                        "ALTER TABLE conversation_threads MODIFY last_access_mode "
                        "VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '确认模式'"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE conversation_run_registrations "
                        "MODIFY access_mode VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '确认模式', "
                        "MODIFY local_profile_id VARCHAR(36) NULL COMMENT '执行配置'"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE device_execution_profiles MODIFY access_mode "
                        "VARCHAR(32) NOT NULL COMMENT '确认模式'"
                    )
                )
                before = await _schema(connection, "contract")
                now = datetime(2026, 10, 21)
                await connection.execute(
                    insert(ConversationThread),
                    dict(
                        id=1,
                        user_id=1,
                        project_id="project",
                        thread_id="thread",
                        title="报告",
                        status="idle",
                        last_access_mode="full",
                        created_at=now,
                        updated_at=now,
                    ),
                )
                await connection.execute(
                    insert(DeviceExecutionProfile),
                    dict(
                        profile_id="profile",
                        user_id=1,
                        device_id="device",
                        source_thread_id="thread",
                        source_run_id="first",
                        workspace_id="project",
                        workspace_label="研究",
                        access_mode="full",
                        skills=[],
                        created_at=now,
                    ),
                )
                original = [
                    {
                        "runId": "first",
                        "messages": [
                            {"id": "message", "role": "user", "content": "记忆"}
                        ],
                        "forwardedProps": {
                            "accessMode": "full",
                            "projectId": "project",
                            "localTarget": {"device_id": "device"},
                        },
                    },
                    {
                        "runId": "second",
                        "forwardedProps": {
                            "accessMode": "write_approval",
                            "local_target": None,
                        },
                    },
                ]
                await connection.execute(
                    insert(ConversationRunRegistration),
                    [
                        dict(
                            id=index + 1,
                            conversation_thread_id=1,
                            run_id=request["runId"],
                            access_mode="full" if index == 0 else "write_approval",
                            model_id="model",
                            local_profile_id="profile",
                            status="succeeded",
                            input_json=request,
                            started_at=now,
                            finished_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        for index, request in enumerate(original)
                    ],
                )

            backup = tmp_path / "before.json"
            convert = conversion.convert_computer_resources
            with pytest.raises(ValueError, match="指定业务库"):
                await convert(engine, expected_database="another")
            dry = await convert(engine, expected_database="contract")
            assert dry == {
                "database": "contract",
                "requests": 2,
                "columns": 4,
                "applied": False,
            }
            assert not backup.exists()
            async with engine.connect() as connection:
                assert await _schema(connection, "contract") == before

            for table, assignment, restore in (
                (
                    "conversation_run_registrations",
                    "status='running'",
                    "status='succeeded'",
                ),
                (
                    "conversation_threads",
                    "has_pending_interrupt=1",
                    "has_pending_interrupt=0",
                ),
            ):
                async with engine.begin() as connection:
                    await connection.execute(text(f"UPDATE {table} SET {assignment}"))
                with pytest.raises(ValueError, match="结束运行与待审批"):
                    await convert(
                        engine, expected_database="contract", apply=True, backup=backup
                    )
                with pytest.raises(ValueError, match="结束运行与待审批"):
                    await convert(
                        engine, expected_database="contract", apply=True, no_backup=True
                    )
                assert not backup.exists()
                async with engine.begin() as connection:
                    await connection.execute(text(f"UPDATE {table} SET {restore}"))

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE conversation_run_registrations SET status='waiting' WHERE id=1"
                    )
                )
                await connection.execute(
                    text("UPDATE conversation_threads SET last_run_id='first'")
                )
            with pytest.raises(ValueError, match="结束运行与待审批"):
                await convert(
                    engine, expected_database="contract", apply=True, no_backup=True
                )
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE conversation_threads SET last_run_id='second'")
                )

            backup.write_text("保留已有备份")
            with pytest.raises(FileExistsError):
                await convert(
                    engine, expected_database="contract", apply=True, backup=backup
                )
            assert backup.read_text() == "保留已有备份"
            backup.unlink()
            applied = await convert(
                engine, expected_database="contract", apply=True, backup=backup
            )
            assert applied == {**dry, "applied": True}
            assert backup.stat().st_mode & 0o777 == 0o600
            saved = json.loads(backup.read_text())
            assert [item["input_json"] for item in saved["requests"]] == original
            assert set(saved["ddl"]) == set(_TABLES)
            assert len(saved["columns"]) == 4
            async with engine.connect() as connection:
                assert await _schema(connection, "contract") == fresh
                values = (
                    await connection.execute(
                        select(
                            ConversationRunRegistration.run_id,
                            ConversationRunRegistration.access_mode,
                            ConversationRunRegistration.local_profile_id,
                            ConversationRunRegistration.input_json,
                        ).order_by(ConversationRunRegistration.id)
                    )
                ).all()
                for index, row in enumerate(values):
                    expected = json.loads(json.dumps(original[index]))
                    expected["forwardedProps"].pop("localTarget", None)
                    expected["forwardedProps"].pop("local_target", None)
                    assert row == (
                        original[index]["runId"],
                        "full" if index == 0 else "write_approval",
                        "profile",
                        expected,
                    )
                assert (
                    await connection.scalar(select(ConversationThread.last_access_mode))
                    == "full"
                )
                assert (
                    await connection.scalar(select(DeviceExecutionProfile.access_mode))
                    == "full"
                )
            second_backup = tmp_path / "second.json"
            assert await convert(
                engine, expected_database="contract", apply=True, backup=second_backup
            ) == {
                "database": "contract",
                "requests": 0,
                "columns": 0,
                "applied": False,
            }
            assert not second_backup.exists()
            with pytest.raises(ValueError, match="不能同时"):
                await convert(
                    engine,
                    expected_database="contract",
                    apply=True,
                    backup=backup,
                    no_backup=True,
                )
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE conversation_run_registrations SET input_json=JSON_SET(input_json,'$.forwardedProps.localTarget',NULL) WHERE id=1"
                    )
                )
            files_before = set(tmp_path.iterdir())
            assert await convert(
                engine, expected_database="contract", apply=True, no_backup=True
            ) == {
                "database": "contract",
                "requests": 1,
                "columns": 0,
                "applied": True,
            }
            assert set(tmp_path.iterdir()) == files_before
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(ConversationRunRegistration.status).where(
                            ConversationRunRegistration.id == 1
                        )
                    )
                    == "waiting"
                )
                assert await _schema(connection, "contract") == fresh
        finally:
            await engine.dispose()
    assert (
        json.loads((tmp_path / "resources.json").read_text())["state"]
        == "removed_and_verified"
    )


async def test_mysql_parallel_capture_keeps_one_committed_device_binding(tmp_path):
    async with owned_database(tmp_path, "mysql") as url:
        engine = create_async_engine(url, hide_parameters=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            sql = await asyncio.to_thread(
                (_ROOT / "apps/studio-server/database/mysql/schema.sql").read_text
            )
            async with engine.begin() as connection:
                for statement in sql.split(";\n"):
                    if statement.strip():
                        await connection.execute(text(statement.strip()))
            identity = RunIdentity(namespace="ns_71", thread_id="thread", run_id="run")
            now = datetime(2026, 10, 21)
            async with sessions() as session:
                session.add_all(
                    [
                        User(
                            id=71,
                            username="fixture-owner",
                            password_hash="fixture",
                            roles=[],
                            disabled=False,
                        ),
                        Project(
                            id="project",
                            user_id=71,
                            name="Project",
                            created_at=now,
                            updated_at=now,
                        ),
                        Device(
                            device_id="device",
                            user_id=71,
                            name="Mac",
                            credential_digest="a" * 64,
                            capabilities={},
                            paired_at=now,
                        ),
                    ]
                )
                await session.flush()
                repository = ConversationRepository(session)
                thread = await repository.create_thread(
                    user_id=71,
                    project_id="project",
                    thread_id="thread",
                    title="Report",
                    model_id="model",
                )
                await repository.create_run_registration(
                    thread_id=thread.id,
                    run_id="run",
                    parent_run_id=None,
                    model_id="model",
                    input_json={},
                )
                await SkillRepository(session, 71).capture(
                    identity, project_id="project"
                )
                await session.commit()

            async with sessions() as first, sessions() as second:
                bound = await DeviceProfiles(first, 71).capture(
                    identity=identity,
                    project_id="project",
                    access_mode="write_approval",
                )
                attempting_lock = asyncio.Event()
                second_connection = await second.connection()

                def observe_lock(
                    _connection, _cursor, statement, _parameters, _context, _many
                ):
                    if "FOR UPDATE" in statement:
                        attempting_lock.set()

                event.listen(
                    second_connection.sync_connection,
                    "before_cursor_execute",
                    observe_lock,
                )

                async def capture_again():
                    result = await DeviceProfiles(second, 71).capture(
                        identity=identity,
                        project_id="project",
                        access_mode="write_approval",
                    )
                    await second.commit()
                    return result

                async with asyncio.TaskGroup() as tasks:
                    contender = tasks.create_task(capture_again())
                    try:
                        await attempting_lock.wait()
                        assert not contender.done()
                        await first.commit()
                    finally:
                        await first.rollback()
                assert contender.result() == bound
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(DeviceExecutionProfile)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(ConversationRunRegistration.local_profile_id)
                    )
                    == bound.profile_id
                )
                assert (
                    await DeviceProfiles(session, 71).require(bound.profile_id)
                ).device_id == "device"
        finally:
            await engine.dispose()
    assert (
        json.loads((tmp_path / "resources.json").read_text())["state"]
        == "removed_and_verified"
    )
