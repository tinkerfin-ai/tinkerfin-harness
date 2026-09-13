"""Parent results preserve successful child task and approval-resume outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from test_native_control_messages import recovery_store as recovery_store
from test_pending_scope_settlement import _Model

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiTraceHistory
from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeTaskResultPayload,
    NativeTasksStreamPart,
    NativeValuesStreamPart,
    validate_native_stream_part,
)
from tinkerfin_tracing import SubagentFact, TraceEvent, Tracer, TraceStore


def _proposal(name: str, call_id: str, **arguments: str) -> AIMessage:
    return AIMessage(
        id=f"proposal-{call_id}",
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": name,
                "args": arguments,
            }
        ],
    )


@dataclass(frozen=True, slots=True)
class DelegationCapture:
    """Retain a complete native delegation and its persisted history."""

    history: AgUiTraceHistory
    facts: tuple[TraceEvent, ...]
    parts: tuple[Mapping[str, object], ...]


async def capture_successful_delegation(
    depth: int,
    review: bool,
    *,
    store: TraceStore | None = None,
) -> DelegationCapture:
    executed: list[str] = []

    @tool
    async def report() -> str:
        """Return the completed business calculation."""
        executed.append("report")
        return "Verified calculation: 42"

    worker_model = _Model(
        responses=[
            _proposal("report", "calculation"),
            AIMessage(id="worker-answer", content="Verified calculation: 42"),
        ]
    )
    tracer = Tracer(store=store)
    builder = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("success")
        .with_observer(tracer)
    )
    if depth == 1:
        runtime = builder.build(
            model=_Model(
                responses=[
                    _proposal(
                        "task",
                        "root-call",
                        subagent_type="worker",
                        description="Calculate",
                    ),
                    AIMessage(id="root-answer", content="Complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Calculate the result",
                    "system_prompt": "Complete the calculation.",
                    "model": worker_model,
                    "tools": [report],
                    "interrupt_on": {"report": True} if review else {},
                }
            ],
        )
    else:
        compiled = create_deep_agent(
            model=_Model(
                responses=[
                    _proposal(
                        "task",
                        "middle-call",
                        subagent_type="worker",
                        description="Calculate",
                    ),
                    AIMessage(id="middle-answer", content="Verified calculation: 42"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Calculate the result",
                    "system_prompt": "Complete the calculation.",
                    "model": worker_model,
                    "tools": [report],
                    "interrupt_on": {"report": True} if review else {},
                }
            ],
        )
        runtime = builder.build(
            model=_Model(
                responses=[
                    _proposal(
                        "task",
                        "root-call",
                        subagent_type="coordinator",
                        description="Coordinate the calculation",
                    ),
                    AIMessage(id="root-answer", content="Complete"),
                ]
            ),
            subagents=[
                {
                    "name": "coordinator",
                    "description": "Coordinate calculations",
                    "runnable": compiled,
                }
            ],
        )
    parts: list[Mapping[str, object]] = []

    async def record(part: Mapping[str, object]) -> None:
        parts.append(part)

    first = runtime.open_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(id="user", content="Calculate")]},
        on_native_part=record,
    )
    _ = [part async for part in first]
    assert first.error is None
    if review:
        assert executed == []
        waiting = (await runtime.agui.history(tracer).get("thread")).snapshot
        assert waiting.summary.status.execution == "waiting"
        interrupts = {
            item.id
            for part in parts
            if isinstance(
                parsed := validate_native_stream_part(part), NativeValuesStreamPart
            )
            for item in parsed.interrupts
        }
        assert len(interrupts) == 1
        resumed = runtime.open_run(
            thread_id="thread",
            run_id="resumed",
            input=Command(
                resume={next(iter(interrupts)): {"decisions": [{"type": "approve"}]}}
            ),
            on_native_part=record,
        )
        _ = [part async for part in resumed]
        assert resumed.error is None
    assert executed == ["report"]
    view = await runtime.agui.history(tracer).get("thread")
    facts = (await view.trace.events(limit=1000)).items
    return DelegationCapture(view.snapshot, facts, tuple(parts))


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("review", [False, True])
async def test_successful_child_result_waits_for_its_authoritative_task_completion(
    depth: int,
    review: bool,
    recovery_store: TraceStore,
) -> None:
    captured = await capture_successful_delegation(depth, review, store=recovery_store)
    assert captured.history.summary.status.execution == "succeeded"
    assert captured.history.summary.pending_interactions == ()
    children = [
        node for node in captured.history.graph.nodes if node.kind == "subagent"
    ]
    assert len(children) == depth
    assert all(node.status == "succeeded" for node in children)
    starts = [
        event.fact
        for event in captured.facts
        if isinstance(event.fact, SubagentFact) and event.fact.phase == "started"
    ]
    completed = [
        event.fact
        for event in captured.facts
        if isinstance(event.fact, SubagentFact) and event.fact.phase == "completed"
    ]
    assert len(completed) == depth and all(
        fact.status == "succeeded" for fact in completed
    )
    for start in starts:
        parent = start.graph_namespace[:-1]
        task_id = start.graph_namespace[-1].partition(":")[2]
        result_positions: list[int] = []
        task_positions: list[int] = []
        for index, part in enumerate(captured.parts):
            parsed = validate_native_stream_part(part)
            if parsed.ns != parent:
                continue
            if (
                isinstance(parsed, NativeMessageStreamPart)
                and isinstance(parsed.data.message, ToolMessage)
                and parsed.data.message.tool_call_id == start.parent_tool_call_id
            ):
                result_positions.append(index)
            if (
                isinstance(parsed, NativeTasksStreamPart)
                and isinstance(parsed.data, NativeTaskResultPayload)
                and parsed.data.id == task_id
                and not parsed.data.interrupts
                and parsed.data.error is None
            ):
                task_positions.append(index)
        assert result_positions and task_positions
        assert result_positions[-1] < task_positions[-1]
