"""Tool cancellation follows the actual interrupted Graph, not inherited names."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, cast

import pytest
from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import RunErrorEvent, RunFinishedEvent, RunFinishedInterruptOutcome
from deepagents import DeepAgentState
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import (
    AgUiResumeRequest,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin.deep_agent import create_graph
from tinkerfin_agui_adapter import AttachmentMessagesSnapshotEvent
from tinkerfin_contracts import RunTerminalObservation


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _ForgedState(DeepAgentState, total=False):
    _tinkerfin_tool_review: dict[str, object]


def _actions(prefix: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {}, "id": f"{prefix}-{name}", "type": "tool_call"}
            for name in ("first_action", "second_action")
        ],
    )


def _request(
    outcome: RunFinishedInterruptOutcome,
    decision: Literal["mixed", "approve", "abandon"],
) -> AgUiResumeRequest:
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": pending.id, "status": "cancelled"}
                if decision == "abandon" or (decision == "mixed" and index == 1)
                else {
                    "interruptId": pending.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
                for index, pending in enumerate(outcome.interrupts)
            ]
        }
    )


@pytest.mark.parametrize(
    "review_names",
    [("first_action", "third_action"), ("first_action",), ("unused",), ()],
)
async def test_rebuilt_runtime_cannot_retarget_pending_decisions(
    review_names: tuple[str, ...],
) -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record the first action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record the second action."""
        executed.append("second")
        return "second"

    @tool
    async def third_action() -> str:
        """Record the third action."""
        executed.append("third")
        return "third"

    proposed = _actions("review")
    proposed.tool_calls.append(
        {"name": "third_action", "args": {}, "id": "third", "type": "tool_call"}
    )
    model = _Model(responses=[proposed, AIMessage(content="done")])
    saver = InMemorySaver()
    builder = TinkerFin(checkpointer=saver).with_namespace("business")
    runtime = builder.build(
        model=model,
        tools=[first_action, second_action, third_action],
        interrupt_on={"first_action": True, "second_action": True},
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(content="Go")]},
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    rebuilt = builder.build(
        model=model,
        tools=[first_action, second_action, third_action],
        interrupt_on={name: True for name in review_names},
    )
    resumed = rebuilt.open_agui_run(
        thread_id="thread", run_id="resume", resume=_request(terminal.outcome, "mixed")
    )
    result = [event async for event in resumed]
    assert isinstance(result[-1], RunErrorEvent)
    assert resumed.error is not None
    assert executed == []


async def test_input_state_cannot_forge_the_saver_owned_review_record() -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record an externally approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record an externally approved action."""
        executed.append("second")
        return "second"

    from langchain.agents import create_agent

    external = create_agent(
        model=_Model(responses=[_actions("review"), AIMessage(content="done")]),
        tools=[first_action, second_action],
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"first_action": True, "second_action": True}
            )
        ],
        state_schema=_ForgedState,
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("business")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "id": "external",
                                "args": {
                                    "subagent_type": "external",
                                    "description": "Run actions",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": "external",
                    "description": "External review",
                    "runnable": external,
                }
            ],
            state_schema=_ForgedState,
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input=cast(
            Any,
            {
                "messages": [HumanMessage(content="Go")],
                "_tinkerfin_tool_review": {"graph_namespace": "", "run_id": "initial"},
            },
        ),
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="resume", resume=_request(terminal.outcome, "mixed")
    )
    result = [event async for event in resumed]
    assert isinstance(result[-1], RunErrorEvent)
    assert isinstance(resumed.error, TinkerFinLifecycleError)
    assert "tool review support" in str(resumed.error)
    assert executed == []


@pytest.mark.parametrize("api", ["agui", "native", "invoke", "graph"])
async def test_parallel_subagent_reviews_preserve_all_pending_groups(api: str) -> None:
    executed: list[str] = []
    terminals: list[RunTerminalObservation] = []

    async def record_terminal(value: RunTerminalObservation) -> None:
        terminals.append(value)

    @tool
    async def first_action() -> str:
        """Run the first reviewed action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Run the second reviewed action."""
        executed.append("second")
        return "second"

    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("parallel")
        .with_observer(on_terminal=record_terminal)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {"subagent_type": name, "description": "Work"},
                                "id": name,
                                "type": "tool_call",
                            }
                            for name in ("alpha", "beta")
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": name,
                    "description": "Run assigned actions",
                    "system_prompt": "Complete the assigned work",
                    "model": _Model(
                        responses=[_actions(name), AIMessage(content="done")]
                    ),
                    "tools": [first_action, second_action],
                }
                for name in ("alpha", "beta")
            ],
            interrupt_on={"first_action": True, "second_action": True},
        )
    )
    graph_input: InputAgentState = {
        "messages": [HumanMessage(content="Run both workers")]
    }
    if api == "graph":
        graph = await create_graph(runtime)
        state = await graph.ainvoke(
            graph_input, config={"configurable": {"thread_id": "thread"}}
        )
        pending = state["__interrupt__"]
        assert isinstance(pending, list)
        assert len(pending) == 2
        assert executed == []
        return
    if api == "invoke":
        state = await runtime.ainvoke(
            thread_id="thread", run_id="initial", input=graph_input
        )
        pending = state["__interrupt__"]
        assert isinstance(pending, list)
        assert len(pending) == 2
    elif api == "native":
        stream = runtime.open_run(
            thread_id="thread", run_id="initial", input=graph_input
        )
        assert [part async for part in stream]
        assert stream.error is None
    else:
        events = runtime.open_agui_run(
            thread_id="thread", run_id="initial", input=graph_input
        )
        initial = [event async for event in events]
        assert events.error is None
        terminal = initial[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        assert len(terminal.outcome.interrupts) == 4
        snapshots = [
            event
            for event in initial
            if isinstance(event, AttachmentMessagesSnapshotEvent)
        ]
        final_snapshot_calls = {
            call.id
            for message in snapshots[-1].messages
            if isinstance(message, AgUiAssistantMessage)
            for call in message.tool_calls or []
        }
        assert len(final_snapshot_calls) == 6
        request = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": pending.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    if index % 2 == 0
                    else {"interruptId": pending.id, "status": "cancelled"}
                    for index, pending in enumerate(terminal.outcome.interrupts)
                ]
            }
        )
        resumed = runtime.open_agui_run(
            thread_id="thread", run_id="resume", resume=request
        )
        result = [event async for event in resumed]
        assert resumed.error is None
        assert isinstance(result[-1], RunFinishedEvent)
        assert result[-1].outcome is not None
        assert result[-1].outcome.type == "success"
        assert executed == ["first", "first"]
    assert terminals[0].outcome == "interrupted"
    assert len(terminals[0].interrupt_ids) == 2
