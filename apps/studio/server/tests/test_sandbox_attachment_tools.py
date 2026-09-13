"""Sandbox 附件有界读取、截图产物保留和交付失败边界"""

import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepagents.backends.protocol import ExecuteResponse

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import OpenSandboxFileTooLargeError, RootedOpenSandboxBackend
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.attachments.sandbox_tools import build_sandbox_attachment_tools
from tinkerfin_studio.attachments.service import AttachmentService


@pytest.fixture
def attachment_tools():
    sandbox = MagicMock(spec=RootedOpenSandboxBackend)
    sandbox.aread_bytes = AsyncMock(return_value=b"binary")
    sandbox.aexecute = AsyncMock(return_value=ExecuteResponse(output="", exit_code=0))
    sandbox.to_shell_path.side_effect = lambda path: "/workspace" + path
    service = MagicMock(spec=AttachmentService)
    service.upload = AsyncMock(
        return_value=SimpleNamespace(content_block=lambda: {"type": "file", "id": "a"})
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


async def test_deliver_file_uses_bounded_workspace_read(attachment_tools) -> None:
    sandbox, service, tools, runtime = attachment_tools
    result = await tools["deliver_file"].ainvoke(
        {"runtime": runtime, "file_path": "/report.pdf", "name": "r.pdf"}
    )
    assert result == [{"type": "file", "id": "a"}]
    sandbox.aread_bytes.assert_awaited_once_with(
        "/report.pdf", max_bytes=MAX_FILE_BYTES
    )
    sandbox.aexecute.assert_not_awaited()
    args = service.upload.call_args.kwargs
    assert args["user_id"] == 1
    assert args["thread_id"] == "thread"
    assert args["source"] == "tool"
    assert b"".join([chunk async for chunk in args["chunks"]]) == b"binary"


async def test_deliver_file_does_not_upload_overflow(attachment_tools) -> None:
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = OpenSandboxFileTooLargeError("too large")
    with pytest.raises(OpenSandboxFileTooLargeError):
        await tools["deliver_file"].ainvoke(
            {"runtime": runtime, "file_path": "/report.pdf", "name": "r.pdf"}
        )
    service.upload.assert_not_awaited()


async def test_concurrent_screenshots_keep_distinct_reusable_workspace_artifacts(
    attachment_tools,
) -> None:
    sandbox, service, tools, runtime = attachment_tools
    results = await asyncio.gather(
        tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com/a"}
        ),
        tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com/b"}
        ),
    )
    paths = [call.args[0] for call in sandbox.aread_bytes.await_args_list]
    assert len(set(paths)) == 2
    assert all(
        path.startswith("/browser-capture-") and path.endswith(".png") for path in paths
    )
    commands = [shlex.split(call.args[0]) for call in sandbox.aexecute.await_args_list]
    assert len(commands) == 2
    assert {parts[-1] for parts in commands} == {"/workspace" + path for path in paths}
    assert all(parts[0] == "python" for parts in commands)
    assert service.upload.await_count == 2
    for path, result in zip(paths, results, strict=True):
        assert path in result[0]["text"]
        assert result[1] == {"type": "file", "id": "a"}


async def test_cancelled_capture_propagates_without_uploading_or_deleting_artifacts(
    attachment_tools,
) -> None:
    sandbox, service, tools, runtime = attachment_tools
    started = asyncio.Event()

    async def execute(command: str, *, timeout: int) -> ExecuteResponse:
        started.set()
        await asyncio.Event().wait()
        return ExecuteResponse(output="", exit_code=0)

    sandbox.aexecute.side_effect = execute
    task = asyncio.create_task(
        tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com"}
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.upload.assert_not_awaited()
    sandbox.aread_bytes.assert_not_awaited()
    assert sandbox.aexecute.await_count == 1


@pytest.mark.parametrize(
    "failure", [ConnectionError("connection lost"), TimeoutError("deadline")]
)
async def test_uncertain_capture_does_not_claim_delivery_or_delete_outputs(
    attachment_tools, failure
):
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aexecute.side_effect = failure
    with pytest.raises(type(failure)):
        await tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com"}
        )
    assert sandbox.aexecute.await_count == 1
    sandbox.aread_bytes.assert_not_awaited()
    service.upload.assert_not_awaited()


async def test_capture_overflow_keeps_workspace_artifact_without_upload(
    attachment_tools,
):
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = OpenSandboxFileTooLargeError("too large")
    with pytest.raises(OpenSandboxFileTooLargeError):
        await tools["capture_browser"].ainvoke(
            {"runtime": runtime, "url": "https://example.com"}
        )
    service.upload.assert_not_awaited()
    assert sandbox.aexecute.await_count == 1
