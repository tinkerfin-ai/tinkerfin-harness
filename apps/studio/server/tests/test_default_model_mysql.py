"""在可销毁 MySQL 数据库验证默认模型切换的并发与最新配置读取"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.infrastructure.database import Base, Database
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ModelConnectionSave
from tinkerfin_studio.models.service import AgentModelService

pytestmark = pytest.mark.studio_mysql_integration


@pytest_asyncio.fixture
async def default_database(mysql_admin_url: str) -> AsyncIterator[Database]:
    """仅创建和删除本次测试随机命名的专用数据库"""
    name = f"tinkerfin_default_{uuid4().hex}"
    admin_url = make_url(mysql_admin_url)
    admin = create_async_engine(admin_url)
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f"CREATE DATABASE `{name}`")
        async with Database(
            admin_url.set(database=name).render_as_string(hide_password=False)
        ) as database:
            async with database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with database.session() as session:
                session.add(
                    User(
                        id=1,
                        username="default-test",
                        display_name="默认测试",
                        password_hash="unused",
                        roles=[],
                    )
                )
                await session.commit()
                service = AgentModelService(AgentModelRepository(session, user_id=1))
                await service.save_connection(
                    ModelConnectionSave(
                        connection_id="shared",
                        display_name="连接",
                        provider_id="custom",
                        api_type="openai_chat_completions",
                        base_url="https://models.example/v1",
                        api_key=SecretStr("saved-key"),
                    )
                )
                for model_id in ("first", "second"):
                    await service.save_settings(
                        AgentModelSave(
                            model_id=model_id,
                            display_name=model_id,
                            connection_id="shared",
                            model_name=model_id,
                        )
                    )
            yield database
    finally:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await admin.dispose()


async def test_concurrent_defaults_commit_one_default_per_owner(
    default_database: Database,
) -> None:
    async def choose(model_id: str) -> None:
        async with default_database.session() as session:
            service = AgentModelService(AgentModelRepository(session, user_id=1))
            await service.set_default(model_id)
            assert not session.in_transaction()

    async with asyncio.timeout(10):
        await asyncio.gather(*(choose(value) for value in ("first", "second") * 8))
    async with default_database.session() as session:
        rows = list(await session.scalars(select(AgentModel)))
        assert len([row for row in rows if row.is_default]) == 1
        assert all(row.enabled for row in rows)


async def test_default_waits_for_owner_and_checks_latest_saved_key(
    default_database: Database,
    monkeypatch,
) -> None:
    async with (
        default_database.session() as held,
        default_database.session() as waiting,
    ):
        waiting_repository = AgentModelRepository(waiting, user_id=1)
        stale = await waiting_repository.get("first")
        assert stale is not None
        stale_connection = await waiting_repository.connection(stale.connection_id)
        assert stale_connection is not None and stale_connection.api_key
        await AgentModelRepository(held, user_id=1).lock_owner()
        entered = asyncio.Event()
        original_lock = waiting_repository.lock_owner

        async def signal_lock():
            entered.set()
            await original_lock()

        monkeypatch.setattr(waiting_repository, "lock_owner", signal_lock)
        task = asyncio.create_task(
            AgentModelService(waiting_repository).set_default("first")
        )
        try:
            await entered.wait()
            await held.execute(
                update(ModelConnection)
                .where(ModelConnection.user_id == 1)
                .values(api_key="")
            )
            await held.commit()
            async with asyncio.timeout(3):
                with pytest.raises(BusinessException) as rejected:
                    await task
            assert rejected.value.error_code == ModelErrorCode.KEY_REQUIRED
            assert not waiting.in_transaction()
            rows = list(await waiting.scalars(select(AgentModel)))
            assert not any(row.is_default for row in rows)
        finally:
            await held.rollback()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
