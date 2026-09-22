"""外部依赖 readiness 检查测试"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

import httpx
import pytest
from redis.asyncio import Redis

from tinkerfin_studio.config.settings import SandboxSettings
from tinkerfin_studio.health import ReadinessService
from tinkerfin_studio.infrastructure.database import Database


class _Connection:
    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, statement: object) -> None:
        del statement


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()


class _Database:
    engine = _Engine()


class _Redis:
    async def ping(self) -> bool:
        return True


async def _sandbox_ready() -> None:
    """模拟框架已验证真实预热容量"""


async def test_readiness_reports_all_available_dependencies() -> None:
    """外部依赖全部可用时，就绪检查逐项返回成功"""

    async def sandbox(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://opensandbox:8090/health"
        return httpx.Response(200, json={"status": "healthy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http_client:
        service = ReadinessService(
            business_database=cast(Database, _Database()),
            components_database=cast(Database, _Database()),
            redis=cast(Redis, _Redis()),
            sandbox=SandboxSettings(
                cpu=1,
                memory_mib=1024,
                domain="opensandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=1,
                workspace_root="/workspace",
                state_namespace="studio",
            ),
            automation_ready=_sandbox_ready,
            attachment_ready=_sandbox_ready,
            sandbox_ready=_sandbox_ready,
            http_client=http_client,
            timeout_seconds=1,
        )

        assert await service.check() == {
            "automation": True,
            "attachments": True,
            "business_database": True,
            "components_database": True,
            "redis": True,
            "opensandbox": True,
        }


async def test_readiness_hides_redis_and_sandbox_failures() -> None:
    """Redis 与 Sandbox 失败不得泄漏底层异常"""

    class BrokenRedis:
        def ping(self) -> Awaitable[bool]:
            raise RuntimeError("credential-bearing failure")

    async def sandbox(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http_client:
        service = ReadinessService(
            business_database=cast(Database, _Database()),
            components_database=cast(Database, _Database()),
            redis=cast(Redis, BrokenRedis()),
            sandbox=SandboxSettings(
                cpu=1,
                memory_mib=1024,
                domain="opensandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=1,
                workspace_root="/workspace",
                state_namespace="studio",
            ),
            automation_ready=_sandbox_ready,
            attachment_ready=_sandbox_ready,
            sandbox_ready=_sandbox_ready,
            http_client=http_client,
            timeout_seconds=1,
        )

        result = await service.check()

    assert result["business_database"] is True
    assert result["redis"] is False
    assert result["opensandbox"] is False


async def test_readiness_rejects_control_plane_health_without_warm_capacity() -> None:
    """控制面存活但框架预热失败时应用不得报告 ready"""

    async def sandbox(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "healthy"})

    async def unavailable() -> None:
        raise RuntimeError("warm capacity unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http_client:
        service = ReadinessService(
            business_database=cast(Database, _Database()),
            components_database=cast(Database, _Database()),
            redis=cast(Redis, _Redis()),
            sandbox=SandboxSettings(
                cpu=1,
                memory_mib=1024,
                domain="opensandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=1,
                workspace_root="/workspace",
                state_namespace="studio",
            ),
            automation_ready=_sandbox_ready,
            attachment_ready=_sandbox_ready,
            sandbox_ready=unavailable,
            http_client=http_client,
            timeout_seconds=1,
        )

        result = await service.check()

    assert result["opensandbox"] is False


@pytest.mark.parametrize("unavailable", ["business_database", "components_database"])
async def test_readiness_reports_each_database_independently(tmp_path, unavailable):
    """单个库不可用时，只将对应状态标为失败"""
    databases = {
        name: Database(f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}")
        for name in ("business_database", "components_database")
    }
    available = next(value for name, value in databases.items() if name != unavailable)
    async with (
        available,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client,
    ):
        service = ReadinessService(
            business_database=databases["business_database"],
            components_database=databases["components_database"],
            redis=cast(Redis, _Redis()),
            sandbox=SandboxSettings(
                cpu=1,
                memory_mib=1024,
                domain="sandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=0,
                state_namespace="test",
                workspace_root="/workspace",
            ),
            sandbox_ready=_sandbox_ready,
            automation_ready=_sandbox_ready,
            attachment_ready=_sandbox_ready,
            http_client=client,
        )
        result = await service.check()
    assert result[unavailable] is False
    assert all(value for name, value in result.items() if name != unavailable)
