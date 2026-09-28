"""技能 HTTP 操作接通完整导入与个人安装生命周期"""

from types import SimpleNamespace

import httpx
import pytest
from skill_fakes import MemorySkillSource
from sqlalchemy.ext.asyncio import AsyncSession
from test_skills_library import add_users, archive_bytes, damaged_archive, skill_files

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.dependencies import get_session, get_user_context
from tinkerfin_studio.api.errors import SkillErrorCode
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import SkillFile, parse_package
from tinkerfin_studio.skills.repository import SkillRepository


@pytest.mark.parametrize("kind", ["zip", "github"])
async def test_invalid_skill_import_returns_a_package_error(
    skill_library: SkillLibrary, kind: str
) -> None:
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(skills=skill_library)
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="one", display_name="用户", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        if kind == "zip":
            response = await client.post(
                "/api/skills/imports/zip",
                content=damaged_archive(),
                headers={"content-type": "application/zip"},
            )
        else:
            response = await client.post(
                "/api/skills/imports/github", json={"url": "https://[github.com/a/b"}
            )
    assert response.status_code == 422
    assert response.json()["code"] == int(SkillErrorCode.INVALID_PACKAGE)


async def test_zip_preview_confirm_details_toggle_uninstall_and_owner_scope(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(skills=skill_library)
    user = UserContext(
        user_id=1, username="one", display_name="用户", roles=(), disabled=False
    )
    app.dependency_overrides[get_user_context] = lambda: user
    app.dependency_overrides[get_session] = lambda: session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        preview = await client.post(
            "/api/skills/imports/zip",
            content=archive_bytes(skill_files()),
            headers={"content-type": "application/zip"},
        )
        assert preview.status_code == 200
        draft = preview.json()["data"]
        confirmed = await client.post(
            f"/api/skills/imports/{draft['id']}/confirm",
            json={
                "request_id": "import",
                "digests": [draft["candidates"][0]["digest"]],
            },
        )
        assert confirmed.status_code == 200
        installation_id = confirmed.json()["data"][0]
        conversations = ConversationRepository(session)
        thread = await conversations.create_thread(
            user_id=1, thread_id="thread", title="技能", model_id=None
        )
        await conversations.create_run_registration(
            thread_id=thread.id,
            run_id="run",
            parent_run_id=None,
            model_id="main",
            input_json={"forwardedProps": {"skillIds": [installation_id]}},
        )
        await SkillRepository(session, 1).capture(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="run"),
            selected_ids=(installation_id,),
        )
        await session.commit()
        listing = (await client.get("/api/skills/installations")).json()["data"]
        assert len(listing) == 1 and listing[0]["enabled"]
        detail = (
            await client.get(f"/api/skills/installations/{installation_id}")
        ).json()["data"]
        assert "references/data.bin" in detail["files"] and "enabled" not in detail
        toggle = await client.patch(
            f"/api/skills/installations/{installation_id}",
            json={"request_id": "disable", "enabled": False},
        )
        assert toggle.status_code == 200 and not toggle.json()["data"]["enabled"]
        user = UserContext(
            user_id=2, username="two", display_name="用户", roles=(), disabled=False
        )
        assert (
            await client.get("/api/skills/selection?thread_id=thread&run_id=run")
        ).status_code == 404
        assert (
            await client.request(
                "DELETE",
                f"/api/skills/installations/{installation_id}",
                json={"request_id": "uninstall"},
            )
        ).status_code == 404
        user = UserContext(
            user_id=1, username="one", display_name="用户", roles=(), disabled=False
        )
        assert (
            await client.request(
                "DELETE",
                f"/api/skills/installations/{installation_id}",
                json={"request_id": "uninstall"},
            )
        ).status_code == 200
        assert (await client.get("/api/skills/installations")).json()["data"] == []
        selection = await client.get(
            "/api/skills/selection?thread_id=thread&run_id=run"
        )
        assert selection.status_code == 200
        assert selection.json()["data"] == [{"id": installation_id, "name": "reports"}]


async def test_catalog_update_http_replays_committed_result_and_validates_commands(
    session: AsyncSession,
    skill_library: SkillLibrary,
    skill_catalog: MemorySkillSource,
) -> None:
    await add_users(session)
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(skills=skill_library)
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="one", display_name="用户", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/skills/sources")).json()["data"][0][
            "id"
        ] == "catalog"
        assert (
            len(
                (await client.get("/api/skills/catalog?source_id=catalog")).json()[
                    "data"
                ]["items"]
            )
            == 1
        )
        detail = (
            await client.get(
                "/api/skills/catalog/detail?source_id=catalog&skill_id=author/reports"
            )
        ).json()["data"]
        assert detail["skill"]["revision"] == "first"
        payload = {
            "request_id": "install",
            "source_id": "catalog",
            "skill_id": "author/reports",
            "revision": "first",
        }
        installed = (
            await client.post("/api/skills/installations", json=payload)
        ).json()["data"]
        assert installed["source_kind"] == "catalog"
        update_url = f"/api/skills/installations/{installed['id']}/update"
        assert (await client.post(update_url, json={})).status_code == 422
        skill_catalog.packages["second"] = parse_package(
            (*skill_files(), SkillFile("updated", b"new"))
        )
        skill_catalog.revision = "second"
        result = await client.post(update_url, json={"request_id": "update"})
        assert result.status_code == 200 and result.json()["data"]["changed"]
        downloads = skill_catalog.downloads
        assert (
            await client.post(update_url, json={"request_id": "update"})
        ).json() == result.json()
        assert skill_catalog.downloads == downloads
        assert (
            await client.post(update_url, json={"request_id": "install"})
        ).status_code == 409
        assert (
            await client.patch(
                f"/api/skills/installations/{installed['id']}",
                json={"request_id": "disable", "enabled": "false"},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/api/skills/installations", json={**payload, "user_id": 2}
            )
        ).status_code == 422
