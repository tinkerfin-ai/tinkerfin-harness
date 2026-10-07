"""个人技能的项目选择、专属技能优先级与运行快照"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_skills_library import add_users

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import InstallSkillRequest, SkillEnabledRequest

pytestmark = pytest.mark.usefixtures("projects")


async def test_project_override_does_not_change_personal_or_other_project(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    other = await ProjectRepository(session, 1).create("另一项目")
    shared = (
        await skill_library.install(
            1,
            InstallSkillRequest(
                request_id="shared",
                source_id="catalog",
                skill_id="author/reports",
                revision="first",
            ),
        )
    ).installation
    await skill_library.set_enabled(
        1,
        shared.id,
        SkillEnabledRequest(request_id="off", project_id="project-1", enabled=False),
    )
    assert (await skill_library.list(1))[0].enabled
    assert not (await skill_library.list(1, project_id="project-1"))[0].enabled
    assert (await skill_library.list(1, project_id=other.id))[0].enabled
    with pytest.raises(BusinessException):
        await skill_library.list(2, project_id="project-1")
    repository = SkillRepository(session, 1)
    snapshot = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="one", run_id="run"),
        project_id="project-1",
    )
    assert not snapshot.skills


async def test_project_name_precedence_and_resume_keep_exact_skill_content(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    shared = (
        await skill_library.install(
            1,
            InstallSkillRequest(
                request_id="shared",
                source_id="catalog",
                skill_id="author/reports",
                revision="first",
            ),
        )
    ).installation
    local = (
        await skill_library.install(
            1,
            InstallSkillRequest(
                request_id="local",
                project_id="project-1",
                source_id="catalog",
                skill_id="author/reports",
                revision="first",
            ),
        )
    ).installation
    assert local.id != shared.id
    assert [item.id for item in await skill_library.list(1)] == [shared.id]
    current = await skill_library.list(1, project_id="project-1")
    assert next(item for item in current if item.id == shared.id).overridden
    repository = SkillRepository(session, 1)
    source = RunIdentity(namespace="ns_1", thread_id="one", run_id="source")
    snapshot = await repository.capture(
        source, project_id="project-1", selected_ids=(local.id,)
    )
    await session.commit()
    assert [item.installation_id for item in snapshot.skills] == [local.id]
    await skill_library.set_enabled(
        1,
        local.id,
        SkillEnabledRequest(request_id="off", project_id="project-1", enabled=False),
    )
    fresh = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="one", run_id="fresh"),
        project_id="project-1",
    )
    assert not fresh.skills
    resumed = await repository.capture(
        RunIdentity(namespace="ns_1", thread_id="one", run_id="resumed"),
        project_id="project-1",
        source=source,
    )
    assert resumed == snapshot
    with pytest.raises(BusinessException) as caught:
        await repository.capture(
            RunIdentity(namespace="ns_1", thread_id="one", run_id="invalid"),
            project_id="project-1",
            selected_ids=(shared.id,),
        )
    assert caught.value.error_code == SkillErrorCode.DISABLED


async def test_running_skill_snapshot_cannot_be_inherited_by_another_project(
    session: AsyncSession,
) -> None:
    await add_users(session)
    destination = await ProjectRepository(session, 1).create("目标项目")
    repository = SkillRepository(session, 1)
    source = RunIdentity(namespace="ns_1", thread_id="thread", run_id="source")
    await repository.capture(source, project_id="project-1")
    await session.commit()
    with pytest.raises(BusinessException):
        await repository.capture(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="resume"),
            project_id=destination.id,
            source=source,
        )
