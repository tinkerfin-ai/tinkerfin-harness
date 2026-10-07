"""同一用户的不同项目独立管理自动化任务"""

import pytest
from test_automation_integration import (
    automation_environment,
    automation_resources,
    configuration,
)

from tinkerfin_automation import TaskStatus
from tinkerfin_automation.errors import TaskNotFoundError
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.projects.repository import ProjectRepository

__all__ = ["automation_environment", "automation_resources"]


async def test_automation_pages_counts_and_commands_are_project_owned(
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
    assert (await first.task_counts())[TaskStatus.ENABLED] == 1
    assert (await second.task_counts())[TaskStatus.ENABLED] == 1
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
