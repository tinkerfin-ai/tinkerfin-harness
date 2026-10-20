"""生成、导入与交付分别维护工作文件和附件的可观察边界"""

import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.attachments.tools import build_attachment_tools
from tinkerfin_studio.attachments.workspace_tools import (
    build_sandbox_attachment_tools,
)
from tinkerfin_studio.services.schemas import ImageConfig
from tinkerfin_studio.services.service import ResolvedService

pytestmark = pytest.mark.usefixtures("projects")


@pytest.fixture
def generating_tools():
    processor = MagicMock(spec=DocumentProcessor)
    processor.run = AsyncMock(
        return_value={"data": base64.b64encode(b"# draft").decode()}
    )
    service = MagicMock(spec=AttachmentService)
    tools = build_attachment_tools(
        service=service,
        processor=processor,
        user_id=1,
        thread_id="thread",
        image_service=None,
    )
    return {tool.name: tool for tool in tools}, service, processor


async def test_cancelled_work_file_save_propagates_without_delivery(
    generating_tools, work_file_runtime
):
    tools, service, _ = generating_tools
    runtime, workspace, _ = work_file_runtime
    started = asyncio.Event()

    async def upload(items):
        started.set()
        await asyncio.Event().wait()

    workspace.aupload_files.side_effect = upload
    task = asyncio.create_task(
        tools["create_file"].ainvoke(
            {"runtime": runtime, "name": "draft.md", "kind": "md"}
        )
    )
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    service.upload.assert_not_called()


async def test_import_authorizes_before_copying_and_keeps_original(
    attachments, work_file_runtime
):
    runtime, _, files = work_file_runtime
    await attachments.create_collection(
        project_id="project-1",
        user_id=1,
        collection_id="run",
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    original = await attachments.upload(
        project_id="project-1",
        user_id=1,
        name="draft.md",
        chunks=byte_chunks(b"# original"),
        collection_id="run",
    )
    tools = {
        item.name: item
        for item in build_attachment_tools(
            service=attachments,
            processor=attachments.documents,
            user_id=2,
            collection_id="run",
            image_service=None,
        )
    }
    with pytest.raises(BusinessException):
        await tools["import_attachment"].ainvoke(
            {"runtime": runtime, "attachment_id": original.id}
        )
    assert files == {}
    owner_tools = {
        item.name: item
        for item in build_attachment_tools(
            service=attachments,
            processor=attachments.documents,
            user_id=1,
            collection_id="run",
            image_service=None,
        )
    }
    imported = json.loads(
        await owner_tools["import_attachment"].ainvoke(
            {"runtime": runtime, "attachment_id": original.id}
        )
    )
    assert imported["shell_path"] == imported["file_path"].lstrip("/")
    files[imported["file_path"]] = b"# changed"
    deliver = build_sandbox_attachment_tools(
        service=attachments, user_id=1, collection_id="run"
    )[0]
    await deliver.ainvoke(
        {"runtime": runtime, "file_path": imported["file_path"], "name": "result.md"}
    )
    saved = await attachments.list_collection(user_id=1, collection_id="run")
    assert len(saved) == 2
    output = next(item for item in saved if item.id != original.id)
    assert (await attachments.read(original.id, user_id=1, collection_id="run"))[
        1
    ] == b"# original"
    assert (await attachments.read(output.id, user_id=1, collection_id="run"))[
        1
    ] == b"# changed"
    files[imported["file_path"]] = b"# edited again"
    assert (await attachments.read(output.id, user_id=1, collection_id="run"))[
        1
    ] == b"# changed"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_export_keeps_paid_original_without_another_generation(
    work_file_runtime, monkeypatch, cancelled
):
    runtime, _, files = work_file_runtime
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(output, "PNG")
    data = output.getvalue()
    generate = AsyncMock(return_value=data)
    monkeypatch.setattr(
        "tinkerfin_studio.attachments.tools.generate_image_bytes", generate
    )
    processor = MagicMock(spec=DocumentProcessor)
    processor.run = AsyncMock(
        side_effect=asyncio.CancelledError()
        if cancelled
        else ValueError("processor-private-detail")
    )
    service = MagicMock(spec=AttachmentService)
    image_service = ResolvedService(
        id="image",
        configuration=ImageConfig(
            model="supplier",
            endpoint="https://example.test",
            output_formats=["jpeg", "webp"],
        ),
        fingerprint="test",
        api_key="test",
    )
    tool = next(
        item
        for item in build_attachment_tools(
            service=service,
            processor=processor,
            user_id=1,
            thread_id="thread",
            image_service=image_service,
        )
        if item.name == "generate_image"
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await tool.ainvoke({"prompt": "image", "runtime": runtime})
    else:
        result = await tool.ainvoke({"prompt": "image", "runtime": runtime})
        assert "不要再次生成" in result
        assert "processor-private-detail" not in result
        assert next(iter(files)) in result
    generate.assert_awaited_once()
    assert list(files.values()) == [data]
    service.upload.assert_not_called()
