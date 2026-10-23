"""Sandbox 附件有界读取、截图产物保留和交付失败边界"""

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepagents.backends.protocol import (
    DeleteResult,
    ExecuteResponse,
    FileUploadResponse,
)
from PIL import Image

from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts.media import Attachment
from tinkerfin_sandbox import OpenSandboxFileTooLargeError, RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.attachments.workspace_tools import (
    build_sandbox_attachment_tools,
    save_work_file,
)


@pytest.fixture
def attachment_tools():
    sandbox = MagicMock(spec=RootedOpenSandboxBackend)
    output = io.BytesIO()
    Image.new("RGB", (8, 6), "white").save(output, "PNG")
    sandbox.aread_bytes = AsyncMock(return_value=output.getvalue())
    sandbox.aupload_files = AsyncMock(
        side_effect=lambda items: [
            FileUploadResponse(path=path, error=None) for path, _ in items
        ]
    )
    sandbox.aexecute = AsyncMock(return_value=ExecuteResponse(output="", exit_code=0))
    sandbox.adelete = AsyncMock(side_effect=lambda path: DeleteResult(path=path))
    sandbox.to_shell_path.side_effect = RootedOpenSandboxBackend(
        MagicMock()
    ).to_shell_path
    service = MagicMock(spec=AttachmentService)
    service.documents = DocumentProcessor()
    service.upload = AsyncMock(
        return_value=Attachment(
            id="a", name="r.png", mime_type="image/png", size_bytes=8
        )
    )

    class WorkspaceRuntime(ToolRuntime[None, RootedOpenSandboxBackend]):
        @property
        def workspace(self) -> RootedOpenSandboxBackend:
            return sandbox

    runtime = WorkspaceRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda value: None,
        tool_call_id=None,
        store=None,
    )
    tools = build_sandbox_attachment_tools(
        service=service, user_id=1, thread_id="thread"
    )
    return sandbox, service, {tool.name: tool for tool in tools}, runtime


@pytest.mark.parametrize(
    "name,data",
    [
        ("记录.txt", "原始内容".encode("gb18030")),
        ("broken.json", b'{"x":'),
        ("README", b"binary\x00\xff"),
    ],
)
async def test_general_attachment_import_keeps_bytes_without_preview_or_execution(
    attachment_tools, name, data
):
    sandbox, service, _, _ = attachment_tools
    service.documents.run = AsyncMock()
    saved = await save_work_file(sandbox, service.documents, name=name, data=data)
    assert saved.name == name and saved.size_bytes == len(data)
    assert saved.preview_file_path is None
    sandbox.aupload_files.assert_awaited_once_with([(saved.file_path, data)])
    sandbox.aexecute.assert_not_awaited()
    service.documents.run.assert_not_awaited()


async def test_deliver_files_does_not_upload_overflow(attachment_tools) -> None:
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = OpenSandboxFileTooLargeError("too large")
    result = await tools["deliver_files"].ainvoke(
        {"runtime": runtime, "files": [{"file_path": "/report.pdf", "name": "r.pdf"}]}
    )
    assert json.loads(result[0]["text"])["results"] == [
        {
            "file_path": "/report.pdf",
            "name": "r.pdf",
            "status": "failed",
            "error": "单个附件不能超过 10 MiB",
        }
    ]
    service.upload.assert_not_awaited()


async def test_batch_delivery_retains_success_and_reports_missing_files(
    attachment_tools,
):
    sandbox, service, tools, runtime = attachment_tools

    async def read(path, **kwargs):
        if path == "/missing.csv":
            raise FileNotFoundError(path)
        return b"id\n00123\n"

    sandbox.aread_bytes.side_effect = read
    result = await tools["deliver_files"].ainvoke(
        {
            "runtime": runtime,
            "files": [
                {"file_path": "report.csv", "name": "report.csv"},
                {"file_path": "/missing.csv", "name": "missing.csv"},
                {"file_path": "/report.csv", "name": "report.csv"},
            ],
        }
    )
    results = json.loads(result[0]["text"])["results"]
    assert [item["status"] for item in results] == ["delivered", "failed", "delivered"]
    assert "不存在" in results[1]["error"]
    assert len(result) == 2
    service.upload.assert_awaited_once()


async def test_batch_validates_every_path_before_any_upload(attachment_tools):
    sandbox, service, tools, runtime = attachment_tools
    with pytest.raises(ValueError, match="safe virtual path"):
        await tools["deliver_files"].ainvoke(
            {
                "runtime": runtime,
                "files": [
                    {"file_path": "/ok.csv", "name": "ok.csv"},
                    {"file_path": "/../secret.csv", "name": "secret.csv"},
                ],
            }
        )
    sandbox.aread_bytes.assert_not_awaited()
    service.upload.assert_not_awaited()


async def test_batch_storage_failure_is_not_reported_as_missing_source(
    attachment_tools,
):
    _, service, tools, runtime = attachment_tools
    service.upload.side_effect = OSError("storage unavailable")
    result = await tools["deliver_files"].ainvoke(
        {
            "runtime": runtime,
            "files": [
                {"file_path": "/report.csv", "name": "report.csv"},
            ],
        }
    )
    item = json.loads(result[0]["text"])["results"][0]
    assert item["status"] == "failed" and "存储" in item["error"]


async def test_workspace_path_rejection_is_not_bypassed(attachment_tools):
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = ValueError("工作区路径越界")
    with pytest.raises(ValueError, match="越界"):
        await tools["capture_browser"].ainvoke(
            {"runtime": runtime, "file_path": "/../other.html"}
        )
    sandbox.aexecute.assert_not_awaited()
    sandbox.aupload_files.assert_not_awaited()


async def test_repeated_cancel_waits_for_request_cleanup(attachment_tools):
    sandbox, _, tools, runtime = attachment_tools
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    released = asyncio.Event()
    finished = asyncio.Event()

    async def execute(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def delete(path):
        cleaning.set()
        await released.wait()
        finished.set()
        return DeleteResult(path=path)

    sandbox.aexecute.side_effect = execute
    sandbox.adelete.side_effect = delete
    task = asyncio.create_task(
        tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com"}
        )
    )
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        sandbox.adelete.assert_awaited_once()
    finally:
        released.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_partial_request_upload_is_removed(attachment_tools):
    sandbox, _, tools, runtime = attachment_tools
    files = {"/existing.html": b"<p>keep original</p>"}
    entered = asyncio.Event()
    released = asyncio.Event()

    async def upload(items):
        files.update(items)
        entered.set()
        await released.wait()
        return [FileUploadResponse(path=path) for path, _ in items]

    async def delete(path):
        del files[path]
        return DeleteResult(path=path)

    sandbox.aupload_files.side_effect = upload
    sandbox.adelete.side_effect = delete
    capture = asyncio.create_task(
        tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com"}
        )
    )
    try:
        await entered.wait()
        capture.cancel()
        with pytest.raises(asyncio.CancelledError):
            await capture
        assert files == {"/existing.html": b"<p>keep original</p>"}
        sandbox.aexecute.assert_not_awaited()
    finally:
        released.set()
        if not capture.done():
            capture.cancel()
        await asyncio.gather(capture, return_exceptions=True)
