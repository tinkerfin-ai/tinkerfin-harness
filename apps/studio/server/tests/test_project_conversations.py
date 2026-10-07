"""项目筛选、会话整理与附件归属"""

from unittest.mock import create_autospec

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_notifications import Notifications
from tinkerfin_studio.api.errors import BusinessException, ProjectErrorCode
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.conversation.command import ConversationCommandService
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import Tracer

pytestmark = pytest.mark.usefixtures("projects")


async def test_history_filters_before_pagination_and_rejects_cross_project_cursor(
    session: AsyncSession,
) -> None:
    repository = ConversationRepository(session)
    second = await ProjectRepository(session, 1).create("另一项目")
    for thread_id, project_id, user_id in (
        ("one", "project-1", 1),
        ("two", "project-1", 1),
        ("different", second.id, 1),
        ("foreign", "project-2", 2),
    ):
        thread = await repository.create_thread(
            user_id=user_id,
            project_id=project_id,
            thread_id=thread_id,
            title="研究",
            model_id="main",
        )
        thread.last_run_id = thread_id
    await repository.commit()
    history = ConversationHistoryService(
        repository, user_id=1, tracer=Tracer(), history_queries=HistoryQueryAdmission()
    )
    page = await history.list_history(project_id="project-1", page_size=1, cursor=None)
    assert [item.thread_id for item in page.items] == ["two"]
    assert page.next_cursor
    with pytest.raises(BusinessException):
        await history.list_history(
            project_id=second.id, page_size=1, cursor=page.next_cursor
        )
    all_projects = await history.list_history(
        project_id=None, page_size=10, cursor=None
    )
    assert {item.thread_id for item in all_projects.items} == {
        "one",
        "two",
        "different",
    }


async def test_archive_restore_and_move_preserve_conversation_and_attachments(
    session: AsyncSession,
    notifications: Notifications,
) -> None:
    repository = ConversationRepository(session)
    destination = await ProjectRepository(session, 1).create("目的项目")
    thread = await repository.create_thread(
        user_id=1,
        project_id="project-1",
        thread_id="thread",
        title="会话",
        model_id="main",
    )
    thread.last_run_id = "run"
    file = AttachmentFile(
        id="file",
        user_id=1,
        project_id="project-1",
        thread_id="thread",
        name="研究.txt",
        mime_type="text/plain",
        size_bytes=4,
        sha256="digest",
        status="ready",
        source="user",
    )
    session.add(file)
    await repository.commit()
    resources = create_autospec(ApplicationResources, instance=True)
    resources.configure_mock(notifications=notifications)
    service = ConversationCommandService(repository, user_id=1, resources=resources)
    archived = await service.update(
        thread_id="thread", title=None, pinned=None, archived=True
    )
    assert archived.archived
    assert not await repository.list_threads(
        user_id=1, project_id="project-1", page_size=10, cursor=None
    )
    assert (
        len(
            await repository.list_threads(
                user_id=1,
                project_id="project-1",
                archived=True,
                page_size=10,
                cursor=None,
            )
        )
        == 1
    )
    restored = await service.update(
        thread_id="thread", title=None, pinned=None, archived=False
    )
    assert not restored.archived
    moved = await service.update(
        thread_id="thread", title=None, pinned=None, project_id=destination.id
    )
    await session.refresh(file)
    assert moved.thread_id == "thread"
    assert moved.project_id == file.project_id == destination.id
    assert thread.last_run_id == "run"
    with pytest.raises(BusinessException) as caught:
        await service.update(
            thread_id="thread", title=None, pinned=None, project_id="project-2"
        )
    assert caught.value.error_code == ProjectErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "state", ["running", "waiting_approval", "preparing", "starting"]
)
async def test_active_conversation_cannot_move_or_archive(
    session: AsyncSession, state: str
) -> None:
    repository = ConversationRepository(session)
    destination = await ProjectRepository(session, 1).create("目的项目")
    thread = await repository.create_thread(
        user_id=1,
        project_id="project-1",
        thread_id="thread",
        title="会话",
        model_id="main",
    )
    if state in {"preparing", "starting"}:
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id="run",
            parent_run_id=None,
            model_id="main",
            input_json={},
        )
        run.status = state
    else:
        thread.status = state
    await repository.commit()
    for project_id, archived in ((destination.id, None), (None, True)):
        with pytest.raises(BusinessException) as caught:
            await repository.organize_thread(
                user_id=1, thread_id="thread", project_id=project_id, archived=archived
            )
        assert caught.value.error_code == ProjectErrorCode.ACTIVE_CONVERSATION
    assert thread.project_id == "project-1"
    assert not thread.archived
