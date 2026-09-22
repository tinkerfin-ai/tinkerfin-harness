from pathlib import Path

import pytest
from sqlalchemy import text

from tinkerfin_sandbox import SQLAlchemyOpenSandboxState
from tinkerfin_studio.infrastructure.database import Database


async def test_database_lifecycle_yields_isolated_async_sessions(
    tmp_path: Path,
) -> None:
    """数据库资源应显式打开、关闭并提供原生异步会话"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")

    async with database:
        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1

    with pytest.raises(RuntimeError, match="尚未启动"):
        database.session()


async def test_sandbox_state_close_keeps_shared_components_engine_usable(
    tmp_path: Path,
) -> None:
    """沙箱状态存储关闭后，共享组件连接池仍可创建会话"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}")
    async with database:
        state = SQLAlchemyOpenSandboxState(engine=database.engine)
        await state.start(warm_pool_size=0)
        await state.aclose()

        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.docker_integration
async def test_mysql_connection_budget_covers_both_pools(
    mysql_sandbox_url: str,
) -> None:
    """同实例的两个连接池按合计容量验证预算"""

    database = Database(mysql_sandbox_url, pool_size=2, max_overflow=1)
    async with database:
        verified = await database.verify_connection_budget(
            total_pool_capacity=6,
            configured_budget=6,
            management_reserve=1,
        )
        assert verified.sqlalchemy_pool_capacity == 6
        assert verified.server_max_connections >= 5

        with pytest.raises(RuntimeError, match="max_connections"):
            await database.verify_connection_budget(
                total_pool_capacity=6,
                configured_budget=verified.server_max_connections,
                management_reserve=1,
            )


@pytest.mark.parametrize("cancelled", [False, True])
async def test_application_releases_both_databases_when_startup_fails(
    tmp_path, monkeypatch, cancelled
):
    """第二个库的启动检查失败或取消时，两池均须关闭并保留原始异常"""
    import asyncio

    from fastapi import FastAPI

    from tinkerfin_studio import resources as resources_module
    from tinkerfin_studio.config.settings import Settings

    settings = Settings.model_validate(
        {
            "s3_storage_access_key": "test-access",
            "s3_storage_secret_key": "test-secret",
            "s3_storage_bucket": "test-attachments",
            "business_database_url": "mysql+asyncmy://u:p@db/business",
            "components_database_url": "mysql+asyncmy://u:p@db/components",
        }
    ).model_copy(
        update={
            "s3_storage_access_key": "test-access",
            "s3_storage_secret_key": "test-secret",
            "s3_storage_bucket": "test-attachments",
            "business_database_url": f"sqlite+aiosqlite:///{tmp_path / 'business.db'}",
            "components_database_url": f"sqlite+aiosqlite:///{tmp_path / 'components.db'}",
        }
    )
    monkeypatch.setattr(resources_module, "get_settings", lambda: settings)
    databases = []
    failure = asyncio.CancelledError() if cancelled else RuntimeError("unavailable")

    async def verify(database, **kwargs):
        assert kwargs["total_pool_capacity"] == 20
        databases.append(database)
        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        if len(databases) == 2:
            raise failure

    monkeypatch.setattr(Database, "verify_connection_budget", verify)
    application = FastAPI()
    with pytest.raises(type(failure)) as caught:
        async with resources_module.build_lifespan()(application):
            pytest.fail("启动失败时不得开放服务")
    assert caught.value is failure
    assert len(databases) == 2
    assert not hasattr(application.state, "resources")
    for database in databases:
        with pytest.raises(RuntimeError, match="尚未启动"):
            database.session()
