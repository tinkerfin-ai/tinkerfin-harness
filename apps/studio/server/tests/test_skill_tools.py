"""合并技能管理工具的入参、权限与已提交操作重放"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from ag_ui.core import EventType, ToolCallResultEvent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from skill_fakes import MemorySkillSource
from sqlalchemy.ext.asyncio import AsyncSession
from test_agent_runtime import _model_config
from test_automation_conversation_tools import ConversationModel, call_runtime
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_skills_library import add_users, skill_files

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiHistory
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import (
    build_automation_runtime,
    build_conversation_runtime,
)
from tinkerfin_studio.api.errors import SkillErrorCode
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import parse_package
from tinkerfin_studio.skills.tools import build_skill_tools


def install_operation():
    return {
        "action": "install",
        "target": {
            "kind": "catalog",
            "source_id": "catalog",
            "skill_id": "author/reports",
            "revision": "first",
        },
    }


async def test_skill_tool_schema_and_invalid_operation_fields(
    skill_library: SkillLibrary,
):
    tools = {
        entry.name: entry
        for entry in build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        )
    }
    assert set(tools) == {
        "list_skill_sources",
        "search_skills",
        "list_skills",
        "get_skill",
        "preview_skills",
        "manage_skill",
        "set_skill_enabled",
    }
    for entry in tools.values():
        properties = convert_to_openai_tool(entry)["function"]["parameters"][
            "properties"
        ]
        assert not {"runtime", "user_id", "namespace", "request_id"}.intersection(
            properties
        )
    runtime = call_runtime()
    for operation in [
        {"action": "delete", "installation_id": "id"},
        {"action": "update"},
        {"action": "uninstall", "installation_id": "id", "replacement": {}},
    ]:
        with pytest.raises(ValidationError):
            await tools["manage_skill"].ainvoke(
                {"operation": operation, "runtime": runtime}
            )
    with pytest.raises(ValidationError):
        await tools["manage_skill"].ainvoke(
            {"operation": install_operation(), "runtime": runtime, "user_id": 2}
        )
    with pytest.raises(ValidationError):
        await tools["set_skill_enabled"].ainvoke(
            {"installation_id": "id", "enabled": "false", "runtime": runtime}
        )


async def test_shared_skill_tools_manage_lifecycle_and_return_business_errors(
    session: AsyncSession, skill_library: SkillLibrary, skill_catalog: MemorySkillSource
):
    await add_users(session)
    tools = {
        entry.name: entry
        for entry in build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        )
    }
    assert (
        json.loads(await tools["list_skill_sources"].ainvoke({}))[0]["id"] == "catalog"
    )
    assert (
        json.loads(await tools["search_skills"].ainvoke({"source_id": "catalog"}))[
            "items"
        ][0]["revision"]
        == "first"
    )
    request = {"operation": install_operation(), "runtime": call_runtime()}
    result = json.loads(await tools["manage_skill"].ainvoke(request))
    installed = result["result"]["installation"]
    assert result["effective_from"] == "next_run"
    assert json.loads(await tools["manage_skill"].ainvoke(request)) == result
    assert (
        json.loads(await tools["list_skills"].ainvoke({}))[0]["id"] == installed["id"]
    )
    detail = json.loads(
        await tools["get_skill"].ainvoke(
            {"target": {"kind": "installed", "installation_id": installed["id"]}}
        )
    )
    assert "Original" in detail["markdown"]
    disabled = json.loads(
        await tools["set_skill_enabled"].ainvoke(
            {
                "installation_id": installed["id"],
                "enabled": False,
                "runtime": call_runtime(call_id="disable"),
            }
        )
    )
    assert not disabled["result"]["installation"]["enabled"]
    skill_catalog.packages["second"] = parse_package(skill_files())
    skill_catalog.revision = "second"
    update = {
        "operation": {"action": "update", "installation_id": installed["id"]},
        "runtime": call_runtime(call_id="update"),
    }
    assert json.loads(await tools["manage_skill"].ainvoke(update))["result"]["changed"]
    conflict = json.loads(
        await tools["manage_skill"].ainvoke(
            {**request, "operation": update["operation"]}
        )
    )
    assert conflict["code"] == int(SkillErrorCode.OPERATION_CONFLICT)
    foreign = {
        entry.name: entry
        for entry in build_skill_tools(
            skill_library, project_id="project-2", user_id=2, thread_id="other"
        )
    }
    message = await foreign["manage_skill"].ainvoke(
        {
            "type": "tool_call",
            "id": "foreign",
            "name": "manage_skill",
            "args": {
                "operation": {
                    "action": "uninstall",
                    "installation_id": installed["id"],
                },
                "runtime": call_runtime(call_id="foreign"),
            },
        }
    )
    assert isinstance(message, ToolMessage) and message.status == "error"
    assert json.loads(str(message.content))["code"] == int(SkillErrorCode.NOT_FOUND)
    remove = {
        "operation": {"action": "uninstall", "installation_id": installed["id"]},
        "runtime": call_runtime(call_id="remove"),
    }
    assert await tools["manage_skill"].ainvoke(remove) == await tools[
        "manage_skill"
    ].ainvoke(remove)
    assert await skill_library.list(1, project_id="project-1") == []


async def test_skill_mutation_checkpoint_rebuild_reuses_committed_result(
    session: AsyncSession, skill_library: SkillLibrary, skill_catalog: MemorySkillSource
):
    await add_users(session)

    class InterruptAfterSave(AgentMiddleware):
        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        ) -> ToolMessage | Command[Any]:
            saved = await handler(request)
            interrupt("技能已提交，恢复同一工具调用")
            return saved

    model = ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "manage_skill",
                        "id": "install",
                        "args": {"operation": install_operation()},
                    }
                ],
            ),
            AIMessage(content="技能已安装，将从下一轮生效"),
        ]
    )
    tinkerfin = TinkerFin(checkpointer=InMemorySaver()).with_namespace("ns_1")
    runtime = tinkerfin.build(
        model=model,
        tools=build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        ),
        middleware=(InterruptAfterSave(),),
    )
    first = await runtime.ainvoke(
        thread_id="chat",
        run_id="first",
        input={"messages": [{"role": "user", "content": "安装报告技能"}]},
    )
    assert first["__interrupt__"]
    installed = await skill_library.list(1, project_id="project-1")
    assert len(installed) == 1
    rebuilt = tinkerfin.build(
        model=model,
        tools=build_skill_tools(
            skill_library, project_id="project-1", user_id=1, thread_id="chat"
        ),
        middleware=(InterruptAfterSave(),),
    )
    resumed = await rebuilt.ainvoke(
        thread_id="chat", run_id="resumed", input=Command(resume=True)
    )
    assert not resumed.get("__interrupt__")
    assert (await skill_library.list(1, project_id="project-1")) == installed
    assert skill_catalog.downloads == 1


@pytest.mark.parametrize("role", ["chat", "plan", "delegated", "automation"])
async def test_skill_management_roles_and_agui_history(
    automation_resources, monkeypatch: pytest.MonkeyPatch, role: str
):
    responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "manage_skill",
                    "id": "install",
                    "args": {"operation": install_operation()},
                }
            ],
        ),
        AIMessage(content="处理完成"),
    ]
    if role == "delegated":
        responses.insert(
            0,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": "delegate",
                        "args": {
                            "subagent_type": "general-purpose",
                            "description": "安装技能",
                        },
                    }
                ],
            ),
        )
        responses.append(AIMessage(content="返回主会话"))
    model = ConversationModel(responses=responses)
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    if role == "automation":
        runtime = build_automation_runtime(
            project_id="project-1",
            resources=automation_resources,
            user_id=1,
            thread_id="skills-chat",
            model_config=_model_config(),
            search_service=None,
            image_service=None,
            access_mode="full",
            execution_id="execution",
        )
    else:
        runtime = build_conversation_runtime(
            project_id="project-1",
            resources=automation_resources,
            user_id=1,
            thread_id="skills-chat",
            model_config=_model_config(),
            search_service=None,
            image_service=None,
            access_mode="full",
        )
    stream = runtime.open_agui_run(
        thread_id="skills-chat",
        run_id="skills-run",
        mode="plan" if role == "plan" else "default",
        messages=[{"id": "user", "role": "user", "content": "安装报告技能"}],
    )
    events = [event async for event in stream]
    assert stream.error is None, repr(stream.error)
    items = await automation_resources.skills.list(1, project_id="project-1")
    if role in {"chat", "plan"}:
        assert len(items) == 1
        assert all(
            {"manage_skill", "set_skill_enabled"} <= names for names in model.seen_tools
        )
        types = [event.type for event in events]
        assert (
            types.count(EventType.RUN_STARTED)
            == types.count(EventType.RUN_FINISHED)
            == 1
        )
        assert (
            types.index(EventType.TOOL_CALL_START)
            < types.index(EventType.TOOL_CALL_END)
            < types.index(EventType.TOOL_CALL_RESULT)
        )
        live_results = [
            event
            for event in events
            if isinstance(event, ToolCallResultEvent) and items[0].id in event.content
        ]
        assert len(live_results) == 1
        history = await AgUiHistory(
            automation_resources.tracer, namespace=runtime.namespace
        ).get("skills-chat", head_run_id="skills-run", limit=100)
        saved_results = [
            message
            for message in history.snapshot.messages
            if message.role == "tool" and items[0].id in str(message.content)
        ]
        assert len(saved_results) == 1
        live, saved = live_results[0], saved_results[0]
        assert saved.content == live.content
        assert saved.agui is not None and saved.agui.kind == "tool_message"
        assert saved.agui.message_id == live.message_id
        assert saved.agui.tool_call_id == live.tool_call_id
    else:
        assert items == []
        assert any("manage_skill" not in names for names in model.seen_tools)


pytestmark = pytest.mark.usefixtures("projects")
