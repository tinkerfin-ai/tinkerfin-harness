"""本机会话固定项目、授权快照清理及事务归属"""

import asyncio
from datetime import datetime

import pytest
from device_fakes import SOURCE, create_execution
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices.models import DeviceExecutionProfile
from tinkerfin_studio.devices.profiles import DeviceProfiles
from tinkerfin_studio.devices.views import DeviceSelections
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload


async def source_records(
    session: AsyncSession,
) -> tuple[ConversationThread, ConversationRunRegistration]:
    repository = ConversationRepository(session)
    thread = await repository.get_accessible_thread(
        user_id=1, thread_id=SOURCE.thread_id
    )
    assert thread is not None
    run = await repository.get_run(thread_pk=thread.id, run_id=SOURCE.run_id)
    assert run is not None
    return thread, run


@pytest.fixture
async def local_conversation(database: Database):
    execution = await create_execution(database)
    async with database.session() as session:
        thread, run = await source_records(session)
        run.status = "succeeded"
        thread.last_run_id = run.run_id
        session.add(
            Project(
                id="destination",
                user_id=1,
                name="Destination",
                created_at=datetime(2030, 1, 1),
                updated_at=datetime(2030, 1, 1),
            )
        )
        session.add(
            AttachmentFile(
                id="a" * 32,
                user_id=1,
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id="user-message",
                name="report.txt",
                mime_type="text/plain",
                size_bytes=4,
            )
        )
        await session.commit()
    return execution


async def test_completed_local_conversation_rejects_move_atomically_and_can_continue(
    database: Database, local_conversation
) -> None:
    async with database.session() as session:
        repository = ConversationRepository(session)
        with pytest.raises(BusinessException) as denied:
            await repository.organize_thread(
                user_id=1,
                thread_id=SOURCE.thread_id,
                project_id="destination",
                archived=True,
            )
        assert int(denied.value.error_code) == 1_001_004_035
        assert denied.value.error_code.http_status == 409
        await session.commit()
        thread, _run = await source_records(session)
        attachment = await session.get(AttachmentFile, "a" * 32)
        assert attachment is not None and attachment.project_id == "project"
        assert thread.project_id == "project" and not thread.archived
    selection = await DeviceSelections(database, user_id=1).selection(
        SOURCE.thread_id, None
    )
    assert selection is not None and selection.workspace_id == "project"
    async with database.session() as session:
        following = await DeviceProfiles(session, 1).capture(
            identity=RunIdentity(
                namespace=SOURCE.namespace,
                thread_id=SOURCE.thread_id,
                run_id="following",
            ),
            selection=selection,
            access_mode="write_approval",
            skills=SkillSnapshotPayload(skills=()),
        )
        assert following is not None
        assert following.workspace_id == local_conversation.binding.workspace_id


async def test_local_conversation_can_archive_or_select_the_same_project(
    database: Database, local_conversation
) -> None:
    async with database.session() as session:
        repository = ConversationRepository(session)
        for archived in (True, False):
            thread = await repository.organize_thread(
                user_id=1,
                thread_id=SOURCE.thread_id,
                project_id="project",
                archived=archived,
            )
            await session.commit()
            assert thread.archived is archived and thread.project_id == "project"
        assert await session.get(
            DeviceExecutionProfile, local_conversation.binding.profile_id
        )


@pytest.mark.parametrize("source", ["cloud", "unaccepted-local"])
async def test_cloud_or_unaccepted_local_conversation_can_move_with_attachments(
    database: Database, local_conversation, source: str
) -> None:
    async with database.session() as session:
        _thread, run = await source_records(session)
        if source == "cloud":
            run.local_profile_id = None
        else:
            run.status = "rejected"
        await session.commit()
        moved = await ConversationRepository(session).organize_thread(
            user_id=1,
            thread_id=SOURCE.thread_id,
            project_id="destination",
            archived=False,
        )
        await session.commit()
        attachment = await session.get(AttachmentFile, "a" * 32)
        assert moved.project_id == "destination"
        assert attachment is not None and attachment.project_id == "destination"


def other_profile(
    profile_id: str, *, user_id: int, thread_id: str
) -> DeviceExecutionProfile:
    return DeviceExecutionProfile(
        profile_id=profile_id,
        user_id=user_id,
        device_id="other-device",
        source_thread_id=thread_id,
        source_run_id="source",
        workspace_id="other-project",
        workspace_label="Other project",
        access_mode="write_approval",
        skills=[],
        created_at=datetime(2030, 1, 1),
    )


async def test_thread_delete_removes_profiles_only_for_its_user_and_thread(
    database: Database, local_conversation
) -> None:
    async with database.session() as session:
        thread, _run = await source_records(session)
        thread_pk = thread.id
        session.add_all(
            [
                other_profile("other-user", user_id=2, thread_id=SOURCE.thread_id),
                other_profile("other-thread", user_id=1, thread_id="other-thread"),
                other_profile("unattached", user_id=1, thread_id=SOURCE.thread_id),
            ]
        )
        await session.commit()
        await ConversationRepository(session).delete_thread_cascade(thread_pk)
        await session.commit()
    async with database.session() as session:
        remaining = set(
            await session.scalars(select(DeviceExecutionProfile.profile_id))
        )
        assert remaining == {"other-user", "other-thread"}
        assert await session.get(ConversationThread, thread_pk) is None


@pytest.mark.parametrize("inherited", [False, True])
async def test_unstarted_cleanup_removes_only_unreferenced_profiles(
    database: Database, local_conversation, inherited: bool
) -> None:
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread, run = await source_records(session)
        run.status = "preparing"
        if inherited:
            following = await repository.create_run_registration(
                thread_id=thread.id,
                run_id="following",
                parent_run_id=run.run_id,
                model_id=run.model_id,
                input_json={},
            )
            following.local_profile_id = run.local_profile_id
        await session.commit()
        result = await repository.delete_unstarted_run(
            thread_pk=thread.id,
            run_pk=run.id,
            run_id=run.run_id,
            preparation_id=run.preparation_id,
            delete_empty_thread=True,
        )
        await session.commit()
        assert result.run_deleted and result.thread_deleted is not inherited
        retained = await session.get(
            DeviceExecutionProfile, local_conversation.binding.profile_id
        )
        assert (retained is not None) is inherited
        if inherited:
            following = await repository.get_run(
                thread_pk=thread.id, run_id="following"
            )
            assert following is not None
            binding = await DeviceProfiles(session, 1).capture(
                identity=RunIdentity(
                    namespace=SOURCE.namespace,
                    thread_id=SOURCE.thread_id,
                    run_id="resumed",
                ),
                selection=None,
                access_mode="write_approval",
                skills=SkillSnapshotPayload(skills=()),
                inherited=following.local_profile_id,
            )
            assert binding is not None and binding == local_conversation.binding
            await repository.delete_unstarted_run(
                thread_pk=thread.id,
                run_pk=following.id,
                run_id=following.run_id,
                preparation_id=following.preparation_id,
                delete_empty_thread=True,
            )
            await session.commit()
            assert await session.get(DeviceExecutionProfile, binding.profile_id) is None


async def test_cleanup_preserves_profiles_for_stale_owner_and_cancelled_transaction(
    database: Database, local_conversation
) -> None:
    async with database.session() as session:
        thread, run = await source_records(session)
        run.status = "preparing"
        thread_pk, run_pk, preparation_id = thread.id, run.id, run.preparation_id
        await session.commit()
        rejected = await ConversationRepository(session).delete_unstarted_run(
            thread_pk=thread_pk,
            run_pk=run_pk,
            run_id=SOURCE.run_id,
            preparation_id="stale-owner",
            delete_empty_thread=True,
        )
        await session.commit()
        assert not rejected.run_deleted and not rejected.thread_deleted
    applied, release = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        async with database.session() as session:
            await ConversationRepository(session).delete_unstarted_run(
                thread_pk=thread_pk,
                run_pk=run_pk,
                run_id=SOURCE.run_id,
                preparation_id=preparation_id,
                delete_empty_thread=True,
            )
            applied.set()
            await release.wait()
            await session.commit()

    owner = asyncio.create_task(cleanup())
    waiting = asyncio.create_task(applied.wait())
    try:
        completed, _pending = await asyncio.wait(
            (owner, waiting), return_when=asyncio.FIRST_COMPLETED
        )
        if owner in completed:
            owner.result()
        assert applied.is_set()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    finally:
        owner.cancel()
        waiting.cancel()
        await asyncio.gather(owner, waiting, return_exceptions=True)
    async with database.session() as session:
        assert await session.get(ConversationThread, thread_pk) is not None
        assert await session.get(ConversationRunRegistration, run_pk) is not None
        assert await session.get(
            DeviceExecutionProfile, local_conversation.binding.profile_id
        )
