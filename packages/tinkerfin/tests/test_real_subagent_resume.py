"""Locked Deep Agents subagent Tool HITL resume identity contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from ag_ui.core import RawEvent, ToolCallResultEvent, ToolCallStartEvent
from ag_ui.core.types import ResumeEntry
from deepagents import create_deep_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Interrupt

from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    ResumeMapper,
    SubagentProvenance,
)
from tinkerfin_contracts import RunIdentity


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _ToolBindingFakeModel:
        del tools, kwargs
        return self


@tool
def approved_child_tool(value: str) -> str:
    """Return a value after the child Tool review."""

    return value


async def _capture(
    graph: object,
    graph_input: object,
    config: Mapping[str, object],
) -> tuple[list[Mapping[str, object]], list[str]]:
    parts: list[Mapping[str, object]] = []
    representations: list[str] = []
    stream = cast(Any, graph).astream(
        graph_input,
        config=config,
        stream_mode=["messages", "tasks", "values"],
        version="v2",
        subgraphs=True,
    )
    async for part in stream:
        representations.append(repr(part))
        parts.append(cast(Mapping[str, object], part))
    return parts, representations


def _task_parts(
    parts: Sequence[Mapping[str, object]],
    *,
    namespace: tuple[str, ...],
    name: str,
    phase: str,
) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], part["data"])
        for part in parts
        if part["type"] == "tasks"
        and part["ns"] == namespace
        and cast(Mapping[str, object], part["data"]).get("name") == name
        and (
            ("input" in cast(Mapping[str, object], part["data"])) == (phase == "start")
        )
    ]


def _message_parts(
    parts: Sequence[Mapping[str, object]],
) -> list[tuple[tuple[str, ...], BaseMessage]]:
    messages: list[tuple[tuple[str, ...], BaseMessage]] = []
    for part in parts:
        if part["type"] != "messages":
            continue
        message, _metadata = cast(tuple[BaseMessage, object], part["data"])
        messages.append((cast(tuple[str, ...], part["ns"]), message))
    return messages


def _root_interrupt(parts: Sequence[Mapping[str, object]]) -> Interrupt:
    candidates = [
        cast(tuple[Interrupt, ...], part.get("interrupts", ()))
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]
    assert candidates and len(candidates[-1]) == 1
    return candidates[-1][0]


async def test_real_subagent_tool_resume_preserves_native_identity() -> None:
    """The locked runtime must preserve the logical task across child HITL resume."""

    root_model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Complete the approved child work",
                            "subagent_type": "researcher",
                        },
                        "id": "parent-task-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="root done"),
        ]
    )
    child_model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_child_tool",
                        "args": {"value": "ok"},
                        "id": "child-approved-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="child done"),
        ]
    )
    graph = create_deep_agent(
        model=root_model,
        tools=[],
        subagents=[
            {
                "name": "researcher",
                "description": "Complete approved child work",
                "system_prompt": "Use the approved child Tool.",
                "model": child_model,
                "tools": [approved_child_tool],
                "interrupt_on": {
                    "approved_child_tool": {"allowed_decisions": ["approve"]}
                },
            }
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "real-subagent-resume"}}

    before, before_repr = await _capture(
        graph,
        {"messages": [HumanMessage(content="Delegate the work")]},
        config,
    )
    assert len(before_repr) == len(before)
    parent_starts = _task_parts(
        before,
        namespace=(),
        name="tools",
        phase="start",
    )
    assert len(parent_starts) == 1
    graph_task_id = cast(str, parent_starts[0]["id"])
    child_namespace = (f"tools:{graph_task_id}",)
    child_interrupt = _root_interrupt(before)
    assert any(
        part["type"] == "values"
        and part["ns"] == child_namespace
        and tuple(cast(Sequence[object], part.get("interrupts", ())))
        == (child_interrupt,)
        for part in before
    )
    assert any(
        namespace == child_namespace
        and isinstance(message, AIMessage)
        and [call.get("id") for call in message.tool_calls] == ["child-approved-call"]
        for namespace, message in _message_parts(before)
    )

    after, after_repr = await _capture(
        graph,
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
    )
    assert len(after_repr) == len(after)
    resumed_parent_starts = _task_parts(
        after,
        namespace=(),
        name="tools",
        phase="start",
    )
    assert [part["id"] for part in resumed_parent_starts] == [graph_task_id]
    assert any(
        namespace == child_namespace
        and isinstance(message, ToolMessage)
        and message.tool_call_id == "child-approved-call"
        for namespace, message in _message_parts(after)
    )
    assert any(
        namespace == ()
        and isinstance(message, ToolMessage)
        and message.tool_call_id == "parent-task-call"
        for namespace, message in _message_parts(after)
    )
    parent_results = _task_parts(
        after,
        namespace=(),
        name="tools",
        phase="result",
    )
    assert [part["id"] for part in parent_results] == [graph_task_id]
    assert parent_results[0]["error"] is None
    assert parent_results[0]["interrupts"] == []

    before_adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(
            namespace="test", thread_id="real-subagent-resume", run_id="request-before"
        )
    )
    before_events = [event for part in before for event in before_adapter.process(part)]
    before_events.extend(before_adapter.finish())
    before_outcome = before_adapter.main_outcome()
    assert before_outcome.type == "interrupt"
    descriptors = [
        descriptor
        for event in before_events
        if isinstance(event, RawEvent)
        and event.source == "langgraph.tasks"
        and isinstance(event.event.get("provenance"), dict)
        for descriptor in cast(
            Sequence[object],
            cast(Mapping[str, object], event.event["provenance"]).get("subagents", []),
        )
    ]
    assert len(descriptors) == 1
    before_provenance = SubagentProvenance.model_validate(descriptors[0])
    assert before_provenance.graph_namespace == child_namespace
    assert before_provenance.graph_task_id == graph_task_id
    assert before_provenance.request_run_id == "request-before"
    public_interrupt = before_outcome.interrupts[0]
    translation = ResumeMapper().map_agui(
        entries=(
            ResumeEntry.model_validate(
                {
                    "interruptId": public_interrupt.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ),
        ),
        interrupts=before_outcome.interrupts,
    )

    after_adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(
            namespace="test", thread_id="real-subagent-resume", run_id="request-after"
        ),
        prior_tool_call_ids=frozenset(translation.prior_tool_call_ids),
    )
    after_events = [event for part in after for event in after_adapter.process(part)]
    after_events.extend(after_adapter.finish())
    assert after_adapter.main_outcome().type == "success"
    resumed_descriptors = [
        descriptor
        for event in after_events
        if isinstance(event, RawEvent)
        and event.source == "langgraph.tasks"
        and isinstance(event.event.get("provenance"), dict)
        for descriptor in cast(
            Sequence[object],
            cast(Mapping[str, object], event.event["provenance"]).get("subagents", []),
        )
    ]
    assert len(resumed_descriptors) == 1
    after_provenance = SubagentProvenance.model_validate(resumed_descriptors[0])
    assert (
        after_provenance.subagent_invocation_id
        == before_provenance.subagent_invocation_id
    )
    assert after_provenance.request_run_id == "request-after"
    child_results = [
        event
        for event in after_events
        if isinstance(event, ToolCallResultEvent)
        and event.tool_call_id == public_interrupt.tool_call_id
    ]
    assert len(child_results) == 1
    child_raw_event = child_results[0].raw_event
    assert isinstance(child_raw_event, Mapping)
    child_source = child_raw_event.get("source")
    assert isinstance(child_source, Mapping)
    assert child_source["subagentInvocationId"] == (
        before_provenance.subagent_invocation_id
    )
    assert not any(
        isinstance(event, ToolCallStartEvent)
        and event.tool_call_id == public_interrupt.tool_call_id
        for event in after_events
    )
    parent_results_events = [
        event
        for event in after_events
        if isinstance(event, ToolCallResultEvent)
        and isinstance(event.raw_event, Mapping)
        and event.raw_event.get("relatedSubagentInvocationId")
        == before_provenance.subagent_invocation_id
    ]
    assert len(parent_results_events) == 1
    parent_raw_event = parent_results_events[0].raw_event
    assert isinstance(parent_raw_event, Mapping)
    assert parent_raw_event["runId"] == "request-after"
