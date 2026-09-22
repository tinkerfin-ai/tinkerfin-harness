"""Studio 外部依赖 readiness 检查"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import cast

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tinkerfin_automation import AutomationError
from tinkerfin_studio.config.settings import SandboxSettings
from tinkerfin_studio.infrastructure.database import Database


class ReadinessService:
    """并发检查请求链依赖且隐藏底层异常"""

    def __init__(
        self,
        *,
        business_database: Database,
        components_database: Database,
        redis: Redis,
        sandbox: SandboxSettings,
        sandbox_ready: Callable[[], Awaitable[None]],
        automation_ready: Callable[[], Awaitable[None]],
        attachment_ready: Callable[[], Awaitable[None]],
        http_client: httpx.AsyncClient,
        timeout_seconds: float = 3,
    ) -> None:
        self._business_database = business_database
        self._components_database = components_database
        self._redis = redis
        self._sandbox = sandbox
        self._sandbox_ready = sandbox_ready
        self._automation_ready = automation_ready
        self._attachment_ready = attachment_ready
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    async def _check_database(self, database: Database) -> None:
        async with database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _check_redis(self) -> None:
        if not await cast(Awaitable[bool], self._redis.ping()):
            raise RuntimeError("Redis PING 未返回成功")

    async def _check_opensandbox(self) -> None:
        # 控制面健康不代表预热容量真实可用；框架生命周期检查必须先通过
        await self._sandbox_ready()
        headers = {}
        if self._sandbox.api_key is not None:
            headers["OPEN-SANDBOX-API-KEY"] = self._sandbox.api_key.get_secret_value()
        response = await self._http_client.get(
            f"{self._sandbox.protocol}://{self._sandbox.domain}/health",
            headers=headers,
        )
        response.raise_for_status()

    async def _safe(self, check: Callable[[], Awaitable[None]]) -> bool:
        try:
            await asyncio.wait_for(check(), timeout=self._timeout_seconds)
        except (
            AutomationError,
            OSError,
            RedisError,
            RuntimeError,
            SQLAlchemyError,
            TimeoutError,
            httpx.HTTPError,
        ):
            return False
        return True

    async def check(self) -> dict[str, bool]:
        """返回不含异常和凭据的稳定组件状态"""

        (
            business,
            components,
            redis,
            opensandbox,
            automation,
            attachments,
        ) = await asyncio.gather(
            self._safe(partial(self._check_database, self._business_database)),
            self._safe(partial(self._check_database, self._components_database)),
            self._safe(self._check_redis),
            self._safe(self._check_opensandbox),
            self._safe(self._automation_ready),
            self._safe(self._attachment_ready),
        )
        return {
            "attachments": attachments,
            "business_database": business,
            "components_database": components,
            "redis": redis,
            "opensandbox": opensandbox,
            "automation": automation,
        }
