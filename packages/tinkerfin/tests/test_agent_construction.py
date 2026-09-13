"""Public construction, role inheritance, and explicit resource configuration."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from tinkerfin import TinkerFin
from tinkerfin.subagents import SubAgent


def contents(result: Mapping[str, object]) -> list[object]:
    messages = result["messages"]
    assert isinstance(messages, list)
    assert all(isinstance(message, BaseMessage) for message in messages)
    return [message.content for message in cast(list[BaseMessage], messages)]


def invalid_call(operation: object, **kwargs: object) -> object:
    assert callable(operation)
    return operation(**kwargs)


class Model(FakeMessagesListChatModel):
    bound_tools: list[frozenset[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[BaseTool | dict[str, Any] | type | Callable[..., Any]],
        **kwargs: Any,
    ) -> Runnable:
        del kwargs
        self.bound_tools.append(
            frozenset(t.name for t in tools if isinstance(t, BaseTool))
        )
        return self


def delegate(name: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"subagent_type": name, "description": "Answer the task"},
                "id": "delegate-call",
            }
        ],
    )


@tool
async def reference() -> str:
    """Read the shared reference."""
    return "reference"


@tool
async def restricted() -> str:
    """Read the role-specific reference."""
    return "restricted"


async def test_build_does_not_call_the_upstream_agent_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("The upstream assembly function must not be invoked")

    monkeypatch.setattr("deepagents.graph.create_deep_agent", forbidden)
    model = Model(responses=[AIMessage(content="ready")])
    runtime = TinkerFin().with_namespace("example").build(model=model)
    assert model.i == 0 and not model.bound_tools
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert contents(result)[-1] == "ready"


async def test_automatic_general_purpose_agent_inherits_static_tools() -> None:
    model = Model(
        responses=[
            delegate("general-purpose"),
            AIMessage(content="child"),
            AIMessage(content="parent"),
        ]
    )
    runtime = (
        TinkerFin().with_namespace("example").build(model=model, tools=[reference])
    )
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Delegate"}]},
    )
    assert contents(result)[-1] == "parent"
    assert any(
        "reference" in names and "task" not in names for names in model.bound_tools
    )
    assert any("reference" in names and "task" in names for names in model.bound_tools)


@pytest.mark.parametrize("override", [False, True])
async def test_declared_agent_tool_override_is_explicit(override: bool) -> None:
    child_model = Model(responses=[AIMessage(content="child")])
    child: SubAgent = {
        "name": "reader",
        "description": "Read references",
        "system_prompt": "Read",
        "model": child_model,
    }
    if override:
        child["tools"] = [restricted]
    parent = Model(responses=[delegate("reader"), AIMessage(content="done")])
    runtime = (
        TinkerFin()
        .with_namespace("example")
        .build(model=parent, tools=[reference], subagents=[child])
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Delegate"}]},
    )
    assert child_model.bound_tools
    for names in child_model.bound_tools:
        assert ("restricted" in names) is override
        assert ("reference" in names) is not override


async def test_shared_checkpointer_retains_conversation_history() -> None:
    model = Model(responses=[AIMessage(content="first"), AIMessage(content="second")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("example")
        .build(model=model)
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="first",
        input={"messages": [{"role": "user", "content": "One"}]},
    )
    result = await runtime.ainvoke(
        thread_id="thread",
        run_id="second",
        input={"messages": [{"role": "user", "content": "Two"}]},
    )
    assert contents(result) == ["One", "first", "Two", "second"]


@pytest.mark.parametrize(
    "removed", ["prepare_tools", "checkpointer", "store", "cache", "debug"]
)
def test_build_rejects_removed_configuration(removed: str) -> None:
    builder = TinkerFin().with_namespace("example")
    with pytest.raises(TypeError, match=removed):
        invalid_call(
            builder.build,
            model=Model(responses=[AIMessage(content="unused")]),
            **{removed: None},
        )


def test_build_requires_an_explicit_model() -> None:
    with pytest.raises(TypeError, match="model"):
        invalid_call(TinkerFin().with_namespace("example").build)


def test_declarations_do_not_expose_upstream_context_modes() -> None:
    with pytest.raises(TypeError, match="mode"):
        invalid_call(
            TinkerFin().with_namespace("example").build,
            model=Model(responses=[AIMessage(content="unused")]),
            subagents=[
                {
                    "name": "reader",
                    "description": "Read",
                    "system_prompt": "Read",
                    "mode": "fork",
                }
            ],
        )
