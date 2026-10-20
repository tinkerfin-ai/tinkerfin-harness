"""个人服务配置的数据访问与用户范围锁"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.auth.models import User
from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.schemas import Capability


class ServiceConfigRepository:
    """在已认证用户范围内读写唯一服务配置"""

    def __init__(self, session: AsyncSession, *, user_id: int) -> None:
        self.session = session
        self.user_id = user_id

    async def lock_owner(self) -> None:
        await self.session.scalar(
            select(User.id).where(User.id == self.user_id).with_for_update()
        )

    async def get(
        self, capability: Capability, *, for_update: bool = False
    ) -> ServiceConfig | None:
        statement = (
            select(ServiceConfig)
            .where(
                ServiceConfig.user_id == self.user_id,
                ServiceConfig.capability == capability,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def delete(self, row: ServiceConfig) -> None:
        await self.session.delete(row)
        await self.session.flush()

    async def record_test(
        self, row: ServiceConfig, *, status: str, code: str, fingerprint: str
    ) -> None:
        row.test_status = status
        row.test_code = code
        row.test_fingerprint = fingerprint
        row.tested_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.flush()
