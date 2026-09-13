from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.infrastructure.database import Base, Database


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    """提供每个测试独占的 SQLite 数据库"""

    resource = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")
    async with resource:
        async with resource.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield resource


@pytest_asyncio.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """提供测试独占的异步 Session"""

    async with database.session() as value:
        yield value


@pytest_asyncio.fixture
async def attachments(database, tmp_path):
    """提供使用真实磁盘和业务仓储的附件服务"""
    from tinkerfin_studio.attachments.service import AttachmentService
    from tinkerfin_studio.attachments.storage import DiskAttachmentStorage

    return AttachmentService(database, DiskAttachmentStorage(tmp_path / "attachments"))


@pytest_asyncio.fixture
async def model_connections(database):
    """为模型调用测试准备已保存的用户连接，测试仍通过当前模型接口操作"""
    from pydantic import SecretStr

    from tinkerfin_studio.models.repository import AgentModelRepository
    from tinkerfin_studio.models.schemas import ModelConnectionSave
    from tinkerfin_studio.models.service import AgentModelService

    async with database.session() as session:
        for user_id in (1, 2):
            service = AgentModelService(AgentModelRepository(session, user_id=user_id))
            for connection_id in ("configured", "deepseek"):
                await service.save_connection(
                    ModelConnectionSave(
                        connection_id=connection_id,
                        display_name=connection_id,
                        provider_id="deepseek"
                        if connection_id == "deepseek"
                        else "custom",
                        api_type="openai_chat_completions",
                        base_url="https://models.example/v1",
                        api_key=SecretStr(
                            "private-draft-key" if user_id == 1 else "owner-two-key"
                        ),
                    )
                )
