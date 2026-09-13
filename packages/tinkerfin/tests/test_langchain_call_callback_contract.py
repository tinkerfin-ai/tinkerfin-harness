"""Locked LangChain callback evidence used by Runtime call observation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import tool
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool


class _ToolBindingModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _FailOnceModel(_ToolBindingModel):
    failures_remaining: int = 1

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("retryable provider failure")
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


class _RewriteRequest(AgentMiddleware[Any, Any, Any]):
    @property
    def name(self) -> str:
        return "rewrite-request"

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> ModelResponse[Any]:
        rewritten = request.override(
            messages=[HumanMessage(content="rewritten-user", id="rewritten-user-id")],
            system_message=SystemMessage(content="rewritten-system"),
            model_settings={"temperature": 0.25, "max_tokens": 77},
        )
        return await handler(rewritten)


class _AllAsyncHooks(AgentMiddleware[Any, Any, Any]):
    @property
    def name(self) -> str:
        return "all-hooks"

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        del state, runtime

    async def abefore_model(self, state: Any, runtime: Any) -> None:
        del state, runtime

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> ModelResponse[Any]:
        return await handler(request)

    async def aafter_model(self, state: Any, runtime: Any) -> None:
        del state, runtime

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        del state, runtime


class _ChainRecorder(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []

    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, inputs, kwargs
        self.starts.append(
            {
                "kind": "chain",
                "name": name,
                "run_id": str(run_id),
                "parent_run_id": None if parent_run_id is None else str(parent_run_id),
                "checkpoint_namespace": None
                if metadata is None
                else metadata.get("langgraph_checkpoint_ns"),
            }
        )

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        self.starts.append(
            {
                "kind": "model",
                "name": "Model",
                "run_id": str(run_id),
                "parent_run_id": None if parent_run_id is None else str(parent_run_id),
                "checkpoint_namespace": None,
            }
        )


@tool
async def add_values(a: int, b: int) -> str:
    """Add two values.

    Args:
        a: First value.
        b: Second value.

    Returns:
        Decimal sum.
    """

    return str(a + b)


class _Recorder(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, parent_run_id
        self.events.append(
            (
                "model.started",
                {
                    "run_id": str(run_id),
                    "messages": tuple(
                        (message.type, message.content, message.id)
                        for message in messages[0]
                    ),
                    "metadata": metadata,
                    "invocation_params": kwargs.get("invocation_params"),
                    "options": kwargs.get("options"),
                },
            )
        )

    async def on_llm_end(
        self,
        response: object,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        generations = getattr(response, "generations", ())
        message_ids = tuple(
            message.id
            for batch in generations
            for generation in batch
            if isinstance(
                (message := getattr(generation, "message", None)), BaseMessage
            )
        )
        self.events.append(
            (
                "model.completed",
                {"run_id": str(run_id), "message_ids": message_ids},
            )
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        self.events.append(("model.failed", (str(run_id), type(error).__name__)))

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del input_str, parent_run_id, metadata
        self.events.append(
            (
                "tool.started",
                {
                    "run_id": str(run_id),
                    "name": serialized["name"],
                    "tool_call_id": kwargs.get("tool_call_id"),
                    "inputs": inputs,
                },
            )
        )

    async def on_tool_end(
        self,
        output: object,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del output, parent_run_id, kwargs
        self.events.append(("tool.completed", str(run_id)))


@pytest.mark.asyncio
async def test_callbacks_see_the_final_request_and_real_tool_execution() -> None:
    """Locked callbacks expose post-middleware input before provider execution."""

    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_values",
                        "args": {"a": 1, "b": 2},
                        "id": "call-add",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_deep_agent(
        model=model,
        tools=[add_values],
        system_prompt="original-system",
        middleware=[_RewriteRequest()],
    )
    recorder = _Recorder()

    native_assistant_ids: set[str] = set()
    async for part in graph.astream(
        {"messages": [HumanMessage(content="original-user", id="original-user-id")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        if part.get("type") != "messages":
            continue
        message, _metadata = part["data"]
        if isinstance(message, AIMessage) and message.id is not None:
            native_assistant_ids.add(message.id)

    names = [name for name, _value in recorder.events]
    assert names == [
        "model.started",
        "model.completed",
        "tool.started",
        "tool.completed",
        "model.started",
        "model.completed",
    ]
    first_request = recorder.events[0][1]
    assert isinstance(first_request, dict)
    assert first_request["messages"] == (
        ("system", "rewritten-system", None),
        ("human", "rewritten-user", "rewritten-user-id"),
    )
    completed = [
        value
        for name, value in recorder.events
        if name == "model.completed" and isinstance(value, dict)
    ]
    assert {
        message_id for value in completed for message_id in value["message_ids"]
    } == native_assistant_ids
    tool_start = recorder.events[2][1]
    assert isinstance(tool_start, dict)
    assert tool_start["name"] == "add_values"
    assert tool_start["tool_call_id"] == "call-add"
    assert tool_start["inputs"] == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_chain_callbacks_expose_graph_hooks_and_provider_parentage() -> None:
    """Locked callbacks expose graph hooks but not traceable wrapper functions."""

    graph = create_deep_agent(
        model=_ToolBindingModel(responses=[AIMessage(content="done")]),
        tools=[],
        middleware=[_AllAsyncHooks()],
    )
    recorder = _ChainRecorder()

    async for _part in graph.astream(
        {"messages": [HumanMessage(content="inspect every hook")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        pass

    names = [item["name"] for item in recorder.starts]
    assert names == [
        "LangGraph",
        "PatchToolCallsMiddleware.before_agent",
        "all-hooks.before_agent",
        "all-hooks.before_model",
        "model",
        "Model",
        "all-hooks.after_model",
        "all-hooks.after_agent",
    ]
    by_name = {str(item["name"]): item for item in recorder.starts}
    assert (
        by_name["all-hooks.before_agent"]["parent_run_id"]
        == by_name["LangGraph"]["run_id"]
    )
    assert by_name["model"]["parent_run_id"] == by_name["LangGraph"]["run_id"]
    assert by_name["Model"]["parent_run_id"] == by_name["model"]["run_id"]
    model_scope = by_name["model"]["checkpoint_namespace"]
    assert isinstance(model_scope, str)
    assert model_scope.startswith("model:")
    assert "all-hooks.awrap_model_call" not in names


@pytest.mark.asyncio
async def test_graph_callback_scope_contains_the_native_task_id() -> None:
    """Locked checkpoint metadata correlates a callback node to its Native task."""

    graph = create_deep_agent(
        model=_ToolBindingModel(responses=[AIMessage(content="done")]),
        tools=[],
    )
    recorder = _ChainRecorder()
    model_task_ids: set[str] = set()

    async for part in graph.astream(
        {"messages": [HumanMessage(content="correlate model task")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        if part.get("type") != "tasks":
            continue
        data = part.get("data")
        if isinstance(data, dict) and data.get("name") == "model":
            task_id = data.get("id")
            if isinstance(task_id, str):
                model_task_ids.add(task_id)

    model_start = next(item for item in recorder.starts if item["name"] == "model")
    checkpoint_namespace = model_start["checkpoint_namespace"]
    assert isinstance(checkpoint_namespace, str)
    assert checkpoint_namespace.removeprefix("model:") in model_task_ids


@pytest.mark.asyncio
async def test_model_start_precedes_failure_when_the_provider_has_no_output() -> None:
    """A failed zero-output call still exposes its final request before failure."""

    graph = create_deep_agent(
        model=_ToolBindingModel(responses=[]),
        tools=[],
        middleware=[_RewriteRequest()],
    )
    recorder = _Recorder()

    with pytest.raises(IndexError):
        async for _part in graph.astream(
            {"messages": [HumanMessage(content="original-user")]},
            {"callbacks": [recorder]},
            stream_mode=["messages", "tasks", "values"],
            subgraphs=True,
            version="v2",
        ):
            pass

    assert [name for name, _value in recorder.events] == [
        "model.started",
        "model.failed",
    ]


@pytest.mark.asyncio
async def test_each_provider_retry_has_an_independent_callback_identity() -> None:
    graph = create_deep_agent(
        model=_FailOnceModel(responses=[AIMessage(content="done")]),
        tools=[],
        middleware=[
            ModelRetryMiddleware(
                max_retries=1,
                initial_delay=0.001,
                backoff_factor=1,
                jitter=False,
            )
        ],
    )
    recorder = _Recorder()

    async for _part in graph.astream(
        {"messages": [HumanMessage(content="retry once")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        pass

    assert [name for name, _value in recorder.events] == [
        "model.started",
        "model.failed",
        "model.started",
        "model.completed",
    ]
    first = recorder.events[0][1]
    second = recorder.events[2][1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["run_id"] != second["run_id"]


@pytest.mark.asyncio
async def test_subagent_callback_namespace_matches_the_native_namespace() -> None:
    """Locked checkpoint metadata retains the validated child graph scope."""

    root_model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "delegate",
                            "subagent_type": "researcher",
                        },
                        "id": "call-task",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="root done"),
        ]
    )
    child_model = _ToolBindingModel(responses=[AIMessage(content="child done")])
    graph = create_deep_agent(
        model=root_model,
        tools=[],
        subagents=[
            {
                "name": "researcher",
                "description": "Complete delegated work",
                "system_prompt": "Complete the child request",
                "model": child_model,
                "tools": [],
            }
        ],
    )
    recorder = _Recorder()
    native_namespaces: set[tuple[str, ...]] = set()

    async for part in graph.astream(
        {"messages": [HumanMessage(content="delegate work")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        namespace = part.get("ns")
        if isinstance(namespace, tuple):
            native_namespaces.add(namespace)

    callback_namespaces: set[tuple[str, ...]] = set()
    child_agents: set[str] = set()
    for name, value in recorder.events:
        if name != "model.started" or not isinstance(value, dict):
            continue
        metadata = value["metadata"]
        assert isinstance(metadata, dict)
        checkpoint_namespace = metadata.get("langgraph_checkpoint_ns")
        assert isinstance(checkpoint_namespace, str)
        segments = tuple(checkpoint_namespace.split("|"))
        callback_namespaces.add(segments[:-1])
        agent_name = metadata.get("lc_agent_name")
        if isinstance(agent_name, str):
            child_agents.add(agent_name)

    child_namespaces = {item for item in native_namespaces if item}
    assert child_namespaces
    assert child_namespaces <= callback_namespaces
    assert child_agents == {"researcher"}


@pytest.mark.asyncio
async def test_parallel_tools_have_independent_callback_lifecycles() -> None:
    """Parallel Tool callbacks retain distinct execution and proposal identities."""

    entered: list[str] = []
    both_entered = asyncio.Event()

    @tool
    async def wait_for_peer(value: str) -> str:
        """Wait until both parallel calls start.

        Args:
            value: Per-call value.

        Returns:
            The unchanged value.
        """

        entered.append(value)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        return value

    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_for_peer",
                        "args": {"value": "first"},
                        "id": "call-first",
                        "type": "tool_call",
                    },
                    {
                        "name": "wait_for_peer",
                        "args": {"value": "second"},
                        "id": "call-second",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_deep_agent(model=model, tools=[wait_for_peer])
    recorder = _Recorder()

    async for _part in graph.astream(
        {"messages": [HumanMessage(content="run both")]},
        {"callbacks": [recorder]},
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
        version="v2",
    ):
        pass

    starts = [
        value
        for name, value in recorder.events
        if name == "tool.started" and isinstance(value, dict)
    ]
    completions = [value for name, value in recorder.events if name == "tool.completed"]
    assert {item["tool_call_id"] for item in starts} == {
        "call-first",
        "call-second",
    }
    assert len({item["run_id"] for item in starts}) == 2
    assert set(completions) == {item["run_id"] for item in starts}
    assert set(entered) == {"first", "second"}


@pytest.mark.asyncio
async def test_runtime_must_close_a_tool_callback_left_open_by_cancellation() -> None:
    """Locked Tool callbacks do not emit an error when cancellation stops execution."""

    tool_entered = asyncio.Event()
    release_tool = asyncio.Event()

    @tool
    async def wait_until_cancelled() -> str:
        """Wait for cancellation.

        Returns:
            An unreachable value.
        """

        tool_entered.set()
        await release_tool.wait()
        return "unreachable"

    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_until_cancelled",
                        "args": {},
                        "id": "call-cancelled",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    graph = create_deep_agent(model=model, tools=[wait_until_cancelled])
    recorder = _Recorder()

    async def consume() -> None:
        async for _part in graph.astream(
            {"messages": [HumanMessage(content="wait")]},
            {"callbacks": [recorder]},
            stream_mode=["messages", "tasks", "values"],
            subgraphs=True,
            version="v2",
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(tool_entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tool_events = [name for name, _value in recorder.events if name.startswith("tool.")]
    assert tool_events == ["tool.started"]
