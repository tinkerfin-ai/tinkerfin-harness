"""对话准确查询执行并复用已交付产物，实时输出与历史保持一致"""

import json

import pytest
from ag_ui.core import EventType, ToolCallResultEvent
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from test_agent_runtime import _model_config
from test_automation_conversation_tools import ConversationModel, tools_for
from test_automation_integration import (
    automation_environment as automation_environment,
)
from test_automation_integration import (
    automation_resources as automation_resources,
)
from test_automation_integration import (
    automation_worker as automation_worker,
)
from test_automation_integration import (
    configuration,
)

from tinkerfin.agui import AgUiHistory
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.automation.tools import build_automation_tools


@pytest.fixture
async def generated_runs(automation_resources, automation_worker):
    service = StudioAutomationService(automation_resources, user_id=1)
    saved = await service.save(
        SaveTask(
            request_id="reports",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    task = await automation_resources.automation.for_owner("1").task(saved.id)
    runs = []
    files = []
    for number in (1, 2):
        run = await task.run(request_id=f"report-{number}")
        await automation_worker.wait_until_idle()
        await run.refresh()
        assert run.succeeded
        runs.append(run)
        files.append(
            await automation_resources.attachments.upload(
                user_id=1,
                name=f"report-{number}.md",
                chunks=byte_chunks(f"# Report {number}\n".encode()),
                collection_id=run.id,
                source="tool",
            )
        )
    return saved, runs, files


async def test_query_distinguishes_input_files_and_two_execution_outputs(
    automation_resources, generated_runs
):
    task, runs, files = generated_runs
    tools = tools_for(automation_resources)
    saved = json.loads(await tools["get_automation"].ainvoke({"task_id": task.id}))
    assert saved["inputFiles"] == [] and "files" not in saved
    first = json.loads(
        await tools["list_automation_runs"].ainvoke({"task_id": task.id, "limit": 1})
    )
    second = json.loads(
        await tools["list_automation_runs"].ainvoke(
            {"task_id": task.id, "limit": 1, "cursor": first["nextCursor"]}
        )
    )
    assert {item["id"] for page in (first, second) for item in page["items"]} == {
        run.id for run in runs
    }
    assert second["nextCursor"] is None
    for run, file in zip(runs, files, strict=True):
        detail = json.loads(
            await tools["get_automation_run"].ainvoke({"execution_id": run.id})
        )
        assert detail["status"] == "succeeded" and detail["resultAvailable"]
        assert [item["id"] for item in detail["outputFiles"]] == [file.id]
        assert "attachments" not in detail
        assert all(message["role"] == "assistant" for message in detail["messages"])
        assert run.identity.namespace == "ns_1"
    assert runs[0].identity.thread_id != runs[1].identity.thread_id
    missing = json.loads(
        await tools["list_automation_runs"].ainvoke({"query": "没有匹配的任务"})
    )
    assert missing == {"items": [], "nextCursor": None}
    with pytest.raises(BusinessException):
        await tools["get_automation_run"].ainvoke({"execution_id": "missing"})


async def test_delivered_files_preserve_ids_without_creating_artifacts_or_runs(
    automation_resources, generated_runs
):
    task, runs, files = generated_runs
    tools = tools_for(automation_resources)
    async with automation_resources.database.session() as session:
        before = await session.scalar(select(func.count()).select_from(AttachmentFile))
    for _ in range(2):
        delivered = await tools["deliver_automation_files"].ainvoke(
            {"execution_id": runs[0].id, "attachment_ids": [files[0].id, files[0].id]}
        )
        assert delivered == [files[0].content_block()]
    async with automation_resources.database.session() as session:
        after = await session.scalar(select(func.count()).select_from(AttachmentFile))
    assert before == after
    history = await StudioAutomationService(automation_resources, user_id=1).list_runs(
        task_id=task.id
    )
    assert len(history.items) == 2
    with pytest.raises(BusinessException):
        await tools["deliver_automation_files"].ainvoke(
            {"execution_id": runs[1].id, "attachment_ids": [files[0].id]}
        )
    other = {
        tool.name: tool
        for tool in build_automation_tools(
            automation_resources, user_id=2, model_id="main", access_mode="full"
        )
    }
    assert json.loads(await other["list_automation_runs"].ainvoke({}))["items"] == []
    with pytest.raises(BusinessException):
        await other["get_automation_run"].ainvoke({"execution_id": runs[0].id})
    with pytest.raises(BusinessException):
        await other["deliver_automation_files"].ainvoke(
            {"execution_id": runs[0].id, "attachment_ids": [files[0].id]}
        )


async def test_existing_output_is_a_downloadable_file_in_live_and_replayed_messages(
    automation_resources, generated_runs, monkeypatch
):
    _, runs, files = generated_runs
    model = ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "deliver_automation_files",
                        "id": "send-report",
                        "args": {
                            "execution_id": runs[0].id,
                            "attachment_ids": [files[0].id],
                        },
                    }
                ],
            ),
            AIMessage(content="已发送已有报告"),
        ]
    )
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    runtime = build_conversation_runtime(
        resources=automation_resources,
        user_id=1,
        thread_id="result-chat",
        model_config=_model_config(),
        image_model=None,
    )
    stream = runtime.open_agui_run(
        thread_id="result-chat",
        run_id="get-existing-report",
        messages=[{"id": "request", "role": "user", "content": "发给我已有报告"}],
    )
    events = [event async for event in stream]
    assert stream.error is None
    types = [event.type for event in events]
    assert (
        types.count(EventType.RUN_STARTED) == types.count(EventType.RUN_FINISHED) == 1
    )
    assert EventType.RUN_ERROR not in types
    event = next(event for event in events if isinstance(event, ToolCallResultEvent))
    assert event.content == ""
    assert event.model_dump(mode="json")["attachments"] == [
        files[0].model_dump(mode="json")
    ]
    history = await AgUiHistory(automation_resources.tracer, namespace="ns_1").get(
        "result-chat", head_run_id="get-existing-report"
    )
    message = next(item for item in history.snapshot.messages if item.role == "tool")
    assert message.content == [files[0].content_block()]
