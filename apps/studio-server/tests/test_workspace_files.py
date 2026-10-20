"""项目文件只读入口的授权、预览边界与监听资源归属"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.types import Message
from test_studio_notifications import notification_route as notification_route

from tinkerfin_sandbox import (
    OpenSandboxNotTextError,
    OpenSandboxWorkspaceNotInitializedError,
    WorkspaceDirectoryPage,
    WorkspaceFileInfo,
    WorkspaceText,
)
from tinkerfin_studio.api.dependencies import (
    get_session,
    get_user_context,
)
from tinkerfin_studio.api.errors import (
    BusinessException,
    GlobalErrorCode,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.api.workspace_router import follow_files
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import get_resources
from tinkerfin_studio.workspace.schemas import (
    WorkspaceDirectoryView,
)


class ProjectFiles:
    """提供框架公开查询的有界结果，测试不访问共享沙箱"""

    def __init__(self) -> None:
        self.uninitialized = False
        self.failure: Exception | None = None
        self.reads: list[tuple[str, int, int]] = []
        self.closed = False
        self.changes: asyncio.Queue[str] = asyncio.Queue(2)

    async def get_file_info(self, path: str) -> WorkspaceFileInfo:
        if self.failure:
            raise self.failure
        if self.uninitialized:
            raise OpenSandboxWorkspaceNotInitializedError("absent")
        if path == "/missing.txt":
            raise FileNotFoundError(path)
        return WorkspaceFileInfo(
            path,
            PurePosixPath(path).name,
            "directory" if path == "/" else "file",
            None if path == "/" else 12,
            datetime(2026, 10, 7, tzinfo=UTC),
            "a" * 64,
        )

    async def list_directory(
        self, path: str, *, limit: int, cursor: str | None
    ) -> WorkspaceDirectoryPage:
        await self.get_file_info(path)
        assert limit == 200
        return WorkspaceDirectoryPage(
            path, (await self.get_file_info("/note.py"),), cursor
        )

    async def read_text(
        self, path: str, *, max_bytes: int, max_lines: int
    ) -> WorkspaceText:
        self.reads.append((path, max_bytes, max_lines))
        if path == "/unknown":
            raise OpenSandboxNotTextError("binary")
        return WorkspaceText(await self.get_file_info(path), "print('hello')", True)

    @asynccontextmanager
    async def watch(self) -> AsyncGenerator[AsyncIterator[str], None]:
        async def changes() -> AsyncGenerator[str, None]:
            while True:
                yield await self.changes.get()

        stream = changes()
        try:
            yield stream
        finally:
            await stream.aclose()
            self.closed = True


class Workspaces:
    def __init__(self, files: ProjectFiles) -> None:
        self.files = files
        self.selected: list[tuple[str, str]] = []

    def workspace(self, owner: str, *, workspace_key: str) -> ProjectFiles:
        self.selected.append((owner, workspace_key))
        return self.files


@pytest.fixture
async def file_client(session):
    project = await ProjectRepository(session, 1).create("研究")
    foreign = await ProjectRepository(session, 2).create("另一项目")
    files = ProjectFiles()
    manager = Workspaces(files)
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(sandbox_manager=manager)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        1, "one", (), False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, project.id, foreign.id, files, manager


async def test_directory_authorization_and_uninitialized_state(file_client) -> None:
    client, project, foreign, files, manager = file_client
    forbidden = await client.get(f"/api/projects/{foreign}/workspace/entries")
    assert forbidden.status_code == 404 and manager.selected == []
    response = await client.get(f"/api/projects/{project}/workspace/entries")
    page = (
        ApiResponse[WorkspaceDirectoryView].model_validate_json(response.content).data
    )
    assert page is not None and page.entries[0].path == "/note.py"
    assert manager.selected == [("users/1", project)]
    assert response.headers["Cache-Control"] == "private, no-store"
    files.uninitialized = True
    response = await client.get(f"/api/projects/{project}/workspace/entries")
    page = (
        ApiResponse[WorkspaceDirectoryView].model_validate_json(response.content).data
    )
    assert page is not None and page.state == "uninitialized" and not page.entries


async def test_disconnected_watch_preflight_discards_the_prepared_response(
    notification_route, monkeypatch
) -> None:
    request, auth, _clock, _records, _connections = notification_route
    resources = get_resources(request.app)
    files = ProjectFiles()
    monkeypatch.setattr(resources, "sandbox_manager", Workspaces(files), raising=False)
    entering, admitted = asyncio.Event(), asyncio.Event()
    disconnected = False

    @asynccontextmanager
    async def watch() -> AsyncGenerator[AsyncIterator[str], None]:
        try:
            entering.set()
            await admitted.wait()
            async with ProjectFiles.watch(files) as changes:
                yield changes
        finally:
            files.closed = True

    monkeypatch.setattr(files, "watch", watch)

    async def receive() -> Message:
        return (
            {"type": "http.disconnect"}
            if disconnected
            else {"type": "http.request", "body": b"", "more_body": False}
        )

    request = Request(request.scope, receive)
    async with resources.database.session() as session:
        project = await ProjectRepository(session, auth.user.user_id).create("断连检查")
        task = asyncio.create_task(follow_files(project.id, request, session, auth))
        await entering.wait()
        disconnected = True
        assert await request.is_disconnected()
        admitted.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert files.closed


async def test_workspace_stream_checks_access_and_closes_its_watch(
    notification_route,
) -> None:
    request, auth, clock, records, connections = notification_route
    resources = get_resources(request.app)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(request.scope, receive=receive)
    files = ProjectFiles()
    manager = Workspaces(files)
    request.app.state.resources.sandbox_manager = manager
    async with resources.database.session() as session:
        project = await ProjectRepository(session, 7).create("浏览测试")
        project_id = project.id
        response = await follow_files(project_id, request, session, auth)
    body = aiter(response.body_iterator)
    try:
        assert await anext(body) == b"event: ready\ndata: {}\n\n"
        assert connections == [0]
        files.changes.put_nowait("files_changed")
        changed = await anext(body)
        assert isinstance(changed, bytes) and b"files_changed" in changed
        records[auth.token] = replace(records[auth.token], revoked=True)
        clock.monotonic = 15.0
        with pytest.raises(StopAsyncIteration):
            await anext(body)
    finally:
        await response.aclose()
    assert files.closed and connections == [0]
    assert manager.selected == [("users/7", project_id)]
    async with resources.database.session() as session:
        with pytest.raises(BusinessException) as failure:
            await follow_files(project_id, request, session, auth)
    assert failure.value.error_code is GlobalErrorCode.UNAUTHORIZED
