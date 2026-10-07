"""项目命名与所有权通过公共接口验证"""

from types import SimpleNamespace

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_notifications import Notifications
from tinkerfin_studio.api.dependencies import get_session, get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.projects.repository import ProjectRepository


async def test_create_rename_and_cross_user_access(
    session: AsyncSession, notifications: Notifications
) -> None:
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(notifications=notifications)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="one", display_name="用户", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/projects")).json()["data"] == []
        created = await client.post("/api/projects", json={"name": " 新能源研究 "})
        assert created.status_code == 200
        project_id = created.json()["data"]["id"]
        assert created.json()["data"]["name"] == "新能源研究"
        assert (
            await client.post("/api/projects", json={"name": "新能源研究"})
        ).status_code == 409
        assert (
            await client.post("/api/projects", json={"name": "   "})
        ).status_code == 422
        renamed = await client.patch(
            f"/api/projects/{project_id}", json={"name": "储能研究"}
        )
        assert renamed.json()["data"]["name"] == "储能研究"
        assert renamed.json()["data"]["id"] == project_id
        foreign = await ProjectRepository(session, 2).create("同名不同用户")
        assert (
            await client.patch(f"/api/projects/{foreign.id}", json={"name": "侵入"})
        ).status_code == 404
        assert [
            item["id"] for item in (await client.get("/api/projects")).json()["data"]
        ] == [project_id]
