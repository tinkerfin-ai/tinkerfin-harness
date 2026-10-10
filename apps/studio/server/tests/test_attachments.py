"""附件原件保留、授权读取、文件生成和删除边界"""

import base64
import io
import json
import struct
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from attachment_fakes import MemoryAttachmentStorage
from PIL import Image
from sqlalchemy import select

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.processing import validate_file
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.conversation.models import ConversationThread


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, "PNG")
    return output.getvalue()


def pptx():
    return office_container(
        "ppt/presentation.xml",
        b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" />',
    )


def office_container(member: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return output.getvalue()


@pytest.fixture
async def attachments(notifications, database, attachment_storage):
    return AttachmentService(database, attachment_storage, notifications=notifications)


async def test_upload_keeps_original_and_rejects_other_users(attachments):
    data = png()
    file = await attachments.upload(
        project_id="project-1", user_id=1, name="截图.png", chunks=byte_chunks(data)
    )
    _, original = await attachments.read(file.id, user_id=1)
    assert original == data
    _, preview = await attachments.read(file.id, user_id=1, variant="preview")
    assert Image.open(io.BytesIO(preview)).format == "JPEG"
    with pytest.raises(BusinessException) as denied:
        await attachments.read(file.id, user_id=2)
    assert denied.value.error_code.http_status == 404


@pytest.mark.parametrize("user_id,collection_id", [(2, "files"), (1, "other")])
async def test_model_content_authorizes_before_reading_storage(
    attachments, attachment_storage, monkeypatch, user_id, collection_id
):
    """跨用户或集合读取在访问附件字节前被拒绝"""
    await attachments.create_collection(
        project_id="project-1",
        user_id=1,
        collection_id="files",
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    file = await attachments.upload(
        project_id="project-1",
        user_id=1,
        collection_id="files",
        name="note.md",
        chunks=byte_chunks(b"# private"),
    )
    read = AsyncMock(wraps=attachment_storage.read)
    monkeypatch.setattr(attachment_storage, "read", read)
    with pytest.raises(FileNotFoundError, match="附件内容不可用"):
        await attachments.read_content(
            file, user_id=user_id, collection_id=collection_id
        )
    read.assert_not_awaited()


async def test_binding_prevents_cross_thread_reuse_and_draft_deletion(
    attachments, database
):
    file = await attachments.upload(
        project_id="project-1", user_id=1, name="chart.png", chunks=byte_chunks(png())
    )
    async with database.session() as session:
        session.add(
            ConversationThread(
                archived=False,
                project_id="project-1",
                user_id=1,
                thread_id="thread-a",
                title="附件测试",
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()
        await attachments.bind(
            session, [file.id], user_id=1, thread_id="thread-a", message_id="message-a"
        )
        await session.commit()
    with pytest.raises(BusinessException):
        await attachments.get(file.id, user_id=1, thread_id="thread-b")
    with pytest.raises(FileNotFoundError):
        await attachments.read_content(file, user_id=1, thread_id="thread-b")
    assert (
        await attachments.read_content(file, user_id=1, thread_id="thread-a")
    ).mime_type == "image/jpeg"
    with pytest.raises(BusinessException) as rejected:
        await attachments.remove_draft(file.id, user_id=1)
    assert rejected.value.error_code.http_status == 409
    assert (await attachments.list_thread(user_id=1, thread_id="thread-a"))[
        0
    ].id == file.id


async def test_invalid_type_and_path_do_not_publish_files(attachments):
    for name, data in [
        ("../x.png", png()),
        ("fake.png", b"not an image"),
        ("file.exe", b"binary"),
    ]:
        with pytest.raises(BusinessException):
            await attachments.upload(
                project_id="project-1", user_id=1, name=name, chunks=byte_chunks(data)
            )


def test_pptx_validation_rejects_bad_encrypted_and_oversized_containers():
    """PPTX 校验拒绝坏包、加密包和解压后超限的压缩包"""
    assert validate_file("report.pptx", pptx()) == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    with pytest.raises(ValueError, match="容器损坏"):
        validate_file("report.pptx", b"not a zip")
    with pytest.raises(ValueError, match="文档损坏或已加密"):
        validate_file("report.pptx", office_container("ppt/slide1.xml", b"<slide />"))

    encrypted = bytearray(office_container("ppt/presentation.xml", b"<ppt />"))
    encrypted[6:8] = struct.pack("<H", 1)
    central_header = encrypted.find(b"PK\x01\x02")
    assert central_header >= 0
    encrypted[central_header + 8 : central_header + 10] = struct.pack("<H", 1)
    with pytest.raises(ValueError, match="文档损坏或已加密"):
        validate_file("report.pptx", bytes(encrypted))

    with pytest.raises(ValueError, match="解压后过大"):
        validate_file(
            "report.pptx",
            office_container("ppt/presentation.xml", b"0" * (50 * 1024 * 1024 + 1)),
        )


async def test_cancelled_storage_write_removes_staging_record_and_bytes(
    notifications, database, tmp_path
):
    """取消不得留下可见附件或无法按记录清理的文件"""
    import asyncio

    class InterruptedStorage(MemoryAttachmentStorage):
        async def put(self, key, chunks):
            async def interrupted():
                async for chunk in chunks:
                    yield chunk
                    raise asyncio.CancelledError()

            await super().put(key, interrupted())

    storage = InterruptedStorage()
    service = AttachmentService(database, storage, notifications=notifications)
    with pytest.raises(asyncio.CancelledError):
        await service.upload(
            project_id="project-1",
            user_id=1,
            name="cancelled.png",
            chunks=byte_chunks(png()),
        )
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None
    assert not storage.objects


async def test_workbook_preserves_numbers_and_treats_formula_like_text_as_text(
    attachments,
):
    """生成的数据表保留数值类型，不将外部文字变成可执行公式"""
    from openpyxl import load_workbook

    result = await attachments.documents.run(
        {"operation": "generate", "kind": "xlsx", "rows": [[128, "=SUM(A1:A2)"]]}
    )
    workbook = load_workbook(io.BytesIO(base64.b64decode(result["data"])))
    sheet = workbook.active
    assert sheet is not None
    assert sheet["A1"].value == 128
    assert sheet["B1"].data_type == "s"
    workbook.close()


@pytest.mark.parametrize(
    "kind,error_type", [("invalid_input", ValueError), ("internal_error", RuntimeError)]
)
async def test_file_processor_preserves_worker_failure_category_and_reaps_process(
    monkeypatch, kind, error_type
):
    """无效参数与执行故障分别传播，已退出的处理进程仍会等待回收"""
    from tinkerfin_studio.attachments import documents

    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(return_value=(json.dumps({"kind": kind}).encode(), b"")),
        wait=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        documents.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(error_type):
        await documents.DocumentProcessor().run(
            {"operation": "generate", "kind": "md", "text": "# report"}
        )
    process.wait.assert_awaited_once()


@pytest.mark.parametrize("data", [b"", b"\xff\xfe#\x00", b"# report\x00data"])
async def test_invalid_markdown_is_not_published(attachments, database, data):
    """空文件、非 UTF-8 和二进制内容不得留下可见附件"""
    with pytest.raises(BusinessException):
        await attachments.upload(
            project_id="project-1",
            user_id=1,
            name="report.md",
            chunks=byte_chunks(data),
        )
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None


pytestmark = pytest.mark.usefixtures("projects")
