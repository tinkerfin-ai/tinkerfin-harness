"""对话任务命令复用业务权限、版本与可重放身份"""

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import EventType, ToolCallResultEvent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import ExecutionInfo
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError
from test_agent_runtime import _model_config
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_automation_integration import automation_worker as automation_worker
from test_automation_integration import configuration

from tinkerfin.agui import AgUiHistory
from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.automation.schemas import TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.automation.tools import build_automation_tools


def call_runtime(task_id: str = "node", call_id: str = "call") -> ToolRuntime:
    class ConversationToolRuntime(ToolRuntime):
        @property
        def identity(self) -> RunIdentity:
            return RunIdentity(namespace="ns_1", thread_id="chat", run_id="run")

    return ConversationToolRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
        execution_info=ExecutionInfo(
            task_id=task_id, checkpoint_id="checkpoint", checkpoint_ns=""
        ),
    )


def tools_for(resources):
    return {
        item.name: item
        for item in build_automation_tools(
            resources, user_id=1, model_id="main", access_mode="full"
        )
    }


def creation():
    return {
        "name": "对话日报",
        "prompt": "生成独立日报",
        "schedule": configuration()["schedule"],
    }


async def test_schedule_schema_requires_objects_for_create_and_update(
    automation_resources,
):
    tools = tools_for(automation_resources)
    create = convert_to_openai_tool(tools["create_automation"])["function"]
    update = convert_to_openai_tool(tools["update_automation"])["function"]
    schedules = (
        create["parameters"]["properties"]["schedule"],
        update["parameters"]["properties"]["configuration"]["properties"]["schedule"],
    )
    for schedule in schedules:
        assert schedule["type"] == "object"
        assert schedule["discriminator"]["propertyName"] == "kind"

    with pytest.raises(ValidationError):
        await tools["create_automation"].ainvoke(
            {
                **creation(),
                "schedule": '{"kind":"interval","every":5,"unit":"minutes"}',
                "runtime": call_runtime(),
            }
        )
    assert not json.loads(await tools["list_automations"].ainvoke({}))["items"]


@pytest.mark.parametrize(
    "schedule",
    [
        {"kind": "once", "date": "2099-09-22", "time": "09:00"},
        {"kind": "daily", "time": "09:00"},
        {"kind": "workdays", "time": "09:00"},
        {"kind": "weekly", "weekdays": [0, 4], "time": "09:00"},
        {"kind": "monthly", "day": 15, "time": "09:00"},
        {"kind": "interval", "every": 5, "unit": "minutes"},
    ],
)
async def test_conversation_schedule_objects_keep_saved_json(
    automation_resources, schedule
):
    tools = tools_for(automation_resources)
    created = json.loads(
        await tools["create_automation"].ainvoke(
            {**creation(), "schedule": schedule, "runtime": call_runtime()}
        )
    )
    saved = await StudioAutomationService(automation_resources, user_id=1).get_task(
        created["id"]
    )
    serialized = saved.model_dump(mode="json", by_alias=True)
    assert created["schedule"] == serialized["schedule"] == schedule
    config = TaskConfiguration.model_validate({**configuration(), "schedule": schedule})
    assert TaskConfiguration.model_validate_json(config.model_dump_json()) == config
    changed = json.loads(
        await tools["update_automation"].ainvoke(
            {
                "task_id": created["id"],
                "expected_revision": created["revision"],
                "configuration": {**config.model_dump(mode="json"), "name": "改名"},
                "runtime": call_runtime(call_id="update"),
            }
        )
    )
    assert changed["name"] == "改名" and changed["schedule"] == schedule


async def test_conversation_commands_preserve_saved_configuration_and_replay(
    automation_resources, automation_worker
):
    tools = tools_for(automation_resources)
    assert len(tools) == 11
    for entry in tools.values():
        schema = entry.tool_call_schema
        assert not isinstance(schema, dict)
        assert issubclass(schema, BaseModel)
        properties = schema.model_json_schema()["properties"]
        assert not {
            "runtime",
            "owner_id",
            "namespace",
            "target",
            "request_id",
            "credentials",
            "input",
        }.intersection(properties)
    assert tools["get_automation"].metadata == {"read_only": True}
    assert tools["list_automations"].metadata == {"read_only": True}
    create = {**creation(), "runtime": call_runtime()}
    task = json.loads(await tools["create_automation"].ainvoke(create))
    replay = json.loads(await tools["create_automation"].ainvoke(create))
    assert replay == task
    assert task["modelId"] == "main" and task["accessMode"] == "full"
    assert task["prompt"] == "生成独立日报"
    listed = json.loads(await tools["list_automations"].ainvoke({"query": "对话"}))
    assert [item["id"] for item in listed["items"]] == [task["id"]]
    selected = json.loads(
        await tools["get_automation"].ainvoke({"task_id": task["id"]})
    )
    assert selected == task
    changed = {**configuration(), "name": "修改后的日报", "prompt": "完整新指令"}
    task = json.loads(
        await tools["update_automation"].ainvoke(
            {
                "task_id": task["id"],
                "expected_revision": 1,
                "configuration": changed,
                "runtime": call_runtime("edit"),
            }
        )
    )
    assert task["revision"] == 2 and task["name"] == "修改后的日报"
    with pytest.raises(BusinessException):
        await tools["pause_automation"].ainvoke(
            {
                "task_id": task["id"],
                "expected_revision": 1,
                "runtime": call_runtime("stale"),
            }
        )
    for action, enabled in (("pause_automation", False), ("enable_automation", True)):
        task = json.loads(
            await tools[action].ainvoke(
                {
                    "task_id": task["id"],
                    "expected_revision": task["revision"],
                    "runtime": call_runtime(action),
                }
            )
        )
        assert task["enabled"] is enabled
    command = {
        "task_id": task["id"],
        "expected_revision": task["revision"],
        "runtime": call_runtime("run"),
    }
    run = json.loads(await tools["run_automation_task_now"].ainvoke(command))
    assert (
        json.loads(await tools["run_automation_task_now"].ainvoke(command))["id"]
        == run["id"]
    )
    await automation_worker.wait_until_idle()
    assert (
        await StudioAutomationService(automation_resources, user_id=1).result(run["id"])
    ).status == "succeeded"
    deletion = {**command, "runtime": call_runtime("delete")}
    assert json.loads(await tools["delete_automation"].ainvoke(deletion)) == {
        "deleted": True
    }
    assert json.loads(await tools["delete_automation"].ainvoke(deletion)) == {
        "deleted": True
    }
    assert not json.loads(await tools["list_automations"].ainvoke({}))["items"]


async def test_distinct_native_tasks_do_not_share_request_keys(automation_resources):
    tool = tools_for(automation_resources)["create_automation"]
    first = json.loads(
        await tool.ainvoke({**creation(), "runtime": call_runtime("first", "same")})
    )
    second = json.loads(
        await tool.ainvoke({**creation(), "runtime": call_runtime("second", "same")})
    )
    assert first["id"] != second["id"]
    with pytest.raises(BusinessException):
        await tool.ainvoke(
            {**creation(), "name": "另一命令", "runtime": call_runtime("first", "same")}
        )


class ConversationModel(FakeMessagesListChatModel):
    seen_tools: list[set[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> BaseChatModel:
        self.seen_tools.append(
            {item.name for item in tools if isinstance(item, BaseTool)}
        )
        return self


async def test_runtime_injects_command_identity_and_records_tool_result(
    automation_resources, monkeypatch
):
    model = ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_automation",
                        "args": creation(),
                        "id": "create-call",
                    }
                ],
            ),
            AIMessage(content="已创建对话日报，可在自动化页面管理"),
        ]
    )
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    runtime = build_conversation_runtime(
        resources=automation_resources,
        user_id=1,
        thread_id="chat",
        model_config=_model_config(),
        image_model=None,
    )
    stream = runtime.open_agui_run(
        thread_id="chat",
        run_id="create-run",
        messages=[
            {"id": "user-message", "role": "user", "content": "创建一个独立日报任务"}
        ],
    )
    events = [event async for event in stream]
    assert stream.error is None, repr(stream.error)
    types = [event.type for event in events]
    assert (
        types.count(EventType.RUN_STARTED) == types.count(EventType.RUN_FINISHED) == 1
    )
    assert EventType.RUN_ERROR not in types
    assert (
        types.index(EventType.TOOL_CALL_START)
        < types.index(EventType.TOOL_CALL_END)
        < types.index(EventType.TOOL_CALL_RESULT)
    )
    result = next(event for event in events if isinstance(event, ToolCallResultEvent))
    task = json.loads(result.content)
    assert task["name"] == "对话日报"
    saved = await StudioAutomationService(automation_resources, user_id=1).get_task(
        task["id"]
    )
    assert saved.prompt == "生成独立日报"
    history = await AgUiHistory(
        automation_resources.tracer, namespace=runtime.namespace
    ).get("chat", head_run_id="create-run", limit=100)
    assert any(
        message.role == "tool" and task["id"] in str(message.content)
        for message in history.snapshot.messages
    )


async def test_other_users_cannot_query_or_delete_conversation_tasks(
    automation_resources,
):
    owned = tools_for(automation_resources)
    task = json.loads(
        await owned["create_automation"].ainvoke(
            {**creation(), "runtime": call_runtime()}
        )
    )
    others = {
        entry.name: entry
        for entry in build_automation_tools(
            automation_resources,
            user_id=2,
            model_id="main",
            access_mode="full",
        )
    }
    assert not json.loads(await others["list_automations"].ainvoke({}))["items"]
    with pytest.raises(BusinessException):
        await others["get_automation"].ainvoke({"task_id": task["id"]})
    with pytest.raises(BusinessException):
        await others["delete_automation"].ainvoke(
            {
                "task_id": task["id"],
                "expected_revision": task["revision"],
                "runtime": call_runtime(),
            }
        )
    with pytest.raises(BusinessException):
        await owned["create_automation"].ainvoke(
            {
                **creation(),
                "attachments": ["unavailable"],
                "runtime": call_runtime("attachment"),
            }
        )
    assert len(json.loads(await owned["list_automations"].ainvoke({}))["items"]) == 1


@pytest.mark.parametrize("mode", ["plan", "delegated"])
async def test_plan_and_default_subagent_cannot_create_tasks(
    automation_resources, monkeypatch, mode
):
    messages: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "create_automation", "args": creation(), "id": "forbidden"}
            ],
        ),
        AIMessage(content="由主对话在执行模式管理任务"),
    ]
    if mode == "delegated":
        messages.insert(
            0,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "subagent_type": "general-purpose",
                            "description": "创建日报任务",
                        },
                        "id": "delegate",
                    }
                ],
            ),
        )
        messages.append(AIMessage(content="任务没有创建"))
    model = ConversationModel(responses=messages)
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    runtime = build_conversation_runtime(
        resources=automation_resources,
        user_id=1,
        thread_id="limited",
        model_config=_model_config(),
        image_model=None,
    )
    stream = runtime.open_agui_run(
        thread_id="limited",
        run_id="limited-run",
        mode="plan" if mode == "plan" else "default",
        messages=[{"id": "user", "role": "user", "content": "制定一个日报任务"}],
    )
    _ = [event async for event in stream]
    assert stream.error is None, repr(stream.error)
    assert not (
        await StudioAutomationService(automation_resources, user_id=1).list_tasks()
    ).items
    assert any("create_automation" not in names for names in model.seen_tools)
    if mode == "plan":
        assert all("create_automation" not in names for names in model.seen_tools)


async def test_checkpoint_resume_replays_a_committed_tool_without_another_task(
    automation_resources,
):
    class InterruptAfterSave(AgentMiddleware):
        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ) -> ToolMessage | Command[Any]:
            saved = await handler(request)
            interrupt("任务已保存，恢复同一个工具调用")
            return saved

    model = ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_automation",
                        "args": creation(),
                        "id": "create-call",
                    }
                ],
            ),
            AIMessage(content="已创建任务"),
        ]
    )
    runtime = automation_resources.tinkerfin.with_namespace("ns_1").build(
        model=model,
        tools=list(tools_for(automation_resources).values()),
        middleware=(InterruptAfterSave(),),
    )
    first = await runtime.ainvoke(
        thread_id="checkpoint-chat",
        run_id="before-interrupt",
        input={"messages": [{"role": "user", "content": "创建日报"}]},
    )
    assert first["__interrupt__"]
    service = StudioAutomationService(automation_resources, user_id=1)
    before = (await service.list_tasks()).items
    assert len(before) == 1
    resumed = await runtime.ainvoke(
        thread_id="checkpoint-chat",
        run_id="after-interrupt",
        input=Command(resume=True),
    )
    assert not resumed.get("__interrupt__")
    after = (await service.list_tasks()).items
    assert [item.id for item in after] == [before[0].id]
