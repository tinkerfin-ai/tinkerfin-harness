"""Studio Runtime 的模型配置、用户隔离与惰性工作区"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import create_autospec

import httpx
import pytest
from ag_ui.core import RunAgentInput
from deepagents.backends import BackendProtocol, StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field, SecretStr, ValidationError

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin_automation import Automation
from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    OpenSandboxBackendUnavailableError,
    RootedOpenSandboxBackend,
)
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.agent.subagents import load_subagents
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import prepare_run_request
from tinkerfin_studio.models import providers as providers_module
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.resources import ApplicationResources


@pytest.fixture
async def model_http_client():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        yield client


def _model_config() -> AgentModelConfig:
    return AgentModelConfig(
        model_id="main",
        display_name="Main",
        provider="openai",
        model_name="provider-main",
        base_url="https://models.example.test/v1",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
    )


def _run_input(*, mode: str = "default") -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [
                {"id": "client-message-1", "role": "user", "content": "执行任务"}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "on" if mode == "plan" else "off"},
            },
        }
    )


def _prepared(*, mode: str = "default"):
    return prepare_run_request(
        ChatRequest.from_agui(_run_input(mode=mode)),
        user_id=7,
        thread_id="thread-1",
    )


def test_studio_plan_content_requires_dynamic_description_and_markdown() -> None:
    schema = StudioMarkdownPlanContent.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {"description", "markdown"}
    assert schema["properties"]["description"]["maxLength"] == 80
    content = StudioMarkdownPlanContent(
        description="先澄清范围，再按步骤实现并验证",
        markdown="# Plan\n\n1. Clarify\n2. Implement",
    )
    assert content.description == "先澄清范围，再按步骤实现并验证"
    with pytest.raises(ValidationError):
        StudioMarkdownPlanContent(description="", markdown="# Plan")


@pytest.mark.parametrize(
    ("reasoning_enabled", "thinking_type", "has_reasoning_effort"),
    ((True, "enabled", True), (False, "disabled", False)),
)
def test_create_deepseek_model_explicitly_controls_thinking(
    monkeypatch: pytest.MonkeyPatch,
    reasoning_enabled: bool,
    thinking_type: str,
    has_reasoning_effort: bool,
) -> None:
    """把 Studio reasoning 开关转换为明确的模型参数"""

    captured: dict[str, object] = {}
    model = FakeListChatModel(responses=["unused"])

    def init_model(model_name: str, **kwargs: object):
        captured["model_name"] = model_name
        captured.update(kwargs)
        return model

    monkeypatch.setattr(providers_module, "init_chat_model", init_model)
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": reasoning_enabled,
        }
    )

    assert create_chat_model(config) is model
    assert captured["extra_body"] == {"thinking": {"type": thinking_type}}
    assert ("reasoning_effort" in captured) is has_reasoning_effort


class _ToolModel(FakeListChatModel):
    """记录模型实际可调用的工具，并返回固定回复"""

    seen_tools: list[set[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> BaseChatModel:
        self.seen_tools.append(
            {tool.name for tool in tools if isinstance(tool, BaseTool)}
        )
        return self


class _Workspace:
    """通过同步信号记录执行期间的用户工作区借用"""

    def __init__(self, failure: Exception | None = None) -> None:
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []
        self.failure = failure

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol]]:
        self.opened.append(identity)
        try:
            if self.failure is not None:
                raise self.failure
            yield PreparedWorkspace(
                workspace=create_autospec(RootedOpenSandboxBackend, instance=True),
                backend=StateBackend(),
            )
        finally:
            self.closed.append(identity)


@pytest.mark.parametrize("workspace_failure", (False, True))
async def test_runtime_build_is_separate_from_user_workspace_execution(
    monkeypatch: pytest.MonkeyPatch,
    attachments,
    model_http_client,
    workspace_failure: bool,
) -> None:
    """构建只绑定配置，执行才借用用户工作区且始终结束本轮借用"""

    root_model = _ToolModel(responses=["root"])
    reasoning_overrides: list[bool | None] = []
    failure = OpenSandboxBackendUnavailableError("Sandbox control plane unavailable")
    workspace = _Workspace(failure if workspace_failure else None)

    def create_model(
        config: AgentModelConfig,
        *,
        reasoning_enabled: bool | None = None,
        http_async_client=None,
        http_async_transport=None,
    ) -> BaseChatModel:
        reasoning_overrides.append(reasoning_enabled)
        return root_model

    class Sandboxes:
        def workspace(
            self, key: str, *, routes: dict[str, BackendProtocol]
        ) -> _Workspace:
            assert key == "users/7"
            assert set(routes) == {"/memories/"}
            return workspace

    monkeypatch.setattr(runtime_module, "create_chat_model", create_model)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            automation=Automation(namespace="studio_automation"),
            attachments=attachments,
            model_http_transport=None,
            model_http_client=model_http_client,
            agent_persistence=SimpleNamespace(store=InMemoryStore()),
            agent_subagents=await load_subagents(),
            tinkerfin=TinkerFin(checkpointer=InMemorySaver()),
            sandbox_manager=Sandboxes(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
        ),
    )
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": True,
        }
    )
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=7,
        thread_id="thread-1",
        model_config=config,
        image_model=None,
    )
    assert isinstance(runtime, AgentRuntime)
    assert runtime.namespace == "ns_7"
    assert reasoning_overrides == [None]
    assert workspace.opened == []
    stream = runtime.open_agui_run(
        thread_id="thread-1",
        run_id="unused",
        messages=[{"role": "user", "content": "unused"}],
    )
    await stream.aclose()
    assert workspace.opened == []

    async def execute() -> Mapping[str, object]:
        return await runtime.ainvoke(
            thread_id="thread-1",
            run_id="run-1",
            input={"messages": [{"role": "user", "content": "你好"}]},
        )

    if workspace_failure:
        with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
            await execute()
        assert captured.value is failure
    else:
        result = await execute()
        messages = result["messages"]
        assert isinstance(messages, list)
        final_message = messages[-1]
        assert isinstance(final_message, AIMessage)
        assert final_message.content == "root"
        assert {
            "compact_conversation",
            "web_search",
            "read_attachment",
            "deliver_file",
            "capture_browser",
            "write_todos",
        } <= root_model.seen_tools[-1]
    assert workspace.opened == [runtime.run_identity("thread-1", "run-1")]
    assert workspace.closed == workspace.opened


@pytest.mark.parametrize("access_mode", ["write_approval", "full"])
@pytest.mark.parametrize("delegated", [False, True])
async def test_file_access_choice_controls_root_and_subagent_review(
    monkeypatch: pytest.MonkeyPatch,
    attachments,
    model_http_client,
    access_mode,
    delegated: bool,
) -> None:
    """普通与委派写入均遵守保存的文件审批选择"""
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    class FileModel(FakeMessagesListChatModel):
        def bind_tools(
            self,
            tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
            **kwargs: Any,
        ) -> BaseChatModel:
            return self

    responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "write",
                    "args": {"file_path": "/result.txt", "content": "result"},
                }
            ],
        ),
        AIMessage(content="完成"),
    ]
    if delegated:
        responses.insert(
            0,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": "delegate",
                        "args": {
                            "subagent_type": "researcher",
                            "description": "写入结果",
                        },
                    }
                ],
            ),
        )
        responses.append(AIMessage(content="完成委派"))
    model = FileModel(responses=responses)
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    workspace = _Workspace()

    class Sandboxes:
        def workspace(
            self, key: str, *, routes: dict[str, BackendProtocol]
        ) -> _Workspace:
            assert key == "users/7"
            return workspace

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            automation=Automation(namespace="studio_automation"),
            attachments=attachments,
            model_http_transport=None,
            model_http_client=model_http_client,
            agent_subagents=await load_subagents(),
            tinkerfin=TinkerFin(checkpointer=InMemorySaver()),
            sandbox_manager=Sandboxes(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
        ),
    )
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=7,
        thread_id="thread-1",
        model_config=_model_config(),
        image_model=None,
        access_mode=access_mode,
    )
    result = await runtime.ainvoke(
        thread_id="thread-1",
        run_id="access-test",
        input={"messages": [{"role": "user", "content": "写入结果"}]},
    )
    assert bool(result.get("__interrupt__")) is (access_mode == "write_approval")
    assert workspace.opened == workspace.closed


@pytest.mark.parametrize("delegated", [False, True])
async def test_product_tool_failure_allows_root_and_researcher_to_reply(
    monkeypatch, attachments, model_http_client, delegated
):
    """文件工具校验失败返回对应角色，委派本身可以正常完成"""
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    from tinkerfin_tracing import Tracer

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "invalid-file",
                    "name": "create_file",
                    "args": {"name": "wrong.txt", "kind": "md", "text": "hello"},
                }
            ],
        ),
        AIMessage(content="文件名格式无效，未生成文件"),
    ]
    if delegated:
        responses.insert(
            0,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "subagent_type": "researcher",
                            "description": "准备报告",
                        },
                    }
                ],
            ),
        )
        responses.append(AIMessage(content="已确认报告未生成"))
    model = Model(responses=responses)
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    workspace = _Workspace()

    class Sandboxes:
        def workspace(self, key, *, routes):
            return workspace

    tracer = Tracer()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            automation=Automation(namespace="studio_automation"),
            attachments=attachments,
            model_http_transport=None,
            model_http_client=model_http_client,
            agent_subagents=await load_subagents(),
            tinkerfin=TinkerFin(checkpointer=InMemorySaver()).with_observer(tracer),
            sandbox_manager=Sandboxes(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
        ),
    )
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=7,
        thread_id="tool-error",
        model_config=_model_config(),
        image_model=None,
    )
    result = await runtime.ainvoke(
        thread_id="tool-error",
        run_id="run",
        input={"messages": [{"role": "user", "content": "准备报告"}]},
    )
    messages = result["messages"]
    assert isinstance(messages, list)
    assert "未生成" in str(messages[-1])
    graph = await tracer.query(runtime.thread_identity("tool-error"))
    tools = [node for node in graph.nodes if node.kind == "tool"]
    assert sum(node.status == "failed" for node in tools) == 1
    if delegated:
        assert any(
            node.kind == "subagent" and node.status == "succeeded"
            for node in graph.nodes
        )
    assert workspace.opened == workspace.closed
