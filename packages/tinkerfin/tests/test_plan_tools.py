"""Ordinary Plan tools, root replies and silent card dismissal."""

from __future__ import annotations

import pytest
from ag_ui.core import RunErrorEvent, TextMessageContentEvent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from packages.tinkerfin.tests.test_plan_mode import (
    _agui_events,
    _assert_success,
    _FakeModel,
    _plan_binding,
    _planner,
    _planner_clarification,
    _terminal,
)

from tinkerfin import TinkerFin
from tinkerfin.plan import PlanReviewAction, StructuredPlanContent
from tinkerfin_tracing import Tracer


def _card(kind: str) -> AIMessage:
    return _planner_clarification() if kind == "clarification" else _planner()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "review"])
async def test_dismiss_card_is_silent_and_keeps_trusted_context(kind: str) -> None:
    tracer = Tracer()
    model = _FakeModel(
        responses=[_card(kind), AIMessage(content="We can discuss it.", id="reply")]
    )
    saver = InMemorySaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan()
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    opening = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    payload: dict[str, object] = {"type": "dismiss"}
    if kind == "review":
        payload["baseRevision"] = 1
    binding = _plan_binding(_terminal(opening), payload=payload)
    closed = await _agui_events(
        runtime, None, run_id="close", config=config, mode="plan", resume=binding
    )
    _assert_success(closed)
    assert len(model.model_inputs) == 1
    assert not any(isinstance(event, TextMessageContentEvent) for event in closed)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert not history.summary.pending_interactions
    plan = history.state.root["tinkerfin_plan"]
    assert isinstance(plan, dict)
    assert plan["status"] == "awaiting_input"
    assert plan["effectiveMode"] == "plan"
    assert plan["handoff"] is None
    assert plan["clarificationHistory"] == []
    contexts = plan["discussionHistory"]
    assert isinstance(contexts, list)
    first_context = contexts[0]
    assert isinstance(first_context, dict)
    assert first_context["messageId"] is None
    assert len([message for message in history.messages if message.role == "user"]) == 1
    replay_runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .with_plan()
        .build(model=model)
    )
    retried = await _agui_events(
        replay_runtime, None, run_id="close", config=config, mode="plan", resume=binding
    )
    _assert_success(retried)
    assert len(model.model_inputs) == 1
    stale = await _agui_events(
        runtime, None, run_id="stale", config=config, mode="plan", resume=binding
    )
    assert isinstance(stale[-1], RunErrorEvent)
    followup = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Explain the card", id="followup")]},
        run_id="followup",
        config=config,
        mode="plan",
    )
    _assert_success(followup)
    assert len(model.model_inputs) == 2
    assert "messageId" in str(model.model_inputs[-1])


@pytest.mark.asyncio
async def test_ordinary_reply_needs_one_model_call_and_no_plan_tool() -> None:
    model = _FakeModel(
        responses=[AIMessage(content="A useful explanation.", id="reply")]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan()
        .build(model=model)
    )
    events = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Explain", id="request")]},
        run_id="reply",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    _assert_success(events)
    assert len(model.model_inputs) == 1
    assert (
        "".join(
            event.delta
            for event in events
            if isinstance(event, TextMessageContentEvent)
        )
        == "A useful explanation."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["approve", "reject", "cancel", "edit", "respond", "dismiss"]
)
async def test_stale_review_does_not_consume_card_and_corrected_request_succeeds(
    action: str,
) -> None:
    tracer = Tracer()
    model = _FakeModel(responses=[_planner()])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan(allowed_review_actions=tuple(PlanReviewAction))
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    opened = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    payload: dict[str, object] = {"type": action, "baseRevision": 2}
    if action == "respond":
        payload["message"] = "Discuss the draft"
    elif action == "edit":
        payload["content"] = StructuredPlanContent.model_validate(
            _planner().tool_calls[0]["args"]["content"]
        ).model_dump(mode="json", by_alias=True)
    rejected = await _agui_events(
        runtime,
        None,
        run_id="stale",
        config=config,
        mode="plan",
        resume=_plan_binding(_terminal(opened), payload=payload),
    )
    assert isinstance(rejected[-1], RunErrorEvent)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert len(history.summary.pending_interactions) == 1
    assert len(model.model_inputs) == 1
    corrected = await _agui_events(
        runtime,
        None,
        run_id="corrected",
        config=config,
        mode="plan",
        resume=_plan_binding(
            _terminal(opened), payload={"type": "dismiss", "baseRevision": 1}
        ),
    )
    _assert_success(corrected)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert not history.summary.pending_interactions
    assert len(model.model_inputs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["dismiss", "discuss"])
async def test_closed_clarification_retains_previously_submitted_edit(
    action: str,
) -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            _planner_clarification(),
            AIMessage(content="We can discuss the submitted edit."),
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan(allowed_review_actions=(PlanReviewAction.EDIT,))
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    opened = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    content = StructuredPlanContent.model_validate(
        _planner().tool_calls[0]["args"]["content"]
    ).model_dump(mode="json", by_alias=True)
    content["goal"] = "USER_EDIT_SENTINEL"
    clarification = await _agui_events(
        runtime,
        None,
        run_id="edit",
        config=config,
        mode="plan",
        resume=_plan_binding(
            _terminal(opened),
            payload={"type": "edit", "baseRevision": 1, "content": content},
        ),
    )
    assert "USER_EDIT_SENTINEL" in str(model.model_inputs[-1])
    payload: dict[str, object] = {"type": action}
    if action == "discuss":
        payload["message"] = "Explain my submitted edit"
    closed = await _agui_events(
        runtime,
        None,
        run_id="close",
        config=config,
        mode="plan",
        resume=_plan_binding(_terminal(clarification), payload=payload),
    )
    _assert_success(closed)
    if action == "dismiss":
        assert len(model.model_inputs) == 2
        followup = await _agui_events(
            runtime,
            {
                "messages": [
                    HumanMessage(content="Explain my submitted edit", id="followup")
                ]
            },
            run_id="followup",
            config=config,
            mode="plan",
        )
        _assert_success(followup)
    assert "USER_EDIT_SENTINEL" in str(model.model_inputs[-1])
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    plan = history.state.root["tinkerfin_plan"]
    assert isinstance(plan, dict)
    assert plan["pendingEdit"] is None
    assert plan["handoff"] is None
    assert plan["effectiveMode"] == "plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reject", "cancel"])
async def test_review_acknowledgement_cannot_execute_unoffered_plan_tool(
    action: str,
) -> None:
    tracer = Tracer()
    model = _FakeModel(
        responses=[
            _planner(),
            _planner(suffix=" unsolicited"),
            AIMessage(content="I will keep planning without that draft."),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan(allowed_review_actions=tuple(PlanReviewAction))
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    opened = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    result = await _agui_events(
        runtime,
        None,
        run_id="decision",
        config=config,
        mode="plan",
        resume=_plan_binding(
            _terminal(opened), payload={"type": action, "baseRevision": 1}
        ),
    )
    _assert_success(result)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert not history.summary.pending_interactions
    plan = history.state.root["tinkerfin_plan"]
    assert isinstance(plan, dict)
    assert plan["revision"] == 1
    assert plan["status"] == "awaiting_input"
    assert plan["handoff"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_direct_read_only_result_ends_the_turn_and_keeps_plan_context(
    editing: bool,
) -> None:
    from langchain_core.tools import tool

    @tool(return_direct=True)
    async def read_status() -> str:
        """Read the current synthetic status."""
        return "All good"

    read_status.metadata = {"read_only": True}
    read_call = AIMessage(
        content="", tool_calls=[{"name": "read_status", "args": {}, "id": "read-1"}]
    )
    model = _FakeModel(
        responses=[
            *([_planner()] if editing else []),
            read_call,
            AIMessage(content="We can continue planning."),
        ]
    )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan(allowed_review_actions=tuple(PlanReviewAction))
        .build(model=model, tools=[read_status])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    result = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    if editing:
        content = StructuredPlanContent.model_validate(
            _planner(goal="USER_EDIT_SENTINEL").tool_calls[0]["args"]["content"]
        ).model_dump(mode="json", by_alias=True)
        result = await _agui_events(
            runtime,
            None,
            run_id="edit",
            config=config,
            mode="plan",
            resume=_plan_binding(
                _terminal(result),
                payload={"type": "edit", "baseRevision": 1, "content": content},
            ),
        )
    _assert_success(result)
    assert len(model.model_inputs) == (2 if editing else 1)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    plan = history.state.root["tinkerfin_plan"]
    assert isinstance(plan, dict)
    assert plan["status"] == "awaiting_input"
    assert plan["handoff"] is None
    followup = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Continue planning", id="followup")]},
        run_id="followup",
        config=config,
        mode="plan",
    )
    _assert_success(followup)
    if editing:
        assert "USER_EDIT_SENTINEL" in str(model.model_inputs[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("content_kind", ["custom_validation", "blank_builtin"])
async def test_semantically_invalid_edit_preserves_pending_card(
    content_kind: str,
) -> None:
    from pydantic import field_validator

    from tinkerfin.plan import PlanContentModel

    class RestrictedContent(PlanContentModel):
        goal: str

        @field_validator("goal")
        @classmethod
        def require_valid_goal(cls, value: str) -> str:
            if value == "invalid":
                raise ValueError("The goal violates the host's business constraint")
            return value

    content_schema = (
        RestrictedContent
        if content_kind == "custom_validation"
        else StructuredPlanContent
    )
    draft_content = (
        {"goal": "valid"}
        if content_kind == "custom_validation"
        else StructuredPlanContent.model_validate(
            _planner().tool_calls[0]["args"]["content"]
        ).model_dump(mode="json", by_alias=True)
    )
    invalid_content = {
        **draft_content,
        "goal": "invalid" if content_kind == "custom_validation" else "   ",
    }
    tracer = Tracer()
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_plan",
                        "args": {"content": draft_content},
                        "id": "draft",
                    }
                ],
            )
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan(
            content_schema=content_schema,
            allowed_review_actions=(PlanReviewAction.EDIT,),
        )
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    opened = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    invalid = await _agui_events(
        runtime,
        None,
        run_id="invalid",
        config=config,
        mode="plan",
        resume=_plan_binding(
            _terminal(opened),
            payload={"type": "edit", "baseRevision": 1, "content": invalid_content},
        ),
    )
    assert isinstance(invalid[-1], RunErrorEvent)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert len(history.summary.pending_interactions) == 1
    assert len(model.model_inputs) == 1
    corrected = await _agui_events(
        runtime,
        None,
        run_id="corrected",
        config=config,
        mode="plan",
        resume=_plan_binding(
            _terminal(opened), payload={"type": "dismiss", "baseRevision": 1}
        ),
    )
    _assert_success(corrected)
    history = await tracer.get(runtime.thread_identity("plan-thread"))
    assert not history.summary.pending_interactions
    assert len(model.model_inputs) == 1


@pytest.mark.asyncio
async def test_changed_content_schema_does_not_consume_existing_review() -> None:
    from tinkerfin.plan import MarkdownPlanContent

    saver = InMemorySaver()
    tracer = Tracer()
    factory = TinkerFin(checkpointer=saver).with_namespace("test").with_observer(tracer)
    model = _FakeModel(responses=[_planner()])
    original = factory.with_plan().build(model=model)
    changed = factory.with_plan(content_schema=MarkdownPlanContent).build(model=model)
    config = {"configurable": {"thread_id": "plan-thread"}}
    opened = await _agui_events(
        original,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="open",
        config=config,
        mode="plan",
    )
    dismissal = _plan_binding(
        _terminal(opened), payload={"type": "dismiss", "baseRevision": 1}
    )
    invalid = await _agui_events(
        changed,
        None,
        run_id="different-schema",
        config=config,
        mode="plan",
        resume=dismissal,
    )
    assert isinstance(invalid[-1], RunErrorEvent)
    history = await tracer.get(original.thread_identity("plan-thread"))
    assert len(history.summary.pending_interactions) == 1
    corrected = await _agui_events(
        original,
        None,
        run_id="original-schema",
        config=config,
        mode="plan",
        resume=dismissal,
    )
    _assert_success(corrected)
    assert len(model.model_inputs) == 1
