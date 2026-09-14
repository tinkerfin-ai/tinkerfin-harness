"""生成、导入与交付分别维护工作文件和附件的可观察边界"""

import asyncio
import base64
import io
import json
import shlex
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse
from deepagents.backends.sandbox import MAX_BINARY_BYTES
from PIL import Image

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.attachments.sandbox_tools import build_sandbox_attachment_tools
from tinkerfin_studio.attachments.service import AttachmentService, byte_chunks
from tinkerfin_studio.attachments.tools import build_attachment_tools
from tinkerfin_studio.attachments.work_files import save_work_file


@pytest.mark.parametrize("source", ["generate", "capture", "import"])
async def test_large_images_have_readable_previews_and_deliver_unchanged_original(
    source, large_image, attachments, work_file_runtime, monkeypatch
):
    runtime, workspace, files = work_file_runtime
    await attachments.create_collection(
        user_id=1,
        collection_id="images",
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    tools = {
        item.name: item
        for item in [
            *build_attachment_tools(
                service=attachments,
                processor=attachments.documents,
                user_id=1,
                collection_id="images",
                image_model=None,
            ),
            *build_sandbox_attachment_tools(
                service=attachments, user_id=1, collection_id="images"
            ),
        ]
    }
    if source == "generate":
        monkeypatch.setattr(
            "tinkerfin_studio.attachments.tools.generate_image_bytes",
            AsyncMock(return_value=large_image),
        )
        result = await tools["generate_image"].ainvoke(
            {"runtime": runtime, "prompt": "test image"}
        )
    elif source == "import":
        original = await attachments.upload(
            user_id=1,
            collection_id="images",
            name="original.png",
            chunks=byte_chunks(large_image),
        )
        result = await tools["import_attachment"].ainvoke(
            {"runtime": runtime, "attachment_id": original.id}
        )
    else:
        workspace.to_shell_path.side_effect = lambda path: path

        async def capture(command, *, timeout):
            files[shlex.split(command)[-1]] = large_image
            return ExecuteResponse(output="", exit_code=0)

        workspace.aexecute.side_effect = capture
        result = await tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.test"}
        )
    saved = json.loads(result)
    assert files[saved["file_path"]] == large_image
    preview = files[saved["preview_file_path"]]
    assert 0 < len(preview) <= MAX_BINARY_BYTES
    with Image.open(io.BytesIO(preview)) as image:
        assert image.format == "JPEG" and max(image.size) <= 1280
    before = await attachments.list_collection(user_id=1, collection_id="images")
    assert len(before) == (1 if source == "import" else 0)
    await tools["deliver_file"].ainvoke(
        {"runtime": runtime, "file_path": saved["file_path"], "name": "result.png"}
    )
    after = await attachments.list_collection(user_id=1, collection_id="images")
    delivered = next(item for item in after if item.name == "result.png")
    assert (await attachments.read(delivered.id, user_id=1, collection_id="images"))[
        1
    ] == large_image
    assert len(after) == len(before) + 1


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
        image_model=None,
    )
    return {tool.name: tool for tool in tools}, service, processor


async def test_same_named_generated_files_are_independent(
    generating_tools, work_file_runtime
):
    tools, service, _ = generating_tools
    runtime, _, files = work_file_runtime
    results = await asyncio.gather(
        *(
            tools["create_file"].ainvoke(
                {
                    "runtime": runtime,
                    "name": "draft.md",
                    "kind": "md",
                    "text": "# draft",
                }
            )
            for _ in range(2)
        )
    )
    paths = [json.loads(result)["file_path"] for result in results]
    assert len(set(paths)) == 2
    assert all(files[path] == b"# draft" for path in paths)
    service.upload.assert_not_called()


async def test_failed_work_file_upload_does_not_report_success(
    generating_tools, work_file_runtime
):
    tools, service, _ = generating_tools
    runtime, workspace, _ = work_file_runtime
    workspace.aupload_files.side_effect = lambda items: [
        FileUploadResponse(path=items[0][0], error="permission_denied")
    ]
    with pytest.raises(OSError, match="保存失败"):
        await tools["create_file"].ainvoke(
            {"runtime": runtime, "name": "draft.md", "kind": "md"}
        )
    service.upload.assert_not_called()


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


@pytest.mark.parametrize(
    "name,data",
    [
        ("../draft.md", b"# draft"),
        ("draft.md", b""),
        ("draft.md", b"a" * (MAX_FILE_BYTES + 1)),
    ],
)
async def test_invalid_work_files_are_rejected_before_upload(
    work_file_runtime, name, data
):
    _, workspace, _ = work_file_runtime
    with pytest.raises(ValueError):
        await save_work_file(workspace, DocumentProcessor(), name=name, data=data)
    workspace.aupload_files.assert_not_awaited()


async def test_import_authorizes_before_copying_and_keeps_original(
    attachments, work_file_runtime
):
    runtime, _, files = work_file_runtime
    await attachments.create_collection(
        user_id=1,
        collection_id="run",
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    original = await attachments.upload(
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
            image_model=None,
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
            image_model=None,
        )
    }
    imported = json.loads(
        await owner_tools["import_attachment"].ainvoke(
            {"runtime": runtime, "attachment_id": original.id}
        )
    )
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
