from collections.abc import AsyncIterator
from datetime import UTC, datetime, tzinfo
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_notifications import Notifications
from tinkerfin_studio.infrastructure.database import Base, Database


@pytest.fixture
def fixed_utc_time(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """固定业务期限判断，避免运行速度和自然时间影响过期场景"""
    current = datetime(2030, 1, 1, 12, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz)

    for module in (
        "tinkerfin_studio.auth.service",
        "tinkerfin_studio.auth.repository",
        "tinkerfin_studio.attachments.service",
        "tinkerfin_studio.conversation.repository",
        "tinkerfin_studio.conversation.coordinator",
    ):
        monkeypatch.setattr(f"{module}.datetime", FixedDateTime)
    return current


@pytest_asyncio.fixture
async def notifications() -> AsyncIterator[Notifications]:
    """每项测试独占的通知服务，退出时等待全部自有任务关闭"""
    async with Notifications() as service:
        yield service


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    """提供每个测试独占的 SQLite 数据库"""

    resource = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")
    async with resource:
        async with resource.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield resource


@pytest_asyncio.fixture
async def components_database(tmp_path) -> AsyncIterator[Database]:
    """组件在独立的空库中通过自身入口初始化"""
    async with Database(
        f"sqlite+aiosqlite:///{tmp_path / 'components.db'}"
    ) as resource:
        yield resource


@pytest_asyncio.fixture
async def persistent_store(components_database: Database):
    """持久文件测试使用支持条件写入的独立存储"""
    from tinkerfin_langgraph_store import SqlAlchemyStore

    async with SqlAlchemyStore(components_database.engine) as store:
        yield store


@pytest_asyncio.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """提供测试独占的异步 Session"""

    async with database.session() as value:
        yield value


@pytest_asyncio.fixture
async def attachments(notifications, database, attachment_storage):
    """提供使用内存对象存储和业务仓储的附件服务"""
    from tinkerfin_studio.attachments.service import AttachmentService

    return AttachmentService(database, attachment_storage, notifications=notifications)


@pytest.fixture
def skill_catalog():
    from skill_fakes import MemorySkillSource

    from tinkerfin_studio.skills.packages import SkillFile, parse_package

    return MemorySkillSource(
        {
            "first": parse_package(
                (
                    SkillFile(
                        "SKILL.md",
                        b"---\nname: reports\ndescription: Reports\n---\nOriginal",
                    ),
                )
            )
        }
    )


@pytest_asyncio.fixture
async def skill_library(database, attachments, notifications, skill_catalog):
    import httpx
    from langgraph.store.memory import InMemoryStore

    from tinkerfin import TinkerFin
    from tinkerfin_studio.skills.content import SkillContentStore
    from tinkerfin_studio.skills.downloads import GitHubSkillImporter
    from tinkerfin_studio.skills.library import SkillLibrary
    from tinkerfin_studio.skills.packages import SkillArchiveReader
    from tinkerfin_studio.skills.sources import SkillSources

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        archives = SkillArchiveReader()
        yield SkillLibrary(
            database,
            SkillContentStore(TinkerFin(store=InMemoryStore())),
            sources=SkillSources([skill_catalog]),
            archives=archives,
            github=GitHubSkillImporter(client, archives),
            attachments=attachments,
            notifications=notifications,
        )


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


@pytest.fixture(autouse=True)
def attachment_settings_environment(monkeypatch):
    """测试配置使用独立占位凭据，不读取真实存储配置"""
    monkeypatch.setenv("S3_STORAGE_BUCKET", "test-attachments")
    monkeypatch.setenv("S3_STORAGE_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_STORAGE_SECRET_KEY", "test-secret")


@pytest.fixture
def attachment_storage():
    from attachment_fakes import MemoryAttachmentStorage

    return MemoryAttachmentStorage()


@pytest.fixture
def work_file_runtime():
    """以隔离内存文件验证工作区读写和工具交付，不连接共享沙箱"""
    from unittest.mock import AsyncMock, MagicMock

    from deepagents.backends.protocol import DeleteResult, FileUploadResponse

    from tinkerfin.tools import ToolRuntime
    from tinkerfin_sandbox import OpenSandboxFileTooLargeError, RootedOpenSandboxBackend

    files: dict[str, bytes] = {}
    workspace = MagicMock(spec=RootedOpenSandboxBackend)

    async def upload(items):
        for path, data in items:
            files[path] = data
        return [FileUploadResponse(path=path, error=None) for path, _ in items]

    async def read(path, *, max_bytes):
        data = files[path]
        if len(data) > max_bytes:
            raise OpenSandboxFileTooLargeError("too large")
        return data

    workspace.aupload_files = AsyncMock(side_effect=upload)
    workspace.aread_bytes = AsyncMock(side_effect=read)

    async def delete(path):
        if path not in files:
            return DeleteResult(error="not found")
        del files[path]
        return DeleteResult(path=path)

    workspace.adelete = AsyncMock(side_effect=delete)
    workspace.to_shell_path.side_effect = lambda path: path.lstrip("/")

    class WorkspaceRuntime(ToolRuntime[None, RootedOpenSandboxBackend]):
        @property
        def workspace(self) -> RootedOpenSandboxBackend:
            return workspace

    runtime = WorkspaceRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda value: None,
        tool_call_id=None,
        store=None,
    )
    return runtime, workspace, files


@pytest.fixture(scope="module")
def large_image():
    import io
    import random

    from deepagents.backends.sandbox import MAX_BINARY_BYTES
    from PIL import Image

    from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES

    output = io.BytesIO()
    Image.frombytes(
        "RGB", (1024, 1024), random.Random(0).randbytes(1024 * 1024 * 3)
    ).save(output, "PNG")
    data = output.getvalue()
    assert MAX_BINARY_BYTES < len(data) <= MAX_FILE_BYTES
    return data


@pytest_asyncio.fixture
async def projects(database: Database) -> None:
    """为项目归属测试提供独立的稳定项目"""
    from tinkerfin_studio.projects.models import Project

    async with database.session() as session:
        for user_id in (1, 2, 7):
            session.add(
                Project(
                    id=f"project-{user_id}",
                    user_id=user_id,
                    name="默认测试项目",
                    created_at=datetime(2030, 1, 1),
                    updated_at=datetime(2030, 1, 1),
                )
            )
        await session.commit()
