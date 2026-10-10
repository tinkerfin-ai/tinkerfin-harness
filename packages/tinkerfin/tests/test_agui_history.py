"""Native history references match real Runtime AG-UI delivery and recovery."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin.agui import (
    AgUiHistory,
    AgUiSubagentReference,
    AgUiToolMessageReference,
    AgUiToolReference,
    AgUiTraceGraphPage,
    AgUiTraceUpdate,
)
from tinkerfin_agui_adapter import (
    AttachmentMessagesSnapshotEvent,
    parse_tool_review_interrupt,
)
from tinkerfin_tracing import (
    InvalidTraceReference,
    Tracer,
    TraceThreadNotFound,
)


class _ToolModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


@tool
async def save_report() -> str:
    """Save the reviewed report."""
    return "Report saved"


async def test_model_request_reference_reads_exact_input_and_rejects_foreign_scope() -> (
    None
):
    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("history")
        .with_observer(tracer)
        .build(model=_ToolModel(responses=[AIMessage(content="Complete")]), tools=[])
    )
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id="request-details",
            run_id="run",
            messages=[{"id": "user", "role": "user", "content": "Inspect this input"}],
        )
    ]
    assert isinstance(events[-1], RunFinishedEvent)
    reader = runtime.agui.history(tracer)
    view = await reader.get("request-details")
    model = next(node for node in view.snapshot.graph.nodes if node.kind == "model")
    assert model.request is None
    assert not model.request_omitted
    assert model.request_reference is not None
    request = await reader.model_request(
        "request-details", reference=model.request_reference
    )
    assert request.node_id == model.id
    assert not request.request_omitted
    assert isinstance(request.request, dict)
    inputs = request.request["messages"]
    assert isinstance(inputs, list)
    assert any(
        isinstance(item, dict) and item.get("content") == "Inspect this input"
        for item in inputs
    )
    page = await reader.query("request-details")
    queried_model = next(node for node in page.snapshot.nodes if node.kind == "model")
    assert queried_model.request_reference == model.request_reference
    with pytest.raises(InvalidTraceReference):
        await reader.model_request("another-thread", reference=model.request_reference)
    with pytest.raises(InvalidTraceReference):
        await AgUiHistory(tracer, namespace="another-owner").model_request(
            "request-details", reference=model.request_reference
        )
    with pytest.raises(InvalidTraceReference):
        await reader.model_request("request-details", reference="invalid-reference")
    await view.trace.delete()
    with pytest.raises(TraceThreadNotFound):
        await reader.model_request("request-details", reference=model.request_reference)


@tool
async def fail_delivery() -> str:
    """Report an unavailable delivery destination."""
    raise ValueError("Delivery destination unavailable")


def _call(name: str, call_id: str, **args: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"id": call_id, "name": name, "args": args}]
    )


def _resume(terminal: RunFinishedEvent, decision: str) -> AgUiResumeRequest:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": item.id, "status": "cancelled"}
                if decision == "cancelled"
                else {
                    "interruptId": item.id,
                    "status": "resolved",
                    "payload": {"type": decision},
                }
                for item in terminal.outcome.interrupts
            ]
        }
    )


@pytest.mark.parametrize("decision", ["approve", "reject", "cancelled"])
@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_history_and_updates_keep_live_tool_identity_after_resume(
    decision: str,
    delivery_fails: bool,
) -> None:
    responses: list[BaseMessage] = [_call("save_report", "save-call")]
    if delivery_fails:
        responses.append(_call("fail_delivery", "delivery-call"))
    responses.append(AIMessage(content="Complete"))
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("history")
        .with_observer(tracer)
        .build(
            model=_ToolModel(responses=responses),
            tools=[save_report, fail_delivery],
            interrupt_on={"save_report": True},
        )
    )
    first = [
        event
        async for event in runtime.open_agui_run(
            thread_id="report",
            run_id="request",
            messages=[{"id": "user", "role": "user", "content": "Prepare report"}],
        )
    ]
    terminal = first[-1]
    assert isinstance(terminal, RunFinishedEvent)
    reader = runtime.agui.history(tracer)
    before_view = await reader.get("report")
    before = before_view.trace
    native_graph = before.graph.model_dump()
    exported = before_view.snapshot
    node = next(node for node in exported.graph.nodes if node.kind == "tool")
    tool_reference = node.agui
    assert isinstance(tool_reference, AgUiToolReference)
    proposed = next(event for event in first if isinstance(event, ToolCallStartEvent))
    assert tool_reference.tool_call_id == proposed.tool_call_id
    assert node.id == next(
        node.id for node in before.graph.nodes if node.kind == "tool"
    )
    assert before.graph.model_dump() == native_graph
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    actions = exported.interactions[0].agui
    assert actions is not None
    assert [action.id for action in actions] == [
        action.id for action in terminal.outcome.interrupts
    ]
    assert parse_tool_review_interrupt(actions[0]) == parse_tool_review_interrupt(
        terminal.outcome.interrupts[0]
    )
    assert actions[0].tool_call_id == tool_reference.tool_call_id
    assert exported.summary.pending_interactions[0].agui == actions
    snapshot = next(
        event for event in first if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    exported_message_ids = {
        message.agui.message_id
        for message in exported.messages
        if message.agui is not None
    }
    assert {message.id for message in snapshot.messages} <= exported_message_ids

    follow = before_view.follow()
    native_follow = before.follow()
    try:
        resumed_stream = runtime.open_agui_run(
            thread_id="report",
            run_id="resume",
            resume=_resume(terminal, decision),
        )
        resumed = [event async for event in resumed_stream]
        projected_update = await anext(follow)
        update = await anext(native_follow)
    finally:
        await follow.aclose()
        await native_follow.aclose()
    assert (
        AgUiTraceUpdate.model_validate_json(
            projected_update.model_dump_json(round_trip=True)
        )
        == projected_update
    )
    assert projected_update.as_of_seq == update.as_of_seq
    assert projected_update.generation == update.generation
    assert projected_update.graph.node_removes == update.graph.node_removes
    assert projected_update.facts == update.facts
    after_view = await reader.get("report")
    after = after_view.trace
    final = after_view.snapshot
    baseline = await tracer.get(before.key, head_run_id="resume", at_run_start=True)
    from tinkerfin.agui import AgUiHistoryView

    replay_baseline = AgUiHistoryView(baseline).snapshot
    assert replay_baseline.messages == exported.messages
    assert replay_baseline.interactions == exported.interactions
    final_node = next(
        item for item in final.graph.nodes if item.source_id == "save-call"
    )
    assert final_node.id == node.id and final_node.agui == node.agui
    if decision == "approve":
        result = next(
            event for event in resumed if isinstance(event, ToolCallResultEvent)
        )
        assert result.tool_call_id == tool_reference.tool_call_id
        result_message = next(
            item for item in final.messages if item.tool_call_id == "save-call"
        )
        assert isinstance(result_message.agui, AgUiToolMessageReference)
        assert result_message.agui.tool_call_id == result.tool_call_id
    if delivery_fails and decision != "cancelled":
        assert after.status.execution == "failed"
    graph_view = await reader.query("report", limit=200)
    query = graph_view.trace
    page = graph_view.snapshot
    assert AgUiTraceGraphPage.model_validate_json(page.model_dump_json()) == page
    assert page.next_cursor == query.snapshot.next_cursor
    assert page.ordered_node_ids == query.snapshot.ordered_node_ids
    assert {n.id: n.agui for n in page.nodes} == {
        n.id: n.agui for n in final.graph.nodes
    }


@pytest.mark.parametrize("depth", [1, 2])
async def test_nested_subagent_review_keeps_parent_and_child_live_references(
    depth: int,
) -> None:
    delegate = create_deep_agent(
        model=_ToolModel(
            responses=[_call("save_report", "shared-call"), AIMessage(content="Saved")]
        ),
        tools=[save_report],
        interrupt_on={"save_report": True},
    )
    for level in range(depth - 1):
        delegate = create_deep_agent(
            model=_ToolModel(
                responses=[
                    _call(
                        "task",
                        f"task-{level + 1}",
                        description="Prepare report",
                        subagent_type="worker",
                    ),
                    AIMessage(content="Delegate complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Prepare reports",
                    "runnable": delegate,
                }
            ],
        )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("history")
        .with_observer(tracer)
        .build(
            model=_ToolModel(
                responses=[
                    _call(
                        "task",
                        "task-0",
                        description="Prepare report",
                        subagent_type="worker",
                    ),
                    AIMessage(content="Complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Prepare reports",
                    "runnable": delegate,
                }
            ],
        )
    )
    first = [
        event
        async for event in runtime.open_agui_run(
            thread_id="delegated",
            run_id="before",
            messages=[{"id": "user", "role": "user", "content": "Delegate the report"}],
        )
    ]
    terminal = first[-1]
    assert isinstance(terminal, RunFinishedEvent)
    view = await AgUiHistory(tracer, namespace="history").get("delegated")
    exported = view.snapshot
    children = [node for node in exported.graph.nodes if node.kind == "subagent"]
    assert len(children) == depth
    for child in children:
        assert isinstance(child.agui, AgUiSubagentReference)
        contexts = [
            event.model_dump(mode="json", by_alias=True).get("rawEvent")
            for event in first
        ]
        assert any(
            isinstance(context, dict)
            and isinstance(context.get("source"), dict)
            and context["source"].get("subagentInvocationId")
            == child.agui.subagent_invocation_id
            and context["source"].get("parentToolCallId")
            == child.agui.parent_tool_call_id
            for context in contexts
        )
    actions = exported.interactions[0].agui
    assert actions is not None
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    assert actions[0].id == terminal.outcome.interrupts[0].id
    assert actions[0].tool_call_id == terminal.outcome.interrupts[0].tool_call_id
    after = [
        event
        async for event in runtime.open_agui_run(
            thread_id="delegated",
            run_id="after",
            resume=_resume(terminal, "approve"),
        )
    ]
    assert any(
        isinstance(event, ToolCallResultEvent)
        and event.tool_call_id == actions[0].tool_call_id
        for event in after
    )
    refreshed = (
        await AgUiHistory(tracer, namespace="history").get("delegated")
    ).snapshot
    assert {n.id: n.agui for n in refreshed.graph.nodes if n.kind == "subagent"} == {
        n.id: n.agui for n in children
    }


@pytest.mark.parametrize(
    ("kind", "decision"),
    [
        ("draft", "approve"),
        ("draft", "reject"),
        ("draft", "dismiss"),
        ("draft", "cancelled"),
        ("clarify", "respond"),
        ("clarify", "dismiss"),
        ("clarify", "cancelled"),
    ],
)
async def test_plan_human_input_exports_the_same_live_request_and_response_schema(
    kind: str,
    decision: str,
) -> None:
    outcome = (
        {
            "type": "draft",
            "draft": {
                "goal": "Prepare report",
                "assumptions": [],
                "steps": [
                    {
                        "id": "prepare",
                        "title": "Prepare",
                        "description": "Review source data",
                        "verification": ["Check totals"],
                    }
                ],
                "acceptance_criteria": ["Report reconciles"],
            },
        }
        if kind == "draft"
        else {
            "type": "clarify",
            "clarification": {
                "questions": [
                    {
                        "id": "audience",
                        "answer_type": "text",
                        "prompt": "Who will read this report?",
                        "required": True,
                    },
                ]
            },
        }
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("history")
        .with_observer(tracer)
        .with_plan()
        .build(
            model=_ToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "planner",
                                "name": "submit_plan"
                                if kind == "draft"
                                else "ask_user_question",
                                "args": {"content": outcome["draft"]}
                                if kind == "draft"
                                else {"form": outcome["clarification"]},
                            }
                        ],
                    ),
                    AIMessage(content="Report complete"),
                ]
            ),
        )
    )
    stream = runtime.open_agui_run(
        thread_id="plan",
        run_id="before",
        mode="plan",
        messages=[{"id": "user", "role": "user", "content": "Prepare report"}],
    )
    events = [event async for event in stream]
    assert stream.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    view = await AgUiHistory(tracer, namespace="history").get("plan")
    exported = view.snapshot
    actions = exported.interactions[0].agui
    assert actions is not None
    source = terminal.outcome.interrupts[0]
    assert (actions[0].id, actions[0].reason, actions[0].response_schema) == (
        source.id,
        source.reason,
        source.response_schema,
    )
    assert actions[0].metadata is not None and source.metadata is not None
    assert (
        actions[0].metadata["runtimeInterrupt"] == source.metadata["runtimeInterrupt"]
    )
    payload: dict[str, object] = {"type": decision}
    if kind == "draft":
        payload["baseRevision"] = 1
        if decision == "reject":
            payload["message"] = "Include the source totals"
    elif decision == "respond":
        payload["answers"] = {
            "audience": {
                "status": "answered",
                "answerType": "text",
                "answer": "Finance team",
            }
        }
    request = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": source.id, "status": "cancelled"}
                if decision == "cancelled"
                else {
                    "interruptId": source.id,
                    "status": "resolved",
                    "payload": payload,
                }
            ]
        }
    )
    async with view.follow() as updates:
        resumed = runtime.open_agui_run(
            thread_id="plan",
            run_id="after",
            resume=request,
        )
        resumed_events = [event async for event in resumed]
        assert resumed.error is None
        assert (
            len(
                [
                    event
                    for event in resumed_events
                    if isinstance(event, (RunFinishedEvent, RunErrorEvent))
                ]
            )
            == 1
        )
        if decision == "cancelled":
            assert isinstance(resumed_events[-1], RunErrorEvent)
            assert resumed_events[-1].code == "resume_cancelled"
        else:
            assert isinstance(resumed_events[-1], RunFinishedEvent)
        update = await anext(updates)
    final = (await runtime.agui.history(tracer).get("plan")).snapshot
    assert final.summary.pending_interactions == ()
    settled = next(
        item for item in final.interactions if item.id == exported.interactions[0].id
    )
    assert settled.status == ("cancelled" if decision == "cancelled" else "resolved")
    assert settled.agui == actions
    changed = next(
        item for item in update.interactions.upserts if item.id == settled.id
    )
    assert changed.status == settled.status
    assert changed.agui == actions
    if decision != "cancelled":
        repeated = [
            event
            async for event in runtime.open_agui_run(
                thread_id="plan", run_id="stale", resume=request
            )
        ]
        assert isinstance(repeated[-1], RunErrorEvent)
        assert repeated[-1].code == "runtime_initialization_error"


async def test_parallel_subagents_with_reused_native_tool_ids_stay_distinct() -> None:
    from deepagents import CompiledSubAgent

    children: list[CompiledSubAgent] = []
    for name in ("finance", "operations"):
        children.append(
            {
                "name": name,
                "description": "Prepare a reviewed report",
                "runnable": create_deep_agent(
                    model=_ToolModel(
                        responses=[
                            _call("save_report", "shared-call"),
                            AIMessage(content="Saved"),
                        ]
                    ),
                    tools=[save_report],
                    interrupt_on={"save_report": True},
                ),
            }
        )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("history")
        .with_observer(tracer)
        .build(
            model=_ToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": f"delegate-{name}",
                                "name": "task",
                                "args": {
                                    "description": "Prepare report",
                                    "subagent_type": name,
                                },
                            }
                            for name in ("finance", "operations")
                        ],
                    ),
                    AIMessage(content="Reports complete"),
                ]
            ),
            subagents=children,
        )
    )
    stream = runtime.open_agui_run(
        thread_id="parallel-delegates",
        run_id="before",
        messages=[
            {"id": "user", "role": "user", "content": "Prepare department reports"}
        ],
    )
    events = [event async for event in stream]
    assert stream.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    reader = AgUiHistory(tracer, namespace="history")
    exported = (await reader.get("parallel-delegates")).snapshot
    tool_ids = {
        node.agui.tool_call_id
        for node in exported.graph.nodes
        if isinstance(node.agui, AgUiToolReference) and node.source_id == "shared-call"
    }
    assert len(tool_ids) == 2
    assert tool_ids == {item.tool_call_id for item in terminal.outcome.interrupts}
    assert {
        item.tool_call_id
        for interaction in exported.interactions
        for item in interaction.agui or ()
    } == tool_ids
    resumed = [
        event
        async for event in runtime.open_agui_run(
            thread_id="parallel-delegates",
            run_id="after",
            resume=_resume(terminal, "approve"),
        )
    ]
    assert tool_ids <= {
        event.tool_call_id
        for event in resumed
        if isinstance(event, ToolCallResultEvent)
    }
    query = await reader.query("parallel-delegates", limit=1)
    first_page = query.snapshot
    assert first_page.next_cursor is not None
    older_query = await reader.query(
        "parallel-delegates", limit=1, cursor=first_page.next_cursor
    )
    older_page = older_query.snapshot
    assert older_page.as_of_seq == first_page.as_of_seq
    assert older_page.ordered_node_ids == older_query.trace.snapshot.ordered_node_ids


async def test_history_load_older_preserves_prefix_while_new_turns_arrive() -> None:
    from tinkerfin.agui import AgUiTraceHistory
    from tinkerfin_tracing import InvalidTraceCursor

    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("pages")
        .with_observer(tracer)
        .build(
            model=_ToolModel(
                responses=[
                    response
                    for index in range(4)
                    for response in (
                        _call("save_report", f"save-{index}"),
                        AIMessage(content="Saved", id=f"saved-{index}"),
                    )
                ]
            ),
            tools=[save_report],
        )
    )

    async def record(run_id: str) -> None:
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            messages=[{"id": f"user-{run_id}", "role": "user", "content": run_id}],
        )
        _ = [event async for event in stream]
        assert stream.error is None

    for run_id in ("one", "two", "three"):
        await record(run_id)
    reader = AgUiHistory(tracer, namespace="pages")
    view = await reader.get("thread", limit=1)
    before = view.snapshot
    assert len(before.graph.turns) == 1
    assert before.history_cursor is not None
    graph = await reader.query("thread", limit=1)
    first_page = graph.snapshot
    assert first_page.next_cursor is not None
    older_graph = await reader.query("thread", cursor=first_page.next_cursor, limit=1)
    assert older_graph.snapshot.as_of_seq == first_page.as_of_seq
    assert older_graph.trace.key == graph.trace.key
    await record("four")
    same = await view.load_older(limit=2)
    expanded = view.snapshot
    assert same is view
    assert len(expanded.graph.turns) == 3
    assert expanded.history_cursor is None
    assert expanded.as_of_seq == before.as_of_seq
    assert expanded.observed_at == before.observed_at
    assert expanded.generation == before.generation
    assert expanded.head_run_id == before.head_run_id
    assert expanded.state == before.state
    assert expanded.summary == before.summary
    assert all(message.content != "four" for message in expanded.messages)
    from_cursor = await reader.get(
        "thread", history_cursor=before.history_cursor, limit=2
    )
    assert from_cursor.snapshot == expanded
    with pytest.raises(InvalidTraceCursor):
        await reader.query("thread", cursor=first_page.next_cursor, limit=1)
    assert (await reader.get("thread")).snapshot.as_of_seq > expanded.as_of_seq
    assert AgUiTraceHistory.model_validate_json(expanded.model_dump_json()) == expanded


@pytest.mark.parametrize("graph_query", [False, True])
@pytest.mark.parametrize(
    "ending", ["external_close", "cancel", "timeout", "early_exit", "conversion_error"]
)
async def test_public_history_follow_settles_source_and_borrows_store(
    monkeypatch: pytest.MonkeyPatch,
    graph_query: bool,
    ending: str,
) -> None:
    import asyncio
    from collections.abc import AsyncGenerator

    from ag_ui.core import BaseEvent

    import tinkerfin._agui_history as history_module
    from tinkerfin_tracing import (
        InMemoryTraceStore,
        TraceFollowLifecycleError,
        TraceStoreUpdate,
        TraceThreadKey,
    )
    from tinkerfin_tracing.backend import StoredTraceEventPage, TraceEventPageRequest

    entered, released, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ObservedStore(InMemoryTraceStore):
        def follow(
            self, key: TraceThreadKey, *, after_seq: int
        ) -> AsyncGenerator[TraceStoreUpdate, None]:
            source = super().follow(key, after_seq=after_seq)

            async def observe() -> AsyncGenerator[TraceStoreUpdate, None]:
                try:
                    async for update in source:
                        yield update
                finally:
                    await source.aclose()
                    settled.set()

            return observe()

    store = ObservedStore()
    tracer = Tracer(store=store)
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("follow")
        .with_observer(tracer)
        .build(
            model=_ToolModel(
                responses=[
                    response
                    for index in range(4)
                    for response in (
                        _call("save_report", f"save-{index}"),
                        AIMessage(content="Saved", id=f"saved-{index}"),
                    )
                ]
            ),
            tools=[save_report],
        )
    )

    async def record(run_id: str) -> list[BaseEvent]:
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            messages=[{"id": f"user-{run_id}", "role": "user", "content": run_id}],
        )
        events = [event async for event in stream]
        assert stream.error is None
        return events

    await record("one")
    reader = AgUiHistory(tracer, namespace="follow")
    view = await reader.query("thread") if graph_query else await reader.get("thread")
    follower = view.follow()
    original_seq = view.snapshot.as_of_seq
    new_events: list[BaseEvent] = []
    if ending in ("early_exit", "conversion_error"):
        new_events = await record("two")
    read_page = store.backend.read_event_page

    async def gated_read(request: TraceEventPageRequest) -> StoredTraceEventPage:
        entered.set()
        await released.wait()
        return await read_page(request)

    monkeypatch.setattr(store.backend, "read_event_page", gated_read)
    pending = None
    try:
        if ending in ("early_exit", "conversion_error"):
            released.set()
            if ending == "conversion_error":
                failure = ValueError("Recorded reference cannot be converted")

                def fail_conversion(*args: object, **kwargs: object) -> None:
                    del args, kwargs
                    raise failure

                monkeypatch.setattr(
                    history_module,
                    "_project_graph_delta" if graph_query else "_project_update",
                    fail_conversion,
                )
                with pytest.raises(ValueError) as caught:
                    await anext(follower)
                assert caught.value is failure
            else:
                async with follower:
                    update = await anext(follower)
                    assert update.as_of_seq > original_seq
                    delta = (
                        update.graph if isinstance(update, AgUiTraceUpdate) else update
                    )
                    assert {
                        node.agui.tool_call_id
                        for node in delta.node_upserts
                        if isinstance(node.agui, AgUiToolReference)
                    } == {
                        event.tool_call_id
                        for event in new_events
                        if isinstance(event, ToolCallStartEvent)
                    }
        elif ending == "timeout":
            timeout_scope = asyncio.timeout(None)

            async def read_until_deadline() -> None:
                async with timeout_scope:
                    await anext(follower)

            pending = asyncio.create_task(read_until_deadline())
            await entered.wait()
            timeout_scope.reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError):
                await pending
        else:
            pending = asyncio.create_task(anext(follower))
            await entered.wait()
            with pytest.raises(TraceFollowLifecycleError, match="already active"):
                await anext(follower)
            assert not pending.done()
            if ending == "external_close":
                await follower.aclose()
            else:
                pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert settled.is_set()
        await follower.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(follower)
    finally:
        released.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
        monkeypatch.setattr(store.backend, "read_event_page", read_page)
    # A completed follower leaves the caller's history source usable for writes and reads.
    await record("after-close")
    assert (await reader.get("thread")).snapshot.head_run_id == "after-close"
