"""Agent 模型配置数据访问"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.models.schemas import AgentModelWrite, ModelConnectionSave


class AgentModelRepository:
    """模型目录查询与事务写入"""

    def __init__(self, session: AsyncSession, *, user_id: int) -> None:
        self._session = session
        self._user_id = user_id

    async def lock_owner(self) -> None:
        """串行化同一用户的默认模型变更，避免并发产生多个默认项"""
        await self._session.scalar(
            select(User.id).where(User.id == self._user_id).with_for_update()
        )

    async def list_enabled(self) -> list[AgentModel]:
        """按业务排序读取启用模型"""

        result = await self._session.scalars(
            select(AgentModel)
            .where(
                AgentModel.user_id == self._user_id,
                AgentModel.purpose == "chat",
                AgentModel.enabled.is_(True),
            )
            .order_by(AgentModel.sort_order, AgentModel.id)
        )
        return list(result)

    async def get(self, model_id: str) -> AgentModel | None:
        """按稳定 ID 读取模型"""

        return await self._session.scalar(
            select(AgentModel).where(
                AgentModel.user_id == self._user_id, AgentModel.model_id == model_id
            )
        )

    async def get_for_update(self, model_id: str) -> AgentModel | None:
        """锁定并读取最新配置，与运行登记共用用户锁顺序"""
        return await self._session.scalar(
            select(AgentModel)
            .where(AgentModel.user_id == self._user_id, AgentModel.model_id == model_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def clear_default(self, purpose: Literal["chat", "image"] = "chat") -> None:
        """在当前事务中清除既有默认标记"""

        await self._session.execute(
            update(AgentModel)
            .where(
                AgentModel.user_id == self._user_id,
                AgentModel.purpose == purpose,
                AgentModel.is_default.is_(True),
            )
            .values(is_default=False, updated_at=datetime.now(UTC).replace(tzinfo=None))
        )

    async def upsert(self, value: AgentModelWrite) -> AgentModel:
        """新增或完整更新一条模型配置"""

        entity = await self.get(value.model_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        if entity is None:
            entity = AgentModel(
                user_id=self._user_id,
                model_id=value.model_id,
                created_at=now,
                updated_at=now,
            )
            self._session.add(entity)
        entity.generation_options = value.generation_options
        entity.purpose = value.purpose
        entity.display_name = value.display_name
        entity.connection_id = value.connection_id
        entity.chat_options = value.chat_options.model_dump(
            mode="json", exclude_none=True
        )
        entity.model_name = value.model_name
        entity.image_support = value.image_support
        entity.reasoning_enabled = value.reasoning_enabled
        entity.enabled = value.enabled
        entity.is_default = value.is_default
        entity.sort_order = value.sort_order
        entity.updated_at = now
        await self._session.flush()
        return entity

    async def set_default(self, model_id: str, *, purpose: str) -> None:
        """在已持有用户锁的事务内启用目标，并替换本人同用途默认项"""
        now = datetime.now(UTC).replace(tzinfo=None)
        await self._session.execute(
            update(AgentModel)
            .where(
                AgentModel.user_id == self._user_id,
                AgentModel.purpose == purpose,
                AgentModel.model_id != model_id,
                AgentModel.is_default.is_(True),
            )
            .values(is_default=False, updated_at=now)
        )
        await self._session.execute(
            update(AgentModel)
            .where(
                AgentModel.user_id == self._user_id,
                AgentModel.model_id == model_id,
                AgentModel.purpose == purpose,
            )
            .values(enabled=True, is_default=True, updated_at=now)
        )

    async def in_use(self, model_id: str) -> bool:
        """包括准备阶段在内，有未终止运行时保护该模型配置"""
        row = await self._session.scalar(
            select(ConversationRunRegistration.id)
            .join(
                ConversationThread,
                ConversationRunRegistration.conversation_thread_id
                == ConversationThread.id,
            )
            .where(
                ConversationThread.user_id == self._user_id,
                ConversationRunRegistration.model_id == model_id,
                ConversationRunRegistration.status.in_(
                    ("preparing", "starting", "running", "waiting")
                ),
            )
            .limit(1)
        )
        return row is not None

    async def list_settings(self) -> list[AgentModel]:
        """返回当前用户的全部配置，包括禁用模型和生图服务"""
        rows = await self._session.scalars(
            select(AgentModel)
            .where(AgentModel.user_id == self._user_id)
            .order_by(AgentModel.sort_order, AgentModel.id)
        )
        return list(rows)

    async def delete(self, model_id: str) -> None:
        """只删除当前用户拥有的模型配置"""
        row = await self.get(model_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    async def connections(self) -> list[ModelConnection]:
        rows = await self._session.scalars(
            select(ModelConnection)
            .where(ModelConnection.user_id == self._user_id)
            .order_by(ModelConnection.id)
        )
        return list(rows)

    async def connection(
        self, connection_id: str, *, for_update: bool = False
    ) -> ModelConnection | None:
        """读取本人连接，写入路径在用户锁后使用当前读避免旧事务快照"""
        statement = (
            select(ModelConnection)
            .where(
                ModelConnection.user_id == self._user_id,
                ModelConnection.connection_id == connection_id,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def save_connection(self, value: ModelConnectionSave, key: str) -> None:
        entity = await self.connection(value.connection_id, for_update=True)
        if entity is None:
            entity = ModelConnection(
                user_id=self._user_id, connection_id=value.connection_id
            )
            self._session.add(entity)
        entity.display_name = value.display_name
        entity.provider_id = value.provider_id
        entity.api_type = value.api_type
        entity.base_url = value.base_url
        entity.auth_type = value.auth_type
        entity.api_key = key
        entity.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self._session.flush()

    async def connection_models(self, connection_id: str) -> list[AgentModel]:
        rows = await self._session.scalars(
            select(AgentModel)
            .where(
                AgentModel.user_id == self._user_id,
                AgentModel.connection_id == connection_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def delete_connection(self, connection_id: str) -> None:
        await self._session.execute(
            delete(AgentModel).where(
                AgentModel.user_id == self._user_id,
                AgentModel.connection_id == connection_id,
            )
        )
        await self._session.execute(
            delete(ModelConnection).where(
                ModelConnection.user_id == self._user_id,
                ModelConnection.connection_id == connection_id,
            )
        )

    async def commit(self) -> None:
        """提交当前模型目录事务"""

        await self._session.commit()

    async def rollback(self) -> None:
        """回滚当前模型目录事务"""

        await self._session.rollback()
