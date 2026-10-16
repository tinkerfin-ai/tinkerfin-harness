"""同一用户的不同项目独立管理自动化任务"""

import pytest
from test_automation_integration import (
    automation_environment,
    automation_resources,
    automation_worker,
    configuration,
)

from tinkerfin_automation.errors import TaskNotFoundError
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.automation.schemas import SaveTask, TaskCommand, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.projects.repository import ProjectRepository

__all__ = ["automation_environment", "automation_resources", "automation_worker"]


async def test_automation_pages_and_commands_are_project_owned(
    automation_resources,
):
    resources = automation_resources
    async with resources.database.session() as session:
        project = await ProjectRepository(session, 1).create("第二项目")
    first = StudioAutomationService(resources, user_id=1, project_id="project-1")
    second = StudioAutomationService(resources, user_id=1, project_id=project.id)
    a = await first.save(
        SaveTask(
            request_id="same-request",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    b = await second.save(
        SaveTask(
            request_id="same-request",
            configuration=TaskConfiguration.model_validate(
                {**configuration(), "projectId": project.id}
            ),
        )
    )
    assert a.id != b.id
    assert [item.id for item in (await first.list_tasks(limit=1)).items] == [a.id]
    assert [item.id for item in (await second.list_tasks(limit=1)).items] == [b.id]
    with pytest.raises(TaskNotFoundError):
        await first.get_task(b.id)
    with pytest.raises(BusinessException):
        await first.save(
            SaveTask(
                request_id="change-project",
                expected_revision=a.revision,
                configuration=TaskConfiguration.model_validate(
                    {**configuration(), "projectId": project.id}
                ),
            ),
            task_id=a.id,
        )


async def test_active_execution_pages_and_status_authorize_each_project(
    automation_resources, monkeypatch
):
    import httpx

    from tinkerfin_automation import Automation
    from tinkerfin_studio.api.dependencies import get_user_context
    from tinkerfin_studio.application import create_application
    from tinkerfin_studio.auth.types import UserContext

    resources = automation_resources
    async with resources.database.session() as session:
        second = await ProjectRepository(session, 1).create("活跃任务项目")
    automation = Automation()
    automation.remote_target("job", execution_namespace="active")
    monkeypatch.setattr(resources, "automation", automation)
    app = create_application(lifespan=None)
    app.state.resources = resources
    user = UserContext(user_id=1, username="owner", roles=(), disabled=False)
    app.dependency_overrides[get_user_context] = lambda: user
    async with (
        automation,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        first = await automation.for_owner("1:project-1").run("job")
        other = await automation.for_owner(f"1:{second.id}").run("job")
        foreign = await automation.for_owner("2:project-2").run("job")
        response = await client.get("/api/automation/runs/active", params={"limit": 1})
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        items = page["items"]
        cursor = page["nextCursor"]
        assert cursor
        user = UserContext(user_id=2, username="other", roles=(), disabled=False)
        assert (
            await client.get("/api/automation/runs/active", params={"cursor": cursor})
        ).status_code == 422
        user = UserContext(user_id=1, username="owner", roles=(), disabled=False)
        while cursor:
            response = await client.get(
                "/api/automation/runs/active", params={"cursor": cursor, "limit": 1}
            )
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            items.extend(page["items"])
            cursor = page["nextCursor"]
        assert {item["id"] for item in items} == {first.id, other.id}
        assert {item["projectId"] for item in items} == {"project-1", second.id}
        status = await client.get(
            f"/api/automation/runs/{first.id}/status", params={"projectId": "project-1"}
        )
        assert status.status_code == 200, status.text
        assert status.json()["data"]["status"] == "queued"
        assert status.json()["data"]["conversation"] == {"state": "not_created"}
        assert "messages" not in status.json()["data"]
        assert (
            await client.get(
                f"/api/automation/runs/{foreign.id}/status",
                params={"projectId": "project-1"},
            )
        ).status_code == 404


async def test_run_conversation_tracks_move_and_deletion_without_losing_results(
    automation_resources, automation_worker
):
    from datetime import datetime

    from tinkerfin_automation.errors import ExecutionNotFoundError
    from tinkerfin_studio.conversation.repository import ConversationRepository

    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="navigation",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    queued = await service.task_command(
        task.id, "run", TaskCommand(request_id="run", expected_revision=task.revision)
    )
    assert queued is not None
    await automation_worker.wait_until_idle()
    original = await service.result(queued.id)
    assert original.conversation.state == "available"
    assert original.conversation.thread_id == original.thread_id
    assert original.conversation.project_id == "project-1"
    async with resources.database.session() as session:
        destination = await ProjectRepository(session, 1).create("会话归属项目")
        thread = await ConversationRepository(session).get_thread(
            user_id=1, thread_id=original.thread_id
        )
        assert thread is not None
        thread.project_id = destination.id
        await session.commit()
    moved = await service.status(queued.id)
    assert moved.project_id == "project-1"
    assert moved.conversation.state == "available"
    assert moved.conversation.project_id == destination.id
    async with resources.database.session() as session:
        thread = await ConversationRepository(session).get_thread(
            user_id=1, thread_id=original.thread_id
        )
        assert thread is not None
        thread.deleted_at = datetime(2030, 1, 1)
        await session.commit()
    deleted = await service.result(queued.id)
    assert deleted.conversation.model_dump() == {"state": "deleted"}
    assert (await service.status(queued.id)).conversation.state == "deleted"
    assert deleted.messages == original.messages
    assert deleted.output_files == original.output_files
    with pytest.raises(ExecutionNotFoundError):
        await StudioAutomationService(
            resources, user_id=2, project_id="project-2"
        ).status(queued.id)
