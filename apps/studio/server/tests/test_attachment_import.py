"""搬迁只写缺少的对象，源文件及冲突目标均保持完整"""

import pytest

from tinkerfin_studio.attachments.import_disk import import_attachments
from tinkerfin_studio.attachments.service import byte_chunks


async def test_import_validates_sources_and_never_overwrites_conflicts(
    attachments, attachment_storage, database, tmp_path
):
    item = await attachments.upload(
        user_id=1, name="a.md", chunks=byte_chunks(b"content")
    )
    original = tmp_path / item.id
    original.write_bytes(b"content")
    attachment_storage.objects.clear()
    checked = await import_attachments(database, attachment_storage, tmp_path)
    assert checked.objects == 1 and checked.copied == 0
    assert not attachment_storage.objects
    copied = await import_attachments(
        database, attachment_storage, tmp_path, apply=True
    )
    assert copied.copied == 1 and original.read_bytes() == b"content"
    attachment_storage.objects[item.id] = b"other"
    with pytest.raises(ValueError, match="已有不同内容"):
        await import_attachments(database, attachment_storage, tmp_path, apply=True)
    assert attachment_storage.objects[item.id] == b"other"
    original.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验值不符"):
        await import_attachments(database, attachment_storage, tmp_path, apply=True)
    original.unlink()
    with pytest.raises(ValueError, match="缺失"):
        await import_attachments(database, attachment_storage, tmp_path, apply=True)
