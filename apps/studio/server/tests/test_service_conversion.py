"""一次性服务转换仅在本轮独占的 MySQL 数据库执行"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from apps.studio.server.tools.convert_service_configs import (
    backup,
    convert,
    preflight,
    restore,
)
from pydantic import SecretStr
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import SearchConfig, ServiceSave
from tinkerfin_studio.services.service import ServiceConfigService

pytestmark = pytest.mark.studio_mysql_integration


@pytest_asyncio.fixture
async def conversion_database(mysql_admin_url: str) -> AsyncIterator[AsyncEngine]:
    name = f"tinkerfin_service_{uuid4().hex}"
    admin_url = make_url(mysql_admin_url)
    admin = create_async_engine(admin_url)
    engine = create_async_engine(admin_url.set(database=name), hide_parameters=True)
    created = False
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            created = True
        schema = (Path(__file__).parents[1] / "database/mysql/schema.sql").read_text()
        async with engine.begin() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    await connection.exec_driver_sql(statement)
        yield engine
    finally:
        await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.exec_driver_sql(f"DROP DATABASE `{name}`")
                assert not await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"
                    ),
                    {"name": name},
                )
        await admin.dispose()


async def source_data(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in (
            "DROP TABLE service_configs",
            "ALTER TABLE agent_models ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'chat', ADD COLUMN generation_options JSON",
            "ALTER TABLE conversation_run_registrations DROP COLUMN service_bindings",
            "ALTER TABLE agent_models DROP INDEX ix_agent_models_default, ADD INDEX ix_agent_models_default (user_id,purpose,is_default,enabled)",
            "INSERT INTO users (id,username,password_hash,roles,disabled) VALUES (2,'second','unused','[]',0)",
            "INSERT INTO projects (id,user_id,name,created_at,updated_at) VALUES ('project-1',1,'测试项目','2026-09-01','2026-09-01')",
        ):
            await connection.execute(text(statement))
        for owner, connection_id in (
            (1, "shared"),
            (1, "image-only"),
            (2, "image-only"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO model_connections (user_id,connection_id,display_name,provider_id,api_type,base_url,auth_type,api_key,created_at,updated_at) "
                    "VALUES (:owner,:connection_id,'Provider','custom','openai_chat_completions','https://images.example/v1','api_key',:key,'2026-09-01','2026-09-02')"
                ),
                {
                    "owner": owner,
                    "connection_id": connection_id,
                    "key": f"owner-{owner}-key",
                },
            )
        for owner, model_id, connection_id, purpose in (
            (1, "chat", "shared", "chat"),
            (1, "image", "shared", "image"),
            (1, "alternative", "image-only", "image"),
            (2, "image", "image-only", "image"),
        ):
            await connection.execute(
                text(
                    "INSERT INTO agent_models (user_id,model_id,display_name,connection_id,chat_options,model_name,reasoning_enabled,enabled,is_default,sort_order,created_at,updated_at,purpose,generation_options) "
                    "VALUES (:owner,:model_id,'Display',:connection_id,'{}','image-model',0,:enabled,:is_default,0,'2026-09-01','2026-09-02',:purpose,:options)"
                ),
                {
                    "owner": owner,
                    "model_id": model_id,
                    "connection_id": connection_id,
                    "purpose": purpose,
                    "enabled": owner == 1 and model_id != "alternative",
                    "is_default": purpose == "chat",
                    "options": json.dumps(
                        {
                            "size": "1024x1024",
                            "output_formats": ["png", "webp"],
                            "quality": "high",
                        }
                    ),
                },
            )
        await connection.execute(
            text(
                "INSERT INTO attachment_collections (id,user_id,project_id,purpose,configuration,task_id,created_at) "
                "VALUES ('past-run',1,'project-1','execution',:configuration,'task-1','2026-09-01')"
            ),
            {"configuration": json.dumps({"prompt": "绘图", "model": "chat"})},
        )


async def test_conversion_reports_choices_preserves_ownership_and_restores_backup(
    conversion_database: AsyncEngine,
    tmp_path: Path,
) -> None:
    engine = conversion_database
    await source_data(engine)
    selected, blockers = await preflight(
        engine, engine, choices={}, search_key="global-key"
    )
    assert len(selected) == 1
    assert any("多个生图配置" in item for item in blockers)
    assert any("--search-owner" in item for item in blockers)
    selected, blockers = await preflight(
        engine,
        engine,
        choices={1: "image"},
        search_key="global-key",
        search_owner=1,
    )
    assert blockers == []
    before = tmp_path / "before.json"
    await backup(engine, before)
    assert before.stat().st_mode & 0o777 == 0o600
    await convert(engine, selected, search_key="global-key", search_owner=1)
    async with AsyncSession(engine) as session:
        rows = (await session.scalars(select(ServiceConfig))).all()
        assert len(rows) == 3
        images = {
            row.user_id: row for row in rows if row.capability == "image_generation"
        }
        assert images[1].id == images[2].id == "image"
        assert images[1].enabled and not images[2].enabled
        assert images[1].api_key == "owner-1-key"
        assert images[2].api_key == "owner-2-key"
        assert images[1].config["output_formats"] == ["png", "webp"]
        assert images[1].config["extra"] == {"quality": "high"}
        assert images[1].created_at.isoformat() == "2026-09-01T00:00:00"
        search = next(row for row in rows if row.capability == "web_search")
        assert search.user_id == 1 and search.api_key == "global-key"
        assert (
            await session.execute(text("SELECT model_id FROM agent_models"))
        ).scalars().all() == ["chat"]
        assert (
            await session.execute(text("SELECT connection_id FROM model_connections"))
        ).scalars().all() == ["shared"]
        snapshot = await session.scalar(
            text("SELECT configuration FROM attachment_collections WHERE id='past-run'")
        )
        assert json.loads(snapshot)["services"] is None
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync).get_columns("agent_models")
            }
        )
        assert "purpose" not in columns and "generation_options" not in columns
    with pytest.raises(ValueError, match="无需再次转换"):
        await preflight(engine, engine, choices={})
    await restore(engine, before)
    after = tmp_path / "restored.json"
    await backup(engine, after)
    assert json.loads(after.read_text()) == json.loads(before.read_text())


async def test_preflight_blocks_unfinished_conversations_and_unknown_authentication(
    conversion_database: AsyncEngine,
) -> None:
    engine = conversion_database
    await source_data(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE model_connections SET auth_type='none' WHERE user_id=2")
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_threads (user_id,project_id,thread_id,title,status,message_count,tool_call_count,has_pending_interrupt,pinned,created_at,updated_at) "
                "VALUES (1,'project-1','pending','Pending','waiting_approval',0,0,1,0,'2026-09-01','2026-09-01')"
            )
        )
    _, blockers = await preflight(engine, engine, choices={1: "image"})
    assert any("未结束的会话或审批" in item for item in blockers)
    assert any("无法无损转换" in item for item in blockers)


async def test_concurrent_mysql_saves_keep_one_configuration_per_user(
    conversion_database: AsyncEngine,
) -> None:
    async def save(depth: str) -> str:
        async with AsyncSession(conversion_database, expire_on_commit=False) as session:
            service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
            config = SearchConfig.model_validate({"depth": depth})
            return (
                await service.save(
                    "web_search",
                    ServiceSave(configuration=config, api_key=SecretStr("key")),
                )
            ).id

    first, second = await asyncio.gather(save("basic"), save("advanced"))
    assert first == second
    async with AsyncSession(conversion_database) as session:
        assert len((await session.scalars(select(ServiceConfig))).all()) == 1
