"""直传确认、用户归属、并发处理与失败回收"""

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from tinkerfin_studio.api.errors import AttachmentErrorCode, BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile


async def test_upload_confirmation_publishes_validated_bytes_once(
    attachments, attachment_storage, database
):
    data = b"# report\n"
    attachment_id, form = await attachments.request_upload(
        user_id=1, name="report.md", size_bytes=len(data)
    )
    assert form.expires_in == 600
    with pytest.raises(BusinessException):
        await attachments.download_url(attachment_id, user_id=1)
    attachment_storage.objects[attachment_id + "-upload"] = data
    with pytest.raises(BusinessException):
        await attachments.complete_upload(attachment_id, user_id=2)
    result = await attachments.complete_upload(attachment_id, user_id=1)
    attachment_storage.objects[attachment_id + "-upload"] = b"late upload"
    assert await attachments.complete_upload(attachment_id, user_id=1) == result
    assert (await attachments.read(attachment_id, user_id=1))[1] == data
    async with database.session() as session:
        row = await session.get(AttachmentFile, attachment_id)
        assert row.status == "ready"
        assert row.sha256 == hashlib.sha256(data).hexdigest()
    assert (await attachments.download_url(attachment_id, user_id=1)).expires_in == 300
    with pytest.raises(BusinessException):
        await attachments.download_url(attachment_id, user_id=2)
    with pytest.raises(BusinessException):
        await attachments.download_url(attachment_id, user_id=1, variant="preview")
    await attachments.remove_draft(attachment_id, user_id=1)
    assert not attachment_storage.objects


@pytest.mark.parametrize("data", [None, b"short", b"bad\x00content"])
async def test_incomplete_or_invalid_upload_is_not_published(
    attachments, attachment_storage, database, data
):
    attachment_id, _ = await attachments.request_upload(
        user_id=1, name="report.md", size_bytes=11
    )
    if data is not None:
        attachment_storage.objects[attachment_id + "-upload"] = data
    with pytest.raises(BusinessException):
        await attachments.complete_upload(attachment_id, user_id=1)
    async with database.session() as session:
        assert await session.scalar(select(AttachmentFile.id)) is None
    assert not attachment_storage.objects


async def test_concurrent_confirmation_and_removal_cannot_interleave_publication(
    attachments,
    attachment_storage,
    monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()
    read = attachment_storage.read

    async def blocked_read(key):
        entered.set()
        await release.wait()
        return await read(key)

    attachment_id, _ = await attachments.request_upload(
        user_id=1, name="report.md", size_bytes=4
    )
    attachment_storage.objects[attachment_id + "-upload"] = b"text"
    monkeypatch.setattr(attachment_storage, "read", blocked_read)
    task = asyncio.create_task(attachments.complete_upload(attachment_id, user_id=1))
    try:
        await entered.wait()
        with pytest.raises(BusinessException) as duplicate:
            await attachments.complete_upload(attachment_id, user_id=1)
        assert duplicate.value.error_code == AttachmentErrorCode.UPLOAD_IN_PROGRESS
        with pytest.raises(BusinessException):
            await attachments.remove_draft(attachment_id, user_id=1)
    finally:
        release.set()
        result = await task
    assert result.id == attachment_id


async def test_cancellation_preserves_primary_error_when_cleanup_is_unavailable(
    attachments,
    attachment_storage,
    database,
    monkeypatch,
):
    entered = asyncio.Event()

    async def blocked_read(key):
        entered.set()
        await asyncio.Event().wait()

    async def unavailable(key):
        raise OSError("unavailable")

    attachment_id, _ = await attachments.request_upload(
        user_id=1, name="report.md", size_bytes=4
    )
    monkeypatch.setattr(attachment_storage, "read", blocked_read)
    monkeypatch.setattr(attachment_storage, "delete", unavailable)
    task = asyncio.create_task(attachments.complete_upload(attachment_id, user_id=1))
    try:
        await entered.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with database.session() as session:
        row = await session.get(AttachmentFile, attachment_id)
        assert row.status == "deleting"
