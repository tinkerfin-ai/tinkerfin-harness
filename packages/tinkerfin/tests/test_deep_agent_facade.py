"""Public behavior of the deferred Deep Agents runtime façade."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, cast

import pytest
from ag_ui.core import RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from tinkerfin import (
    AgentRuntime,
    RunIdentity,
    TinkerFin,
)
from tinkerfin.deep_agent import create_graph


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _RecordingGraph:
    def __init__(
        self,
        *,
        parts: tuple[object, ...] = (),
        source_error: Exception | None = None,
    ) -> None:
        self.parts = parts
        self.source_error = source_error
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.opened = 0
        self.pulled = 0
        self.closed = 0

    def astream(
        self,
        *args: object,
        **options: object,
    ) -> AsyncIterator[object]:
        self.calls.append((args, options))

        async def source() -> AsyncIterator[object]:
            self.opened += 1
            try:
                for part in self.parts:
                    self.pulled += 1
                    yield part
                if self.source_error is not None:
                    raise self.source_error
            finally:
                self.closed += 1

        return source()


setattr(
    _RecordingGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


def _graph_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "thread-1"}}


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _real_definition() -> AgentRuntime[None]:
    return (
        TinkerFin()
        .with_namespace("test")
        .build(model=_FakeModel(responses=[AIMessage(content="ok")]), tools=[])
    )


@pytest.mark.asyncio
async def test_direct_graph_ainvoke_runs_a_real_deep_agent() -> None:
    graph = await create_graph(_real_definition())
    result = await graph.ainvoke(
        InputAgentState(messages=[HumanMessage(content="hello")]),
        _graph_config(),
    )
    messages = cast(Sequence[BaseMessage], result["messages"])

    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_open_agui_run_emits_one_lifecycle_for_preparation_failure(
    definition_factory,
) -> None:
    runtime = definition_factory(RuntimeError("model setup failed"))
    stream = runtime.open_agui_run(
        thread_id="thread-1", run_id="run-1", input=_graph_input()
    )
    events = [event async for event in stream]
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, RuntimeError)


@pytest.mark.asyncio
async def test_agui_facade_streams_a_real_deep_agent_graph() -> None:
    identity = _identity()
    runtime = _real_definition()
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="hello")]),
        )
    ]

    assert events[0].type.value == "RUN_STARTED"
    assert events[-1].type.value == "RUN_FINISHED"
    assert [event.type.value for event in events].count("RUN_STARTED") == 1
    assert [event.type.value for event in events].count("RUN_FINISHED") == 1
    assert any(event.type.value == "TEXT_MESSAGE_CONTENT" for event in events)
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].input is None
