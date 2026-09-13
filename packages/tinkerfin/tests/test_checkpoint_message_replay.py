"""Checkpoint history repair must not append prior assistant content again."""

import pytest
from ag_ui.core import TextMessageContentEvent, ToolCallResultEvent, ToolCallStartEvent
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_plan_mode import _FakeModel

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiHistory
from tinkerfin_tracing import Tracer


@pytest.mark.parametrize("record_history", [False, True])
async def test_new_input_after_tool_failure_keeps_old_text_once(record_history: bool):
    @tool
    async def deliver() -> str:
        """Deliver a prepared report."""
        raise ValueError("Delivery failed")

    model = _FakeModel(
        responses=[
            AIMessage(
                id="prior-reply",
                content="The report is prepared.",
                tool_calls=[{"name": "deliver", "args": {}, "id": "delivery"}],
            ),
            AIMessage(id="new-reply", content="I will revise the delivery."),
        ]
    )
    tracer = Tracer()
    builder = TinkerFin(checkpointer=InMemorySaver()).with_namespace("report")
    if record_history:
        builder = builder.with_observer(tracer)
    runtime = builder.build(model=model, tools=[deliver])
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[{"id": "request-1", "role": "user", "content": "Prepare report"}],
    )
    before = [event async for event in first]
    second = runtime.open_agui_run(
        thread_id="thread",
        run_id="second",
        messages=[{"id": "request-2", "role": "user", "content": "Revise delivery"}],
    )
    after = [event async for event in second]
    assert isinstance(first.error, ValueError)
    assert second.error is None
    assert [
        event.delta for event in before if isinstance(event, TextMessageContentEvent)
    ] == ["The report is prepared."]
    assert [
        event.delta for event in after if isinstance(event, TextMessageContentEvent)
    ] == ["I will revise the delivery."]
    proposal = next(event for event in before if isinstance(event, ToolCallStartEvent))
    assert not any(isinstance(event, ToolCallStartEvent) for event in after)
    results = [event for event in after if isinstance(event, ToolCallResultEvent)]
    assert len(results) == 1 and results[0].tool_call_id == proposal.tool_call_id
    assert sum(event.type == "RUN_FINISHED" for event in after) == 1
    assert not any(event.type == "RUN_ERROR" for event in after)
    assert len(model.model_inputs) == 2
    if record_history:
        snapshot = (
            await AgUiHistory(tracer, namespace="report").get("thread")
        ).snapshot
        assert [
            message.content
            for message in snapshot.messages
            if message.role == "assistant"
        ] == [
            "The report is prepared.",
            "I will revise the delivery.",
        ]


@pytest.mark.parametrize("record_history", [False, True])
async def test_failed_tool_can_be_requested_with_a_new_tool_id(
    record_history: bool,
) -> None:
    attempts: list[str] = []

    @tool
    async def save(report: str) -> str:
        """Save the selected report."""
        attempts.append(report)
        if len(attempts) == 1:
            raise ValueError("Storage temporarily unavailable")
        return f"Saved {report}"

    responses: list[BaseMessage] = [
        AIMessage(
            id=f"proposal-{index}",
            content=f"Saving report {index}",
            tool_calls=[
                {
                    "id": f"save-call-{index}",
                    "name": "save",
                    "args": {"report": str(index)},
                }
            ],
        )
        for index in (1, 2)
    ]
    responses.append(AIMessage(id="done", content="Report saved"))
    model = _FakeModel(responses=responses)
    tracer = Tracer()
    builder = TinkerFin(checkpointer=InMemorySaver()).with_namespace("report")
    if record_history:
        builder = builder.with_observer(tracer)
    runtime = builder.build(model=model, tools=[save])
    first = runtime.open_agui_run(
        thread_id="thread",
        run_id="first",
        messages=[{"id": "request-1", "role": "user", "content": "Save report 1"}],
    )
    _ = [event async for event in first]
    second = runtime.open_agui_run(
        thread_id="thread",
        run_id="second",
        messages=[{"id": "request-2", "role": "user", "content": "Save report 2"}],
    )
    after = [event async for event in second]
    assert isinstance(first.error, ValueError)
    assert second.error is None
    assert attempts == ["1", "2"]
    assert [
        event.delta for event in after if isinstance(event, TextMessageContentEvent)
    ] == ["Saving report 2", "Report saved"]
    assert sum(isinstance(event, ToolCallStartEvent) for event in after) == 1
    results = [event for event in after if isinstance(event, ToolCallResultEvent)]
    assert len(results) == 2
    assert results[-1].content == "Saved 2"
    assert sum(event.type == "RUN_FINISHED" for event in after) == 1
    assert not any(event.type == "RUN_ERROR" for event in after)
    if record_history:
        snapshot = (await runtime.agui.history(tracer).get("thread")).snapshot
        tools = [node for node in snapshot.graph.nodes if node.kind == "tool"]
        assert {node.source_id for node in tools} == {"save-call-1", "save-call-2"}
        current = next(node for node in tools if node.source_id == "save-call-2")
        assert current.turn_id == snapshot.graph.turns[-1].id
        previous = next(node for node in tools if node.source_id == "save-call-1")
        assert previous.turn_id == snapshot.graph.turns[0].id
        assert current.id != previous.id
