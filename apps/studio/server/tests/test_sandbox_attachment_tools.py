"""Sandbox 附件有界读取、截图产物保留和交付失败边界"""

import asyncio
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.messages import ToolMessage

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import OpenSandboxFileTooLargeError, RootedOpenSandboxBackend
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.processing import MAX_FILE_BYTES
from tinkerfin_studio.attachments.sandbox_tools import build_sandbox_attachment_tools
from tinkerfin_studio.attachments.service import AttachmentService


@pytest.fixture
def attachment_tools():
    sandbox = MagicMock(spec=RootedOpenSandboxBackend)
    sandbox.aread_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nimage")
    sandbox.aexecute = AsyncMock(return_value=ExecuteResponse(output="", exit_code=0))
    sandbox.to_shell_path.side_effect = lambda path: "/workspace" + path
    service = MagicMock(spec=AttachmentService)
    service.documents = DocumentProcessor()
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
    assert (
        b"".join([chunk async for chunk in args["chunks"]]) == b"\x89PNG\r\n\x1a\nimage"
    )


async def test_deliver_file_does_not_upload_overflow(attachment_tools) -> None:
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = OpenSandboxFileTooLargeError("too large")
    with pytest.raises(OpenSandboxFileTooLargeError):
        await tools["deliver_file"].ainvoke(
            {"runtime": runtime, "file_path": "/report.pdf", "name": "r.pdf"}
        )
    service.upload.assert_not_awaited()


async def test_missing_delivery_source_returns_error_result_without_upload(
    attachment_tools,
):
    sandbox, service, tools, runtime = attachment_tools
    sandbox.aread_bytes.side_effect = FileNotFoundError("private backend path")
    result = await tools["deliver_file"].ainvoke(
        {
            "type": "tool_call",
            "id": "missing-file",
            "name": "deliver_file",
            "args": {
                "runtime": runtime,
                "file_path": "/missing.pdf",
                "name": "report.pdf",
            },
        }
    )
    assert isinstance(result, ToolMessage)
    assert result.status == "error" and result.tool_call_id == "missing-file"
    assert "工作文件不存在" in result.content
    assert "private backend path" not in result.content
    service.upload.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("storage missing"),
        ConnectionError("storage unavailable"),
        asyncio.CancelledError(),
    ],
)
async def test_delivery_storage_failure_is_not_reported_as_missing_source(
    attachment_tools, failure
):
    _, service, tools, runtime = attachment_tools
    service.upload.side_effect = failure
    with pytest.raises(type(failure)):
        await tools["deliver_file"].ainvoke(
            {"runtime": runtime, "file_path": "/ready.pdf", "name": "report.pdf"}
        )


async def test_missing_file_allows_model_reply_and_preserves_failed_tool_trace(
    attachment_tools,
):
    """预期交付失败返回模型，Trace 保留工具失败且对话正常结束"""
    from contextlib import asynccontextmanager

    from deepagents.backends import StateBackend
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    from tinkerfin import TinkerFin
    from tinkerfin_contracts import PreparedWorkspace
    from tinkerfin_tracing import Tracer

    sandbox, service, tools, _ = attachment_tools
    sandbox.aread_bytes.side_effect = FileNotFoundError("missing")

    class Workspace:
        @asynccontextmanager
        async def prepare(self, identity):
            yield PreparedWorkspace(workspace=sandbox, backend=StateBackend())

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "missing",
                        "name": "deliver_file",
                        "args": {"file_path": "/missing.pdf", "name": "result.pdf"},
                    }
                ],
            ),
            AIMessage(content="文件不存在，未交付附件"),
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("file-error")
        .with_observer(tracer)
        .build(model=model, tools=[tools["deliver_file"]], backend=Workspace())
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="run",
        messages=[{"id": "input", "role": "user", "content": "交付文件"}],
    )
    try:
        events = [
            event.model_dump(mode="json", by_alias=True) async for event in stream
        ]
        assert stream.error is None
    finally:
        await stream.aclose()
    result = next(event for event in events if event["type"] == "TOOL_CALL_RESULT")
    assert "工作文件不存在" in result["content"] and result["attachments"] == []
    assert sum(event["type"] == "RUN_FINISHED" for event in events) == 1
    assert not any(event["type"] == "RUN_ERROR" for event in events)
    service.upload.assert_not_awaited()
    history = await tracer.get(runtime.thread_identity("thread"))
    assert "文件不存在，未交付附件" in str(history.messages)
    graph = await tracer.query(runtime.thread_identity("thread"))
    assert any(node.kind == "tool" and node.status == "failed" for node in graph.nodes)


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
    service.upload.assert_not_awaited()
    for path, result in zip(paths, results, strict=True):
        assert json.loads(result) == {
            "file_path": path,
            "name": "浏览器截图.png",
            "mime_type": "image/png",
            "size_bytes": len(b"\x89PNG\r\n\x1a\nimage"),
        }
    selected = json.loads(results[0])
    delivered = await tools["deliver_file"].ainvoke(
        {
            "runtime": runtime,
            "file_path": selected["file_path"],
            "name": selected["name"],
        }
    )
    assert delivered == [{"type": "file", "id": "a"}]
    service.upload.assert_awaited_once()


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
