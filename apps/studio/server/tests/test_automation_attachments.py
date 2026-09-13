"""任务和运行通过持久引用保存文件，不依赖页面或草稿生命周期"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.service import byte_chunks


async def test_task_and_run_keep_reference_through_cleanup_and_reconfiguration(
    attachments, database
) -> None:
    file = await attachments.upload(
        user_id=1, name="参考.md", chunks=byte_chunks(b"# reference")
    )
    await attachments.create_collection(
        user_id=1,
        collection_id="input",
        purpose="input",
        attachment_ids=(file.id,),
        configuration={"prompt": "summary"},
    )
    await attachments.mark_collection_task(
        user_id=1, collection_id="input", task_id="task"
    )
    await attachments.create_collection(
        user_id=1,
        collection_id="run",
        purpose="execution",
        attachment_ids=(file.id,),
        configuration={"prompt": "summary"},
        task_id="task",
    )
    async with database.session() as session:
        await session.execute(
            update(AttachmentFile).values(
                created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
            )
        )
        await session.commit()
    await attachments.cleanup()
    assert (await attachments.read(file.id, user_id=1, collection_id="run"))[
        1
    ] == b"# reference"
    with pytest.raises(BusinessException):
        await attachments.remove_draft(file.id, user_id=1)
    with pytest.raises(BusinessException):
        await attachments.create_collection(
            user_id=1,
            collection_id="input",
            purpose="input",
            attachment_ids=(),
            configuration={"prompt": "changed"},
        )
    with pytest.raises(BusinessException):
        await attachments.read(file.id, user_id=2, collection_id="run")
    assert len(await attachments.list_collection(user_id=1, collection_id="input")) == 1


async def test_outputs_are_bound_to_execution_and_input_collection_is_immutable(
    attachments,
) -> None:
    await attachments.create_collection(
        user_id=1,
        collection_id="run",
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    file = await attachments.upload(
        user_id=1,
        name="结果.md",
        chunks=byte_chunks(b"# result"),
        collection_id="run",
        source="tool",
    )
    assert [
        item.id
        for item in await attachments.list_collection(user_id=1, collection_id="run")
    ] == [file.id]
    await attachments.create_collection(
        user_id=1,
        collection_id="input",
        purpose="input",
        attachment_ids=(),
        configuration={},
    )
    with pytest.raises(BusinessException):
        await attachments.upload(
            user_id=1,
            name="不可改写.md",
            chunks=byte_chunks(b"# no"),
            collection_id="input",
        )
    with pytest.raises(BusinessException):
        await attachments.get(file.id, user_id=1, collection_id="input")


async def test_failed_input_can_release_references_for_normal_draft_cleanup(
    attachments,
) -> None:
    file = await attachments.upload(
        user_id=1, name="草稿.md", chunks=byte_chunks(b"# draft")
    )
    await attachments.create_collection(
        user_id=1,
        collection_id="input",
        purpose="input",
        attachment_ids=(file.id,),
        configuration={},
    )
    await attachments.discard_collection(user_id=1, collection_id="input")
    await attachments.remove_draft(file.id, user_id=1)
    with pytest.raises(BusinessException):
        await attachments.get(file.id, user_id=1)


async def test_duplicate_collection_creation_is_idempotent_under_concurrency(
    attachments,
) -> None:
    import asyncio

    await asyncio.gather(
        *(
            attachments.create_collection(
                user_id=1,
                collection_id="same",
                purpose="input",
                attachment_ids=(),
                configuration={"prompt": "same"},
            )
            for _ in range(2)
        )
    )
    assert await attachments.list_collection(user_id=1, collection_id="same") == []
