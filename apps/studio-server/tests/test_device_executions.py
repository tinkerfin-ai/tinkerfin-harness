"""云端运行的本机授权、账号隔离与项目映射"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from device_fakes import SOURCE, create_execution
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tinkerfin_studio.api.dependencies import get_network_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.devices.models import Device


@pytest.fixture
async def execution_api(database, components_database, notifications):
    execution = await create_execution(database)
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(
        database=database,
        components_database=components_database,
        notifications=notifications,
    )
    app.dependency_overrides[get_network_user_context] = lambda: UserContext(
        user_id=1, username="owner", roles=(), disabled=False
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://studio.test"
    ) as client:
        yield client, app, execution


async def test_configuration_contains_only_the_authorized_workspace(execution_api):
    client, _app, execution = execution_api
    path = f"/api/devices/executions/{execution.binding.profile_id}/configuration"
    headers = {"Authorization": "Bearer " + execution.token}
    params = {"thread_id": SOURCE.thread_id, "run_id": SOURCE.run_id}
    response = await client.get(path, headers=headers, params=params)
    assert response.status_code == 200
    assert response.json()["data"] == {
        "authorization": execution.binding.profile_id,
        "workspace_id": "project",
        "skills": [],
    }
    for invalid in ({"thread_id": "other"}, {"run_id": "other"}):
        denied = await client.get(path, headers=headers, params={**params, **invalid})
        assert denied.status_code == 404


async def test_retry_selection_preserves_original_device_after_revocation(
    execution_api,
):
    client, app, execution = execution_api
    params = {"thread_id": SOURCE.thread_id, "run_id": SOURCE.run_id}
    before = await client.get("/api/devices/selection", params=params)
    assert before.status_code == 200
    assert before.json()["data"] == {
        "device_id": execution.binding.device_id,
        "workspace_id": execution.binding.workspace_id,
    }
    await execution.devices.revoke(1, execution.binding.device_id)
    after = await client.get("/api/devices/selection", params=params)
    assert after.json() == before.json()
    assert (
        await client.get(
            "/api/devices/selection", params={**params, "run_id": "missing"}
        )
    ).status_code == 404
    app.dependency_overrides[get_network_user_context] = lambda: UserContext(
        user_id=2, username="other", roles=(), disabled=False
    )
    assert (
        await client.get("/api/devices/selection", params=params)
    ).status_code == 404


@pytest.mark.parametrize(
    "revocation",
    [
        "conversation",
        "archive",
        "device",
        "workspace",
        "user",
        "completed",
        "cancelled",
    ],
)
async def test_changed_authority_prevents_new_device_operations(
    execution_api, database, revocation
):
    client, _app, execution = execution_api
    async with database.session() as session:
        thread = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.thread_id == SOURCE.thread_id
            )
        )
        assert thread is not None
        if revocation == "conversation":
            await session.delete(thread)
        elif revocation == "archive":
            thread.archived = True
        elif revocation == "workspace":
            thread.project_id = "other-project"
        elif revocation == "user":
            user = await session.get(User, 1)
            assert user is not None
            user.disabled = True
        elif revocation == "device":
            device = await session.get(Device, execution.binding.device_id)
            assert device is not None
            device.revoked_at = datetime(2030, 1, 1)
        else:
            run = await session.scalar(
                select(ConversationRunRegistration).where(
                    ConversationRunRegistration.run_id == SOURCE.run_id
                )
            )
            assert run is not None
            run.status = "succeeded" if revocation == "completed" else "cancelled"
        await session.commit()
    response = await client.get(
        f"/api/devices/executions/{execution.binding.profile_id}/configuration",
        headers={"Authorization": "Bearer " + execution.token},
        params={"thread_id": SOURCE.thread_id, "run_id": SOURCE.run_id},
    )
    assert response.status_code in {401, 404, 409}


async def test_device_project_pages_are_scoped_and_new_projects_need_no_reconnection(
    execution_api, database
):
    from uuid import UUID

    from tinkerfin_studio.projects.models import Project

    client, _app, execution = execution_api
    now = datetime(2030, 1, 1)
    identifiers = [str(UUID(int=index)) for index in range(1, 130)]
    async with database.session() as session:
        session.add_all(
            [
                Project(id=key, user_id=1, name=key, created_at=now, updated_at=now)
                for key in identifiers
            ]
        )
        session.add(
            Project(
                id="other-owner",
                user_id=2,
                name="Private",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    headers = {"Authorization": "Bearer " + execution.token}
    first = await client.get("/api/devices/projects", headers=headers)
    page = first.json()["data"]
    assert (
        first.status_code == 200 and len(page["items"]) == 128 and page["next_cursor"]
    )
    second = await client.get(
        "/api/devices/projects", params={"after": page["next_cursor"]}, headers=headers
    )
    items = page["items"] + second.json()["data"]["items"]
    assert len(items) == 130 and "other-owner" not in {
        item["project_id"] for item in items
    }
    prepared = await execution.devices.require_workspace(
        1, execution.binding.device_id, identifiers[0]
    )
    assert prepared.project_id == identifiers[0]
    denied = await client.get("/api/devices/projects/other-owner", headers=headers)
    assert denied.json()["code"] != 0
    async with database.session() as session:
        project = await session.get(Project, identifiers[0])
        assert project is not None
        project.name = "Renamed"
        await session.commit()
    renamed = await execution.devices.require_workspace(
        1, execution.binding.device_id, identifiers[0]
    )
    assert renamed.project_id == prepared.project_id and renamed.name == "Renamed"


async def test_replacement_device_is_used_for_new_turn_but_not_old_approval(
    execution_api, database
):
    from pydantic import SecretStr

    from tinkerfin_contracts import RunIdentity
    from tinkerfin_studio.api.errors import BusinessException
    from tinkerfin_studio.devices.profiles import DeviceProfiles
    from tinkerfin_studio.devices.schemas import (
        AuthorizationStart,
        LocalTargetSelection,
    )
    from tinkerfin_studio.skills.schemas import SkillSnapshotPayload

    client, _app, execution = execution_api
    secret = "cd" * 32
    request = await execution.devices.create_authorization(
        AuthorizationStart(name="New Mac", credential=SecretStr(secret))
    )
    approval = await execution.devices.approve_authorization(
        1, request.authorization_id, request.browser_token, 1
    )
    assert approval.revoked_device_ids == (execution.binding.device_id,)
    repeated = await execution.devices.approve_authorization(
        1, request.authorization_id, request.browser_token, 1
    )
    assert repeated.revoked_device_ids == approval.revoked_device_ids
    paired = await execution.devices.authorization_status(
        request.authorization_id, secret
    )
    assert paired.device is not None
    target = LocalTargetSelection(
        device_id=paired.device.device_id, workspace_id="project"
    )
    current = await client.get(
        "/api/devices/selection", params={"thread_id": SOURCE.thread_id}
    )
    original = await client.get(
        "/api/devices/selection",
        params={"thread_id": SOURCE.thread_id, "run_id": SOURCE.run_id},
    )
    assert current.json()["data"] == target.model_dump()
    assert original.json()["data"]["device_id"] == execution.binding.device_id
    async with database.session() as session:
        profiles = DeviceProfiles(session, 1)
        new = await profiles.capture(
            identity=RunIdentity(
                namespace=SOURCE.namespace, thread_id=SOURCE.thread_id, run_id="next"
            ),
            selection=target,
            access_mode="write_approval",
            skills=SkillSnapshotPayload(skills=()),
        )
        assert new is not None and new.device_id == target.device_id
        with pytest.raises(BusinessException):
            await profiles.capture(
                identity=RunIdentity(
                    namespace=SOURCE.namespace,
                    thread_id=SOURCE.thread_id,
                    run_id="resume",
                ),
                selection=target,
                access_mode="write_approval",
                skills=SkillSnapshotPayload(skills=()),
                inherited=execution.binding.profile_id,
            )
        retained = await profiles.capture(
            identity=RunIdentity(
                namespace=SOURCE.namespace,
                thread_id=SOURCE.thread_id,
                run_id="resume-original",
            ),
            selection=None,
            access_mode="write_approval",
            skills=SkillSnapshotPayload(skills=()),
            inherited=execution.binding.profile_id,
        )
        assert retained == execution.binding
