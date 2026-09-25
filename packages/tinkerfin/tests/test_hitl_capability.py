"""Tool cancellation follows the actual interrupted Graph, not inherited names."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Literal, cast

import pytest
from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import RunErrorEvent, RunFinishedEvent, RunFinishedInterruptOutcome
from deepagents import DeepAgentState, create_deep_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from tinkerfin import (
    AgUiResumeReceipt,
    AgUiResumeRequest,
    AgUiResumeResponse,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin.deep_agent import create_graph
from tinkerfin_agui_adapter import AttachmentMessagesSnapshotEvent
from tinkerfin_contracts import RunTerminalObservation
from tinkerfin_tracing import CapturePolicy, InMemoryTraceStore, RunFact, Tracer


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _ExternalState(DeepAgentState, total=False):
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]
    _tinkerfin_tool_review: dict[str, object] | None


class _ForgedState(DeepAgentState, total=False):
    _tinkerfin_tool_review: dict[str, object]


class _ContinuationState(DeepAgentState, total=False):
    note: str


@pytest.mark.parametrize("update", [False, True])
async def test_native_continuation_keeps_dynamic_review_pending(update: bool) -> None:
    executed: list[str] = []

    @tool
    async def save_report() -> str:
        """Save a report after approval."""
        executed.append("saved")
        return "saved"

    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "save_report", "id": "save", "args": {}}],
                    ),
                    AIMessage(content="Done"),
                ]
            ),
            tools=[save_report],
            interrupt_on={"save_report": True},
            state_schema=_ContinuationState,
        )
    )
    initial = [
        event
        async for event in runtime.open_agui_run(
            thread_id="pending",
            run_id="initial",
            messages=[{"id": "user", "role": "user", "content": "Save report"}],
        )
    ]
    assert isinstance(initial[-1], RunFinishedEvent)
    assert isinstance(initial[-1].outcome, RunFinishedInterruptOutcome)
    before = await tracer.get(runtime.thread_identity("pending"))
    stream = runtime.open_run(
        thread_id="pending",
        run_id="continued",
        input=Command(update={"note": "updated"}) if update else None,
    )
    async for _part in stream:
        pass
    assert stream.error is None
    waiting = await tracer.get(runtime.thread_identity("pending"))
    assert waiting.status.execution == "waiting"
    assert len(waiting.graph.turns) == 1
    assert executed == []
    assert [(item.id, item.status) for item in waiting.interactions] == [
        (item.id, item.status) for item in before.interactions
    ]
    approved = [
        event
        async for event in runtime.open_agui_run(
            thread_id="pending",
            run_id="approved",
            resume=_request(initial[-1].outcome, "approve"),
        )
    ]
    assert isinstance(approved[-1], RunFinishedEvent)
    assert executed == ["saved"]


@pytest.mark.parametrize("command", ["none", "update", "goto"])
async def test_native_continuation_retains_input_without_creating_user_turn(
    command: Literal["none", "update", "goto"],
) -> None:
    tracer = Tracer()
    model = _Model(
        responses=[
            AIMessage(content="First"),
            AIMessage(content="Second"),
            AIMessage(content="Third"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .build(
            model=model,
            state_schema=_ContinuationState,
        )
    )
    async for _part in runtime.open_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(content="Hello", id="user")]},
    ):
        pass
    graph_input: Command[object] | None = (
        None
        if command == "none"
        else Command(
            update={"note": "updated"}, goto="model" if command == "goto" else ()
        )
    )
    async for _part in runtime.open_run(
        thread_id="thread", run_id="continued", input=graph_input
    ):
        pass
    continued = await tracer.get(runtime.thread_identity("thread"))
    assert len(continued.graph.turns) == 1
    assert model.i == (2 if command == "goto" else 1)
    facts = [event.fact for event in (await continued.events(limit=100)).items]
    source = next(
        fact
        for fact in facts
        if isinstance(fact, RunFact)
        and fact.identity.run_id == "continued"
        and fact.phase == "resumed"
    )
    assert source.input_kind == "continuation"
    assert source.interrupt_ids == ()
    assert source.input is not None
    if command == "none":
        assert source.input.value is None
    else:
        assert isinstance(source.input.value, dict)
        command_value = source.input.value["value"]
        assert isinstance(command_value, dict)
        assert command_value["update"] == {"note": "updated"}
        if command == "goto":
            assert command_value["goto"] == "model"
        assert continued.state.root["note"] == "updated"
    async for _part in runtime.open_run(
        thread_id="thread",
        run_id="new-request",
        input={"messages": [HumanMessage(content="Another request", id="next-user")]},
    ):
        pass
    assert len((await tracer.get(runtime.thread_identity("thread"))).graph.turns) == 2


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


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_resumed_tool_result_survives_later_delivery_failure(
    delivery_fails: bool,
) -> None:
    from ag_ui.core import ToolCallResultEvent, ToolCallStartEvent

    saved: list[str] = []

    @tool
    async def save_report() -> str:
        """Save the approved report."""
        saved.append("report")
        return "Report saved"

    @tool
    async def deliver_report() -> str:
        """Deliver the saved report."""
        if delivery_fails:
            raise ValueError("Unsupported delivery format")
        return "Report delivered"

    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "save_report", "args": {}, "id": "save-call"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "deliver_report", "args": {}, "id": "deliver-call"}
                ],
            ),
            AIMessage(content="Done"),
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("reports")
        .with_observer(tracer)
        .build(
            model=model,
            tools=[save_report, deliver_report],
            interrupt_on={"save_report": True},
        )
    )
    before = [
        event
        async for event in runtime.open_agui_run(
            thread_id="thread",
            run_id="before",
            messages=[{"id": "request", "role": "user", "content": "Prepare a report"}],
        )
    ]
    terminal = before[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    assert saved == []
    proposal = next(event for event in before if isinstance(event, ToolCallStartEvent))
    resumed = runtime.open_agui_run(
        thread_id="thread",
        run_id="after",
        resume=_request(terminal.outcome, "approve"),
    )
    after = [event async for event in resumed]
    assert saved == ["report"]
    result = next(event for event in after if isinstance(event, ToolCallResultEvent))
    assert result.tool_call_id == proposal.tool_call_id
    assert result.content == "Report saved"
    assert (
        len(
            [
                event
                for event in after
                if isinstance(event, (RunErrorEvent, RunFinishedEvent))
            ]
        )
        == 1
    )
    assert isinstance(after[-1], RunErrorEvent if delivery_fails else RunFinishedEvent)
    thread = await tracer.get(runtime.thread_identity("thread"))
    assert thread.status.execution == ("failed" if delivery_fails else "succeeded")
    graph = await tracer.query(runtime.thread_identity("thread"), limit=100)
    tools = {node.name: node for node in graph.nodes if node.kind == "tool"}
    assert set(tools) == {"save_report", "deliver_report"}
    assert tools["save_report"].status == "succeeded"
    assert tools["save_report"].failure is None
    assert tools["deliver_report"].status == (
        "failed" if delivery_fails else "succeeded"
    )
    assert (tools["deliver_report"].failure is not None) is delivery_fails


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
async def test_failed_tool_retry_with_new_tracer_preserves_previous_prefix(
    nested: bool, outcome: Literal["succeeded", "failed", "cancelled"]
) -> None:
    store = InMemoryTraceStore()
    saver = InMemorySaver()
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0
    saved: list[str] = []

    @tool
    async def save_report() -> str:
        """Save the approved report."""
        saved.append("report")
        return "saved"

    @tool
    async def deliver_report() -> str:
        """Deliver a saved report once the destination is available."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("First destination unavailable")
        entered.set()
        await release.wait()
        if outcome == "failed":
            raise ValueError("Second destination unavailable")
        return "delivered"

    def build(tracer: Tracer, *, retry: bool):
        calls = [
            AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}])
            for name in ("save_report", "deliver_report")
        ]
        worker = _Model(
            responses=[AIMessage(content="Done")]
            if retry
            else [*calls, AIMessage(content="Done")]
        )
        factory = (
            TinkerFin(checkpointer=saver)
            .with_namespace("reports")
            .with_observer(tracer)
        )
        if nested:
            return factory.build(
                model=_Model(
                    responses=[
                        AIMessage(content="Done")
                        if retry
                        else AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "task",
                                    "id": "delegate",
                                    "args": {
                                        "subagent_type": "reporter",
                                        "description": "Prepare report",
                                    },
                                }
                            ],
                        ),
                        AIMessage(content="Done"),
                    ]
                ),
                subagents=[
                    {
                        "name": "reporter",
                        "description": "Prepare reports",
                        "system_prompt": "Prepare and deliver the requested report.",
                        "model": worker,
                        "tools": [save_report, deliver_report],
                    }
                ],
                interrupt_on={"save_report": True},
            )
        return factory.build(
            model=worker,
            tools=[save_report, deliver_report],
            interrupt_on={"save_report": True},
        )

    policy = CapturePolicy.public_history(include_error_messages=True)
    original_tracer = Tracer(store=store, capture_policy=policy)
    original = build(original_tracer, retry=False)
    before = [
        event
        async for event in original.open_agui_run(
            thread_id="thread",
            run_id="before",
            messages=[{"id": "request", "role": "user", "content": "Prepare a report"}],
        )
    ]
    assert isinstance(before[-1], RunFinishedEvent)
    assert isinstance(before[-1].outcome, RunFinishedInterruptOutcome)
    failed = [
        event
        async for event in original.open_agui_run(
            thread_id="thread",
            run_id="failed",
            resume=_request(before[-1].outcome, "approve"),
        )
    ]
    assert isinstance(failed[-1], RunErrorEvent)
    assert saved == ["report"]
    old_view = await original_tracer.get(
        original.thread_identity("thread"), head_run_id="failed"
    )
    assert old_view.status.execution == "failed"
    old_events = await old_view.events(limit=100)

    tracer = Tracer(store=store, capture_policy=policy)
    runtime = build(tracer, retry=True)
    retried = runtime.open_run(thread_id="thread", run_id="retry", input=None)

    async def consume() -> None:
        async for _part in retried:
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        running = await tracer.query(runtime.thread_identity("thread"), limit=100)
        delivery = next(node for node in running.nodes if node.name == "deliver_report")
        assert delivery.status == "running"
        assert delivery.failure is None
        assert delivery.result is None
        assert delivery.completed_at is None
        if outcome == "cancelled":
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        else:
            release.set()
            if outcome == "failed":
                with pytest.raises(ValueError, match="Second destination"):
                    await consumer
            else:
                await consumer
                assert retried.error is None
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    assert attempts == 2
    assert saved == ["report"]
    current = await tracer.query(runtime.thread_identity("thread"), limit=100)
    tools = {node.name: node for node in current.nodes if node.kind == "tool"}
    assert tools["save_report"].status == "succeeded"
    assert tools["save_report"].failure is None
    assert tools["deliver_report"].status == outcome
    assert (tools["deliver_report"].failure is not None) is (outcome == "failed")
    if tools["deliver_report"].failure is not None:
        assert (
            tools["deliver_report"].failure.message == "Second destination unavailable"
        )
    old_delivery = next(
        node for node in old_view.graph.nodes if node.name == "deliver_report"
    )
    assert old_delivery.status == "failed"
    assert old_delivery.failure is not None
    assert old_delivery.failure.message == "First destination unavailable"
    assert await old_view.events(limit=100) == old_events


@pytest.mark.parametrize("nested_kind", ["named", "unnamed", "inherited", "completed"])
@pytest.mark.parametrize("decision", ["mixed", "approve", "abandon"])
async def test_nested_graph_cancellation_checks_only_its_actual_owner(
    nested_kind: str, decision: Literal["mixed", "approve", "abandon"]
) -> None:
    executed: list[str] = []
    received: list[object] = []

    @tool
    async def first_action() -> str:
        """Record the first approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record the second approved action."""
        executed.append("second")
        return "second"

    external = create_deep_agent(
        model=_Model(
            responses=(
                [AIMessage(content="nested done")]
                if nested_kind == "completed"
                else [_actions("nested"), AIMessage(content="nested done")]
            )
        ),
        tools=[first_action, second_action],
        interrupt_on={"first_action": True, "second_action": True},
        state_schema=_ExternalState,
        name="foreign" if nested_kind == "named" else None,
    )
    if nested_kind == "inherited":

        def request_tools(state: _ExternalState) -> dict[str, object]:
            del state
            return {"messages": [_actions("nested")]}

        def review(state: _ExternalState) -> dict[str, object]:
            del state
            decisions: object = interrupt(
                {
                    "action_requests": [
                        {"name": name, "args": {}}
                        for name in ("first_action", "second_action")
                    ],
                    "review_configs": [
                        {"action_name": name, "allowed_decisions": ["approve"]}
                        for name in ("first_action", "second_action")
                    ],
                }
            )
            received.append(decisions)
            return {
                "messages": [
                    ToolMessage(
                        content="approved", tool_call_id=f"nested-{name}", name=name
                    )
                    for name in ("first_action", "second_action")
                ]
                + [AIMessage(content="nested done")]
            }

        builder = StateGraph(_ExternalState)
        builder.add_node("request_tools", request_tools)
        builder.add_node("review", review)
        builder.add_edge(START, "request_tools")
        builder.add_edge("request_tools", "review")
        builder.add_edge("review", END)
        external = builder.compile()

    @tool
    async def call_nested(runtime: ToolRuntime) -> str:
        """Call a compiled Graph with the worker's inherited state and metadata."""
        nested_input = dict(runtime.state)
        nested_input["messages"] = [HumanMessage(content="Run nested actions")]
        result = await external.ainvoke(cast(Any, nested_input))
        return str(result["messages"][-1].content)

    worker_responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "call_nested", "args": {}, "id": "nested", "type": "tool_call"}
            ],
        )
    ]
    if nested_kind == "completed":
        worker_responses.append(_actions("worker"))
    worker_responses.append(AIMessage(content="worker done"))
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
                                "args": {
                                    "subagent_type": "worker",
                                    "description": "Work",
                                },
                                "id": "worker",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Run assigned actions",
                    "system_prompt": "Complete assigned work",
                    "model": _Model(responses=worker_responses),
                    "tools": [call_nested, first_action, second_action],
                }
            ],
            interrupt_on={"first_action": True, "second_action": True},
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        messages=[{"id": "user", "role": "user", "content": "Go"}],
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    receipts: list[AgUiResumeReceipt] = []

    async def saved(receipt: AgUiResumeReceipt) -> None:
        assert not executed and not received
        receipts.append(receipt)

    request = _request(terminal.outcome, decision)
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="resume", resume=request, on_resume_saved=saved
    )
    result = [event async for event in resumed]
    if decision == "mixed" and nested_kind != "completed":
        assert isinstance(resumed.error, TinkerFinLifecycleError)
        assert "tool review support" in str(resumed.error)
        assert isinstance(result[-1], RunErrorEvent)
        assert not executed and not received
        assert receipts == []
    elif decision == "abandon":
        assert isinstance(result[-1], RunErrorEvent)
        assert result[-1].code == "resume_cancelled"
        assert not executed and not received
        assert receipts == []
    else:
        assert resumed.error is None
        assert isinstance(result[-1], RunFinishedEvent)
        assert result[-1].outcome is not None
        assert result[-1].outcome.type == "success"
        assert len(receipts) == 1
        assert receipts[0].identity == runtime.run_identity("thread", "resume")
        assert receipts[0].parent_run_id == "initial"
        assert receipts[0].responses == tuple(
            AgUiResumeResponse(entry.interrupt_id, entry.status)
            for entry in sorted(request.entries, key=lambda entry: entry.interrupt_id)
        )
        if nested_kind == "inherited":
            assert received == [
                {"decisions": [{"type": "approve"}, {"type": "approve"}]}
            ]
        else:
            assert sorted(executed) == (
                ["first"] if decision == "mixed" else ["first", "second"]
            )


async def test_repeated_reviews_refresh_support_for_each_resume_run() -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record an approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record an action only if it was approved."""
        executed.append("second")
        return "second"

    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("business")
        .build(
            model=_Model(
                responses=[
                    _actions("first"),
                    _actions("next"),
                    AIMessage(content="done"),
                ]
            ),
            tools=[first_action, second_action],
            interrupt_on={"first_action": True, "second_action": True},
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        messages=[{"id": "user", "role": "user", "content": "Go"}],
    )
    for run_id in ("first-resume", "second-resume"):
        events = [event async for event in stream]
        assert stream.error is None
        terminal = events[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            resume=_request(terminal.outcome, "mixed"),
        )
    events = [event async for event in stream]
    assert stream.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None
    assert terminal.outcome.type == "success"
    assert executed == ["first", "first"]


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


@pytest.mark.parametrize("api", ["native", "agui", "graph"])
@pytest.mark.parametrize("with_review", [False, True])
async def test_stateless_calls_do_not_wait_for_nonexistent_checkpoints(
    api: str, with_review: bool
) -> None:
    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True} if with_review else {},
        )
    )
    graph_input = {"messages": [HumanMessage(content="Go")]}
    if api == "graph":
        result = await (await create_graph(runtime)).ainvoke(cast(Any, graph_input))
        assert "messages" in result
    else:
        stream = (
            runtime.open_run(
                thread_id="thread", run_id="run", input=cast(Any, graph_input)
            )
            if api == "native"
            else runtime.open_agui_run(
                thread_id="thread", run_id="run", input=cast(Any, graph_input)
            )
        )
        assert [item async for item in stream]
        assert stream.error is None


@pytest.mark.parametrize("api", ["native", "agui"])
@pytest.mark.parametrize("durability", ["async", "exit"])
async def test_tool_review_rejects_uncommitted_step_persistence(
    api: str, durability: Literal["async", "exit"]
) -> None:
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("business")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True},
        )
    )
    graph_input = {"messages": [HumanMessage(content="Go")]}
    if api == "native":
        stream = runtime.open_run(
            thread_id="thread",
            run_id="run",
            input=cast(Any, graph_input),
            durability=durability,
        )
        with pytest.raises(ValueError, match="requires durability='sync'"):
            [item async for item in stream]
    else:
        events = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            input=cast(Any, graph_input),
            durability=durability,
        )
        [event async for event in events]
        assert isinstance(events.error, ValueError)
        assert "requires durability='sync'" in str(events.error)


@pytest.mark.parametrize("api", ["native", "agui"])
async def test_default_persistence_finishes_before_model_execution(api: str) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    persisted = asyncio.Event()

    class DelayedSaver(InMemorySaver):
        async def aput(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> RunnableConfig:
            entered.set()
            await release.wait()
            result = await super().aput(config, checkpoint, metadata, new_versions)
            persisted.set()
            return result

    class CheckedModel(_Model):
        def _generate(self, *args: Any, **kwargs: Any):
            assert persisted.is_set(), (
                "model ran before the source checkpoint was saved"
            )
            return super()._generate(*args, **kwargs)

    runtime = (
        TinkerFin(checkpointer=DelayedSaver())
        .with_namespace("business")
        .build(
            model=CheckedModel(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True},
        )
    )
    stream = (
        runtime.open_run(thread_id="thread", run_id="run", input={"messages": []})
        if api == "native"
        else runtime.open_agui_run(
            thread_id="thread", run_id="run", input={"messages": []}
        )
    )

    async def consume() -> None:
        [item async for item in stream]

    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        release.set()
        await consumer
        assert stream.error is None
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


class _SaveControl(BaseException):
    pass


@pytest.mark.parametrize("operation", ["review", "resume"])
@pytest.mark.parametrize("failure", ["success", "ordinary", "control"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_review_and_resume_writes_keep_concurrent_failure_evidence(
    operation: str, failure: str, cancel: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    selected_channel = (
        "_tinkerfin_tool_review" if operation == "review" else "_tinkerfin_resume"
    )

    class InterruptedSaver(InMemorySaver):
        async def aput_writes(
            self,
            config: RunnableConfig,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
        ) -> None:
            if any(channel == selected_channel for channel, _value in writes):
                entered.set()
                try:
                    await release.wait()
                    if failure == "ordinary":
                        raise ValueError("saver failure evidence")
                    if failure == "control":
                        raise _SaveControl("saver control evidence")
                finally:
                    finished.set()
            await super().aput_writes(config, writes, task_id, task_path)

    @tool
    async def first_action() -> str:
        """Return an approved action result."""
        return "first"

    @tool
    async def second_action() -> str:
        """Return an approved action result."""
        return "second"

    runtime = (
        TinkerFin(checkpointer=InterruptedSaver())
        .with_namespace("business")
        .build(
            model=_Model(responses=[_actions("review"), AIMessage(content="done")]),
            tools=[first_action, second_action],
            interrupt_on={"first_action": True, "second_action": True},
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(content="Go")]},
    )
    if operation == "resume":
        initial = [event async for event in stream]
        assert stream.error is None
        terminal = initial[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="resume",
            resume=_request(terminal.outcome, "mixed"),
        )

    async def consume() -> BaseException | None:
        try:
            [event async for event in stream]
            return stream.error
        except BaseException as error:  # noqa: BLE001 - assert process-control propagation
            return error

    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        if cancel:
            consumer.cancel()
        release.set()
        error = await consumer
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    assert finished.is_set()
    if failure == "control":
        assert isinstance(error, _SaveControl)
    elif cancel:
        assert isinstance(error, asyncio.CancelledError)
        if failure == "ordinary":
            assert any("saver failure evidence" in note for note in error.__notes__)
    elif failure == "ordinary":
        assert isinstance(error, TinkerFinLifecycleError)
        assert isinstance(error.cause, ValueError)
    else:
        assert error is None
    if error is not None:
        assert error.__cause__ is not error


@pytest.mark.parametrize("operation", ["build", "execute"])
async def test_handled_independent_graph_failure_preserves_parent_success(
    operation: str,
) -> None:
    handled: list[str] = []

    class FailedSaver(InMemorySaver):
        async def aput_writes(
            self,
            config: RunnableConfig,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
        ) -> None:
            if any(channel == "_tinkerfin_tool_review" for channel, _value in writes):
                raise ValueError("independent review unavailable")
            await super().aput_writes(config, writes, task_id, task_path)

    @tool
    async def first_action() -> str:
        """Run only after a reviewer approves this action."""
        raise AssertionError("no approval was supplied")

    def undocumented_tool() -> str:
        return "invalid tool configuration"

    child = (
        TinkerFin(checkpointer=FailedSaver())
        .with_namespace("child")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "first_action",
                                "args": {},
                                "id": "child",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            ),
            tools=[undocumented_tool] if operation == "build" else [first_action],
            interrupt_on={"first_action": True},
        )
    )

    @tool
    async def inspect_child() -> str:
        """Report an independent operation's failure without failing the parent."""
        try:
            graph = await create_graph(child)
            await graph.ainvoke(
                {"messages": [HumanMessage(content="Go")]},
                config={"configurable": {"thread_id": "child"}},
            )
        except (ValueError, TinkerFinLifecycleError) as error:
            handled.append(str(error))
            return "The child operation is unavailable; no child action ran."
        raise AssertionError("child operation must fail")

    parent = (
        TinkerFin()
        .with_namespace("parent")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "inspect_child",
                                "args": {},
                                "id": "parent",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="The child operation is unavailable."),
                ]
            ),
            tools=[inspect_child],
        )
    )
    stream = parent.open_agui_run(
        thread_id="parent",
        run_id="parent",
        input={"messages": [HumanMessage(content="Go")]},
    )
    events = [event async for event in stream]
    assert len(handled) == 1
    assert stream.error is None
    assert not any(isinstance(event, RunErrorEvent) for event in events)
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].outcome is not None
    assert events[-1].outcome.type == "success"


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
