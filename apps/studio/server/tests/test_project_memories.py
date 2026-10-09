"""页面记忆与 Agent 文件同源，旧内容不能覆盖新内容"""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import TinkerFin
from tinkerfin_notifications import Notifications
from tinkerfin_studio.api.dependencies import get_session, get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.projects.repository import ProjectRepository

pytestmark = pytest.mark.usefixtures("projects")


@pytest.fixture
async def memory_client(
    session: AsyncSession, notifications: Notifications, persistent_store
):
    app = create_application(lifespan=None)
    configured = TinkerFin(store=persistent_store)
    app.state.resources = SimpleNamespace(
        notifications=notifications, tinkerfin=configured
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="one", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield (
            client,
            configured.with_namespace("ns_1").files(
                ("projects", "project-1", "memories")
            ),
        )


async def test_memory_crud_and_concurrent_agent_change(memory_client):
    client, agent_files = memory_client
    base = "/api/projects/project-1/memories"
    created = await client.post(
        base, json={"path": "/research.md", "content": "长期研究偏好"}
    )
    assert created.status_code == 200
    first = created.json()["data"]
    original = await agent_files.read("/research.md")
    assert original.content.decode() == first["content"]
    assert (
        await client.post(base, json={"path": first["path"], "content": "重复"})
    ).status_code == 409
    changed = await agent_files.update(
        original.path, "Agent 补充".encode(), expected_etag=original.etag
    )
    assert (
        await client.put(
            base + "/file",
            json={
                "path": first["path"],
                "content": "旧页面保存",
                "etag": first["etag"],
            },
        )
    ).status_code == 409
    assert (
        await client.delete(
            base + "/file", params={"path": first["path"], "etag": first["etag"]}
        )
    ).status_code == 409
    current = await client.get(base + "/file", params={"path": first["path"]})
    assert current.json()["data"]["content"] == "Agent 补充"
    saved = await client.put(
        base + "/file",
        json={"path": first["path"], "content": "人工核对", "etag": changed.etag},
    )
    assert saved.status_code == 200
    assert (await agent_files.read(first["path"])).content.decode() == "人工核对"
    assert (
        await client.delete(
            base + "/file",
            params={"path": first["path"], "etag": saved.json()["data"]["etag"]},
        )
    ).status_code == 200
    assert (
        await client.get(base + "/file", params={"path": first["path"]})
    ).status_code == 404


async def test_memory_project_isolation_search_and_nontext_files(
    memory_client, session: AsyncSession
):
    client, files = memory_client
    other = await ProjectRepository(session, 1).create("其他项目")
    await files.create("/facts.md", "公司的简称".encode())
    await files.create("/image.bin", b"\xff\x00")
    base = "/api/projects/project-1/memories"
    matching = await client.get(base, params={"query": "简称"})
    assert [item["path"] for item in matching.json()["data"]["items"]] == ["/facts.md"]
    page = (await client.get(base)).json()["data"]
    binary = next(item for item in page["items"] if item["path"] == "/image.bin")
    assert not binary["editable"]
    assert (
        await client.get(base + "/file", params={"path": "/image.bin"})
    ).status_code == 422
    assert (await client.get(f"/api/projects/{other.id}/memories")).json()["data"][
        "items"
    ] == []
    assert (await client.get("/api/projects/project-2/memories")).status_code == 404
    assert (
        await client.get(
            f"/api/projects/{other.id}/memories/file", params={"path": "/facts.md"}
        )
    ).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../escape.md",
        "/nested/../file.md",
        "/",
        "relative.md",
        "/double//file.md",
        "/back\\slash.md",
        "/line\nfeed.md",
    ],
)
async def test_memory_path_rejects_noncanonical_names(memory_client, path: str):
    client, _ = memory_client
    response = await client.post(
        "/api/projects/project-1/memories", json={"path": path, "content": "text"}
    )
    assert response.status_code == 422
