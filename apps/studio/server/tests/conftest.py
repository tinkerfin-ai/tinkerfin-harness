from collections.abc import AsyncIterator

import pytest
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
async def components_database(tmp_path) -> AsyncIterator[Database]:
    """组件在独立的空库中通过自身入口初始化"""
    async with Database(
        f"sqlite+aiosqlite:///{tmp_path / 'components.db'}"
    ) as resource:
        yield resource


@pytest_asyncio.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """提供测试独占的异步 Session"""

    async with database.session() as value:
        yield value


@pytest_asyncio.fixture
async def attachments(database, attachment_storage):
    """提供使用内存对象存储和业务仓储的附件服务"""
    from tinkerfin_studio.attachments.service import AttachmentService

    return AttachmentService(database, attachment_storage)


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

    from deepagents.backends.protocol import FileUploadResponse

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
