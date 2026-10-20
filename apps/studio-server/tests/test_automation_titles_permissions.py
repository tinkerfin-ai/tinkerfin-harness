"""自动化复用普通标题总结，并以完全访问执行文件任务"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import RunFinishedEvent, RunFinishedInterruptOutcome, RunStartedEvent
from deepagents.backends import FilesystemBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import ValidationError
from test_agent_runtime import _ToolModel, _Workspace
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_automation_integration import automation_worker as automation_worker
from test_automation_integration import configuration

from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import (
    build_automation_runtime,
    build_conversation_runtime,
)
from tinkerfin_studio.agent.subagents import SubagentSettings
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.conversation import titles as titles_module
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    StartChatIntent,
    classify_intent,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService


class _ActionsModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> BaseChatModel:
        return self


def _chat_request(prompt: str) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "threadId": "",
            "runId": "initial",
            "state": {},
            "tools": [],
            "context": [],
            "messages": [{"role": "user", "content": prompt}],
            "forwardedProps": {
                "projectId": "project-1",
                "model": "main",
                "command": {"plan": "off"},
            },
        }
    )


@pytest.mark.parametrize("field", ["accessMode", "access_mode"])
def test_automation_configuration_rejects_access_selection(field):
    with pytest.raises(ValidationError) as failure:
        TaskConfiguration.model_validate({**configuration(), field: "write_approval"})
    assert any(
        error["loc"] == (field,) and error["type"] == "extra_forbidden"
        for error in failure.value.errors()
    )


@pytest.mark.parametrize("role", ["main", "general-purpose", "researcher"])
@pytest.mark.parametrize("automation", [True, False], ids=["automation", "chat"])
async def test_file_writes_use_automation_full_access_and_chat_approval(
    automation_resources, monkeypatch, tmp_path, role, automation
):
    resources = automation_resources
    workspace = _Workspace(
        backend=FilesystemBackend(
            root_dir=str(tmp_path / "workspace"), virtual_mode=True
        )
    )
    monkeypatch.setattr(
        resources.sandbox_manager, "workspace", lambda *args, **kwargs: workspace
    )
    resources.agent_subagents["researcher"] = SubagentSettings(
        description="写入研究结果", system_prompt="完成文件任务", tools=[]
    )
    write = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "write-result",
                "name": "write_file",
                "args": {"file_path": "/result.txt", "content": "result"},
            }
        ],
    )
    responses: list[BaseMessage] = [write, AIMessage(content="完成")]
    if role != "main":
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "subagent_type": role,
                            "description": "将结果写入 /result.txt",
                        },
                    }
                ],
            ),
            *responses,
            AIMessage(content="已完成委派"),
        ]
    model = _ActionsModel(responses=responses)
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda *a, **k: model)
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")
    common = {
        "resources": resources,
        "user_id": 1,
        "thread_id": "file-task",
        "project_id": "project-1",
        "model_config": model_config,
        "search_service": None,
        "image_service": None,
    }
    runtime = (
        build_automation_runtime(**common, execution_id="file-execution")
        if automation
        else build_conversation_runtime(**common, access_mode="write_approval")
    )
    stream = runtime.open_agui_run(
        thread_id="file-task",
        run_id="file-run",
        input={"messages": [HumanMessage(content="写入结果文件")]},
    )
    events = [event async for event in stream]
    assert stream.error is None
    assert sum(isinstance(event, RunStartedEvent) for event in events) == 1
    assert sum(isinstance(event, RunFinishedEvent) for event in events) == 1
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome) is not automation
    path = tmp_path / "workspace" / "result.txt"
    if automation:
        assert path.read_text() == "result"
    else:
        assert not path.exists()
    assert workspace.opened == workspace.closed


async def test_chat_approval_does_not_become_task_configuration(
    automation_resources, monkeypatch
):
    resources = automation_resources
    model = _ActionsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "create-task",
                        "name": "create_automation",
                        "args": {
                            "name": "日报",
                            "prompt": "整理日报",
                            "schedule": {
                                "kind": "once",
                                "date": "2099-09-22",
                                "time": "09:00",
                            },
                        },
                    }
                ],
            ),
            AIMessage(content="已创建"),
        ]
    )
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda *a, **k: model)
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=1,
        thread_id="create-task",
        project_id="project-1",
        model_config=model_config,
        search_service=None,
        image_service=None,
        access_mode="write_approval",
    )
    stream = runtime.open_agui_run(
        thread_id="create-task",
        run_id="create-run",
        input={"messages": [HumanMessage(content="每天创建日报任务")]},
    )
    events = [event async for event in stream]
    assert stream.error is None
    assert isinstance(events[-1], RunFinishedEvent)
    assert not isinstance(events[-1].outcome, RunFinishedInterruptOutcome)
    tasks = await StudioAutomationService(
        resources, user_id=1, project_id="project-1"
    ).list_tasks()
    assert len(tasks.items) == 1
    saved = tasks.items[0].model_dump(mode="json", by_alias=True)
    assert saved["modelId"] == "main" and saved["prompt"] == "整理日报"
    assert "accessMode" not in saved and "access_mode" not in saved


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "renamed", "service_closed", "run_cancelled"]
)
async def test_automation_uses_conversation_title_lifecycle(
    automation_resources, automation_worker, monkeypatch, outcome
):
    resources = automation_resources
    title_entered, title_release, title_settled = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    main_entered = asyncio.Event()
    title_inputs = []
    prompt = "请整理本周全部研究资料并生成可阅读的总结报告"

    class TitleModel(FakeListChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            title_inputs.append(messages)
            title_entered.set()
            await title_release.wait()
            if outcome == "failure":
                raise ValueError("标题模型不可用")
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="本周研究总结"))]
            )

    class WaitingMainModel(_ToolModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            main_entered.set()
            await asyncio.Event().wait()
            async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                yield chunk

    monkeypatch.setattr(
        titles_module,
        "create_chat_model",
        lambda *a, **k: TitleModel(responses=["本周研究总结"]),
    )
    original_summary = titles_module.summarize_conversation_title

    async def summarize(**kwargs):
        try:
            return await original_summary(**kwargs)
        finally:
            title_settled.set()

    monkeypatch.setattr(titles_module, "summarize_conversation_title", summarize)
    if outcome == "run_cancelled":
        monkeypatch.setattr(
            runtime_module,
            "create_chat_model",
            lambda *a, **k: WaitingMainModel(responses=["完成"]),
        )
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="title-task",
            configuration=TaskConfiguration.model_validate(
                {**configuration(), "name": "不会用于会话标题的名称", "prompt": prompt}
            ),
        )
    )
    run = await (
        await resources.automation.for_owner("1:project-1").task(task.id)
    ).run()
    try:
        await title_entered.wait()
        intent = classify_intent(_chat_request(prompt))
        assert isinstance(intent, StartChatIntent)
        async with resources.database.session() as session:
            repository = ConversationRepository(session)
            thread = await repository.get_thread(
                user_id=1, thread_id=run.identity.thread_id
            )
            assert thread is not None
            assert thread.title == intent.title == prompt[:16]
            assert (thread.title_source, thread.title_generation_status) == (
                "default",
                "running",
            )
            assert thread.last_access_mode == "full"
            if outcome == "renamed":
                await repository.update_thread_meta(
                    thread, title="我的自定义标题", pinned=None
                )
                await repository.commit()
        if outcome == "run_cancelled":
            await main_entered.wait()
            await run.cancel()
        await automation_worker.wait_until_idle()
        if outcome == "service_closed":
            await resources.conversation_titles.aclose()
        else:
            title_release.set()
        await title_settled.wait()
        async with resources.database.session() as session:
            thread = await ConversationRepository(session).get_thread(
                user_id=1, thread_id=run.identity.thread_id
            )
            assert thread is not None
            expected = (
                ("我的自定义标题", "user", "skipped")
                if outcome == "renamed"
                else (prompt[:16], "default", "failed")
                if outcome in {"failure", "service_closed"}
                else ("本周研究总结", "generated", "succeeded")
            )
            assert (
                thread.title,
                thread.title_source,
                thread.title_generation_status,
            ) == expected
        assert len(title_inputs) == 1
        assert json.loads(title_inputs[0][-1].content) == prompt
        snapshot = await run.refresh()
        assert snapshot.status == (
            "needs_attention" if outcome == "run_cancelled" else "succeeded"
        )
    finally:
        title_release.set()
