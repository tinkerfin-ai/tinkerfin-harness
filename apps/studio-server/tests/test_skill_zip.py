"""聊天 ZIP 的完整原件、技能校验、附件归属和模型引用"""

import codecs
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession
from test_automation_conversation_tools import call_runtime
from test_skills_execution import RecordingModel
from test_skills_library import add_users, archive_bytes, skill_files

from tinkerfin import TinkerFin
from tinkerfin.media import AttachmentContent, AttachmentSupport
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.errors import (
    AttachmentErrorCode,
    BusinessException,
    SkillErrorCode,
)
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import SkillFile
from tinkerfin_studio.skills.tools import build_skill_tools


async def test_zip_preview_requires_attachment_bound_by_message_submission(
    session: AsyncSession,
    attachments: AttachmentService,
    skill_library: SkillLibrary,
):
    await add_users(session)
    await ConversationRepository(session).create_thread(
        project_id="project-1", user_id=1, thread_id="chat", title="技能", model_id=None
    )
    await session.commit()
    uploaded = await attachments.upload(
        project_id="project-1",
        user_id=1,
        name="reports.zip",
        chunks=byte_chunks(archive_bytes(skill_files())),
    )
    preview_tool = next(
        entry
        for entry in build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        )
        if entry.name == "preview_skills"
    )
    arguments = {"source": {"kind": "zip", "attachment_id": uploaded.id}}
    rejected = json.loads(await preview_tool.ainvoke(arguments))
    assert rejected["code"] == int(AttachmentErrorCode.NOT_FOUND)
    assert await skill_library.list(1) == []
    assert (
        await attachments.get(
            uploaded.id, user_id=1, thread_id="chat", allow_unbound=True
        )
        == uploaded
    )
    await attachments.bind(
        session, [uploaded.id], user_id=1, thread_id="chat", message_id="message"
    )
    await session.commit()
    preview = json.loads(await preview_tool.ainvoke(arguments))
    assert preview["candidates"][0]["name"] == "reports"


async def test_chat_zip_preview_install_and_replace_share_the_skill_service(
    session: AsyncSession,
    attachments: AttachmentService,
    skill_library: SkillLibrary,
):
    await add_users(session)
    await ConversationRepository(session).create_thread(
        project_id="project-1", user_id=1, thread_id="chat", title="技能", model_id=None
    )
    await session.commit()
    data = archive_bytes(
        (
            SkillFile("SKILL.md", codecs.BOM_UTF8 + skill_files()[0].content),
            *skill_files()[1:],
            SkillFile("references/slides/SKILL.md", b"Nested reference"),
        )
    )
    uploaded = await attachments.upload(
        project_id="project-1",
        user_id=1,
        thread_id="chat",
        name="reports.zip",
        chunks=byte_chunks(data),
    )
    assert uploaded.mime_type == "application/zip"
    assert (await attachments.read(uploaded.id, user_id=1, thread_id="chat"))[1] == data
    tools = {
        entry.name: entry
        for entry in build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        )
    }
    preview = json.loads(
        await tools["preview_skills"].ainvoke(
            {"source": {"kind": "zip", "attachment_id": uploaded.id}}
        )
    )
    assert await skill_library.list(1) == []
    installed = json.loads(
        await tools["manage_skill"].ainvoke(
            {
                "operation": {
                    "action": "install",
                    "target": {
                        "kind": "import",
                        "draft_id": preview["id"],
                        "digests": [preview["candidates"][0]["digest"]],
                    },
                },
                "runtime": call_runtime(call_id="install"),
            }
        )
    )["result"]["installation_ids"][0]
    replacement = await attachments.upload(
        project_id="project-1",
        user_id=1,
        thread_id="chat",
        name="reports.zip",
        chunks=byte_chunks(
            archive_bytes((*skill_files(), SkillFile("updated", b"new")))
        ),
    )
    preview = json.loads(
        await tools["preview_skills"].ainvoke(
            {"source": {"kind": "zip", "attachment_id": replacement.id}}
        )
    )
    updated = json.loads(
        await tools["manage_skill"].ainvoke(
            {
                "operation": {
                    "action": "update",
                    "installation_id": installed,
                    "replacement": {
                        "draft_id": preview["id"],
                        "digest": preview["candidates"][0]["digest"],
                    },
                },
                "runtime": call_runtime(call_id="update"),
            }
        )
    )
    assert updated["result"]["changed"]
    assert updated["result"]["installation"]["id"] == installed
    with pytest.raises(BusinessException):
        await skill_library.preview_attachment(2, "chat", uploaded.id)
    with pytest.raises(BusinessException):
        await skill_library.preview_attachment(1, "another-thread", uploaded.id)


async def test_zip_direct_upload_and_invalid_packages_use_the_same_validation(
    attachments: AttachmentService, attachment_storage
):
    data = archive_bytes(skill_files())
    attachment_id, _ = await attachments.request_upload(
        project_id="project-1", user_id=1, name="reports.zip", size_bytes=len(data)
    )
    attachment_storage.objects[attachment_id + "-upload"] = data
    file = await attachments.complete_upload(attachment_id, user_id=1)
    assert file.mime_type == "application/zip"
    assert (await attachments.read(file.id, user_id=1))[1] == data
    for malformed in [
        b"not zip",
        archive_bytes((SkillFile("notes.txt", b"text"),)),
        archive_bytes((*skill_files(), SkillFile("../outside", b"x"))),
    ]:
        with pytest.raises(BusinessException) as failed:
            await attachments.upload(
                project_id="project-1",
                user_id=1,
                name="invalid.zip",
                chunks=byte_chunks(malformed),
            )
        assert failed.value.error_code == SkillErrorCode.INVALID_PACKAGE
    with pytest.raises(BusinessException) as oversized:
        await attachments.request_upload(
            project_id="project-1",
            user_id=1,
            name="large.zip",
            size_bytes=MAX_FILE_BYTES + 1,
        )
    assert oversized.value.error_code.http_status == 413
    assert set(attachment_storage.objects) == {file.id}


async def test_zip_is_a_model_reference_and_never_sent_as_binary(
    attachments: AttachmentService,
):
    uploaded = await attachments.upload(
        project_id="project-1",
        user_id=1,
        name="reports.zip",
        chunks=byte_chunks(archive_bytes(skill_files())),
    )

    async def forbidden_read(attachment: Attachment) -> AttachmentContent:
        raise AssertionError("模型不应读取 ZIP 字节")

    model = RecordingModel(
        responses=[AIMessage(content="done")],
        profile={"image_inputs": True, "pdf_inputs": True},
    )
    runtime = (
        TinkerFin()
        .with_namespace("ns_1")
        .with_attachments(AttachmentSupport(read_content=forbidden_read))
        .build(model=model)
    )
    await runtime.ainvoke(
        thread_id="zip",
        run_id="reference",
        input={"messages": [HumanMessage(content=[uploaded.content_block()])]},
    )
    assert uploaded.id in model.prompts[0]
    assert "application/zip" in model.prompts[0]
    assert "base64" not in model.prompts[0]


pytestmark = pytest.mark.usefixtures("projects")
