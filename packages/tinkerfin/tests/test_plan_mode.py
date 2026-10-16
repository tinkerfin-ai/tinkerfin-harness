"""Public Plan Mode contracts and native Deep Agent handoff behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import pytest
from ag_ui.core import (
    BaseEvent,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    StateSnapshotEvent,
)
from ag_ui.core.types import ResumeEntry
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.config import get_config
from langgraph.types import Command, Interrupt
from pydantic import PrivateAttr

from tinkerfin import (
    AgUiResumeReceipt,
    AgUiResumeRequest,
    AgUiResumeResponse,
    RunIdentity,
    TinkerFin,
)
from tinkerfin.plan import (
    PlanClarificationResponseError,
    PlanContentModel,
    PlanHandoffPhase,
    PlanReviewAction,
    PlanState,
    PlanStatus,
    StructuredPlanContent,
)
from tinkerfin.plan._workflow import PlanningWorkflowGraph
from tinkerfin_contracts import (
    ModelCallObservation,
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RunTerminalObservation,
    RuntimeObservation,
)
from tinkerfin_tracing import Tracer


class _FakeModel(FakeMessagesListChatModel):
    _bound_tool_names: list[tuple[str, ...]] = PrivateAttr(default_factory=list)
    _model_inputs: list[tuple[BaseMessage, ...]] = PrivateAttr(default_factory=list)

    @property
    def bound_tool_names(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self._bound_tool_names)

    @property
    def model_inputs(self) -> tuple[tuple[BaseMessage, ...], ...]:
        return tuple(self._model_inputs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._model_inputs.append(tuple(messages))
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tool_choice, kwargs
        self._bound_tool_names.append(
            tuple(
                tool.name if isinstance(tool, BaseTool) else str(tool) for tool in tools
            )
        )
        return self


class _InvocationModel(_FakeModel):
    _configs: list[RunnableConfig] = PrivateAttr(default_factory=list)

    @property
    def configs(self) -> tuple[RunnableConfig, ...]:
        return tuple(self._configs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._configs.append(get_config())
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _BlockingNativeModel(_FakeModel):
    block_first_call: bool = False
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _settled: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def started(self) -> asyncio.Event:
        return self._started

    @property
    def settled(self) -> asyncio.Event:
        return self._settled

    @property
    def release(self) -> asyncio.Event:
        return self._release

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.model_inputs and not self.block_first_call:
            return await super()._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
        self._started.set()
        try:
            await self._release.wait()
            raise RuntimeError("native provider failed")
        finally:
            self._settled.set()


class _PlanSession:
    def __init__(self) -> None:
        self.observations: list[RuntimeObservation] = []
        self.closed = 0
        self.failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )

    async def observe(self, observation: RuntimeObservation) -> None:
        self.observations.append(observation)

    async def force(self, boundary: ObservationBoundary) -> None:
        del boundary

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        return self.failure

    async def aclose(self) -> None:
        self.closed += 1


class _PlanObserver:
    def __init__(self) -> None:
        self.sessions: dict[str, _PlanSession] = {}

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        session = _PlanSession()
        self.sessions[context.identity.run_id] = session
        return session


class _HostModelCallbacks(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.model_calls: list[UUID] = []

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        self.model_calls.append(run_id)


def _identity(run_id: str, *, thread_id: str = "plan-thread") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _structured_plan_state(value: object) -> PlanState[StructuredPlanContent]:
    return PlanState[StructuredPlanContent].model_validate(value)


def _planner(
    *,
    suffix: str = "",
    goal: str | None = None,
) -> AIMessage:
    resolved_goal = goal or f"Implement feature{suffix}"
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_plan",
                "args": {
                    "content": {
                        "goal": resolved_goal,
                        "assumptions": [],
                        "steps": [
                            {
                                "id": "step-1",
                                "title": "Implement",
                                "description": "Implement the requested feature",
                                "verification": ["Targeted tests pass"],
                            }
                        ],
                        "acceptance_criteria": ["Feature works"],
                    }
                },
                "id": f"planner{suffix or '-1'}",
                "type": "tool_call",
            }
        ],
        id=f"planner-message{suffix or '-1'}",
    )


def _planner_clarification(
    *,
    question_id: str = "target",
    prompt: str = "Which target?",
    options: list[dict[str, object]] | None = None,
    required: bool = True,
) -> AIMessage:
    question: dict[str, object] = {
        "id": question_id,
        "answer_type": "single_choice" if options else "text",
        "prompt": prompt,
        "required": required,
    }
    if options:
        question.update({"options": options, "allow_free_text": True})
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_user_question",
                "args": {"form": {"questions": [question]}},
                "id": f"clarify-{question_id}",
                "type": "tool_call",
            }
        ],
        id=f"clarify-message-{question_id}",
    )


async def _parts(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    mode: str | None = None,
    durability: str | None = None,
    context: object | None = None,
) -> list[Mapping[str, object]]:
    runtime = cast(Any, definition)
    options: dict[str, object] = {
        "config": config,
        "stream_mode": ["messages", "tasks", "values"],
    }
    if durability is not None:
        options["durability"] = durability
    if context is not None:
        options["context"] = context
    return [
        part
        async for part in runtime.open_run(
            thread_id=_identity(run_id).thread_id,
            run_id=_identity(run_id).run_id,
            mode=mode,
            input=graph_input,
            **options,
        )
    ]


async def _agui_events(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    thread_id: str = "plan-thread",
    mode: str | None = None,
    resume: AgUiResumeRequest | None = None,
    parent_run_id: str | None = None,
) -> list[BaseEvent]:
    identity = _identity(run_id, thread_id=thread_id)
    runtime = cast(Any, definition)
    stream = (
        runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id=parent_run_id,
            mode=mode,
            resume=resume,
            config=config,
        )
        if resume is not None
        else runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id=parent_run_id,
            mode=mode,
            resume=resume,
            input=graph_input,
            config=config,
        )
    )
    return [event async for event in stream]


def _root_values(parts: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], part["data"])
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]


def _root_interrupts(parts: Sequence[Mapping[str, object]]) -> tuple[Interrupt, ...]:
    frames = [
        cast(tuple[Interrupt, ...], part.get("interrupts", ()))
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]
    return frames[-1]


def _terminal(events: Sequence[BaseEvent]) -> RunFinishedEvent:
    terminals = [event for event in events if isinstance(event, RunFinishedEvent)]
    assert len(terminals) == 1
    return terminals[0]


def _interrupt_outcome(
    events: Sequence[BaseEvent],
) -> RunFinishedInterruptOutcome:
    outcome = _terminal(events).outcome
    assert isinstance(outcome, RunFinishedInterruptOutcome)
    return outcome


def _assert_success(events: Sequence[BaseEvent]) -> None:
    assert isinstance(_terminal(events).outcome, RunFinishedSuccessOutcome)


def _resume_entry(interrupt_id: str, payload: Mapping[str, object]) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": "resolved",
            "payload": dict(payload),
        }
    )


def _plan_binding(
    terminal: RunFinishedEvent,
    *,
    payload: Mapping[str, object],
) -> AgUiResumeRequest:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt = terminal.outcome.interrupts[0]
    return AgUiResumeRequest(entries=(_resume_entry(interrupt.id, payload),))


@pytest.mark.asyncio
async def test_plan_review_and_execution_remain_queryable_with_tracing() -> None:
    tracer = Tracer()
    model = _FakeModel(responses=[_planner(), AIMessage(content="Delivered")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan()
        .build(model=model)
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Prepare a report", id="request")]},
        run_id="traced-plan",
        config=config,
        mode="plan",
    )
    outcome = _interrupt_outcome(review)
    assert outcome.interrupts[0].reason == "tinkerfin:plan_review"
    thread_identity = runtime.thread_identity("plan-thread")
    pending = await tracer.get(thread_identity)
    assert pending.status.execution == "waiting"
    assert len(pending.interactions) == 1
    assert pending.interactions[0].status == "pending"
    completed = await _agui_events(
        runtime,
        None,
        run_id="traced-execution",
        parent_run_id="traced-plan",
        config=config,
        resume=_plan_binding(
            _terminal(review), payload={"type": "approve", "baseRevision": 1}
        ),
    )
    _assert_success(completed)
    final = await tracer.get(thread_identity)
    assert final.status.execution == "succeeded"
    assert final.interactions[0].status == "resolved"
    graph = await tracer.query(thread_identity, limit=200)
    assert graph.nodes
    assert all(node.failure is None for node in graph.nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [False, True])
async def test_plan_approval_hands_off_to_native_with_the_same_message_id(
    observed: bool,
) -> None:
    model = _InvocationModel(responses=[_planner(), AIMessage(content="native done")])
    observer = _PlanObserver()
    callbacks = _HostModelCallbacks()
    factory = (
        TinkerFin(
            checkpointer=InMemorySaver(
                serde=JsonPlusSerializer(allowed_msgpack_modules=None)
            )
        )
        .with_namespace("test")
        .with_observer(observer)
        if observed
        else TinkerFin(
            checkpointer=InMemorySaver(
                serde=JsonPlusSerializer(allowed_msgpack_modules=None)
            )
        ).with_namespace("test")
    )
    definition = factory.with_plan(enabled=True).build(
        model=model,
        tools=[],
    )
    config: RunnableConfig = {
        "configurable": {"thread_id": "plan-thread", "caller_policy": "restricted"},
        "callbacks": [callbacks],
        "tags": ["caller-tag"],
        "metadata": {"caller_note": "retained"},
        "recursion_limit": 18,
    }
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="request-1")]},
        run_id="approve-1",
        config=config,
        mode="plan",
    )
    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="approve-2",
        config=config,
        mode="default",
    )
    final = _root_values(completed)[-1]
    plan = _structured_plan_state(final["tinkerfin_plan"])
    assert plan.status is PlanStatus.APPROVED
    assert plan.effective_mode == "default"
    assert plan.handoff is not None
    final_messages = cast(Sequence[BaseMessage], final["messages"])
    assert final_messages[-1].content == "native done"
    original_user = next(
        message
        for message in final_messages
        if isinstance(message, HumanMessage) and message.id == "request-1"
    )
    assert original_user.content == "Implement"
    assert not any(
        "<tinkerfin-approved-plan" in str(message.content) for message in final_messages
    )
    execution_input = model.model_inputs[-1]
    handoff_user = next(
        message
        for message in execution_input
        if isinstance(message, HumanMessage) and message.id == "request-1"
    )
    assert handoff_user.content == "Implement"
    handoff_system = next(
        message
        for message in execution_input
        if isinstance(message, SystemMessage)
        and "<tinkerfin-approved-plan" in str(message.content)
    )
    assert plan.handoff.digest in str(handoff_system.content)
    assert "<tinkerfin-approved-plan" not in repr(completed)
    assert all(
        "execute_deep_agent:" not in "|".join(cast(tuple[str, ...], part["ns"]))
        for part in completed
    )
    assert _root_interrupts(first)[0].value["kind"] == "tinkerfin:plan_review"
    assert len(callbacks.model_calls) == 2
    execution_config = model.configs[-1]
    assert execution_config.get("recursion_limit") == 18
    assert "caller-tag" in execution_config.get("tags", [])
    assert execution_config.get("metadata", {}).get("caller_note") == "retained"
    assert execution_config.get("configurable", {}).get("caller_policy") == "restricted"
    if observed:
        session = observer.sessions["approve-2"]
        calls = [
            item
            for item in session.observations
            if isinstance(item, ModelCallObservation)
        ]
        assert [item.phase for item in calls] == ["started", "completed"]
        assert calls[0].call_id == calls[1].call_id
        assert any(
            item.message_type == "system"
            and "<tinkerfin-approved-plan" in str(item.content)
            for item in calls[0].messages
        )
        terminals = [
            item
            for item in session.observations
            if isinstance(item, RunTerminalObservation)
        ]
        assert [item.outcome for item in terminals] == ["succeeded"]
        assert session.closed == 1


@pytest.mark.asyncio
async def test_duplicate_plan_approval_does_not_execute_native_twice() -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="duplicate-1",
        config=config,
        mode="plan",
    )
    command = Command(resume={"type": "approve", "baseRevision": 1})
    await _parts(
        definition,
        command,
        run_id="duplicate-2",
        config=config,
        mode="default",
    )
    calls = len(model.model_inputs)
    duplicate = await _parts(
        definition,
        command,
        run_id="duplicate-3",
        config=config,
        mode="default",
    )
    assert len(model.model_inputs) == calls
    assert len(_root_values(duplicate)) == 1
    assert (
        _structured_plan_state(_root_values(duplicate)[0]["tinkerfin_plan"]).status
        is PlanStatus.APPROVED
    )


@pytest.mark.asyncio
async def test_plan_handoff_recovers_after_native_checkpoint_precedes_plan_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry must continue a durably accepted handoff without losing execution."""

    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="handoff-crash-1",
        config=config,
        mode="plan",
    )
    original = PlanningWorkflowGraph.mark_handoff_phase

    async def cancel_after_native_checkpoint(
        self: PlanningWorkflowGraph[Any],
        config: RunnableConfig,
        plan: PlanState[PlanContentModel],
        *,
        phase: PlanHandoffPhase,
        native_checkpoint_id: str,
        completed_checkpoint_id: str | None = None,
    ) -> PlanState[PlanContentModel]:
        if phase is PlanHandoffPhase.ACCEPTED:
            raise asyncio.CancelledError
        return await original(
            self,
            config,
            plan,
            phase=phase,
            native_checkpoint_id=native_checkpoint_id,
            completed_checkpoint_id=completed_checkpoint_id,
        )

    monkeypatch.setattr(
        PlanningWorkflowGraph,
        "mark_handoff_phase",
        cancel_after_native_checkpoint,
    )
    command = Command(resume={"type": "approve", "baseRevision": 1})
    with pytest.raises(asyncio.CancelledError):
        await _parts(
            definition,
            command,
            run_id="handoff-crash-2",
            config=config,
            mode="default",
        )
    monkeypatch.setattr(PlanningWorkflowGraph, "mark_handoff_phase", original)

    retry = await _parts(
        definition,
        command,
        run_id="handoff-crash-3",
        config=config,
        mode="default",
    )
    final = _structured_plan_state(_root_values(retry)[-1]["tinkerfin_plan"])
    assert len(model.model_inputs) == 2
    assert final.handoff is not None
    assert final.handoff.phase is PlanHandoffPhase.COMPLETED
    assert any("native done" in str(part.get("data")) for part in retry)


@pytest.mark.asyncio
async def test_agui_plan_resume_checkpoints_durably_and_retries_without_reexecution() -> (
    None
):
    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="settlement-review",
        config=config,
        mode="plan",
    )
    terminal = _terminal(review)
    payload = {"type": "approve", "baseRevision": 1}
    binding = _plan_binding(
        terminal,
        payload=payload,
    )
    identity = _identity("settlement-execution")
    checkpoints: list[AgUiResumeReceipt] = []

    async def checkpointed(value: AgUiResumeReceipt) -> None:
        checkpoints.append(value)

    runtime = cast(Any, definition)
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            mode="default",
            resume=binding,
            on_resume_saved=checkpointed,
            config=config,
        )
    ]
    calls = len(model.model_inputs)

    _assert_success(events)
    assert len(checkpoints) == 1
    assert checkpoints[0].identity == identity
    assert checkpoints[0].parent_run_id == "settlement-review"
    assert checkpoints[0].responses == (
        AgUiResumeResponse(binding.entries[0].interrupt_id, "resolved"),
    )
    serialized_events = "\n".join(
        event.model_dump_json(by_alias=True) for event in events
    )
    assert "<tinkerfin-approved-plan" not in serialized_events
    assert "_tinkerfin_plan_handoff_digest" not in serialized_events
    snapshots = [event for event in review if isinstance(event, MessagesSnapshotEvent)]
    assert snapshots
    assert [
        message.content
        for snapshot in snapshots
        for message in snapshot.messages
        if message.role == "user"
    ] == ["Implement"]
    assert all(
        "_tinkerfin_resume" not in event.snapshot
        and "_tinkerfin_lineage" not in event.snapshot
        for event in events
        if isinstance(event, StateSnapshotEvent)
    )

    retry = cast(Any, definition)
    retried = [
        event
        async for event in retry.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            mode="default",
            resume=binding,
            on_resume_saved=checkpointed,
            config=config,
        )
    ]

    _assert_success(retried)
    assert len(model.model_inputs) == calls
    assert len(checkpoints) == 2
    assert checkpoints[0] == checkpoints[1]


@pytest.mark.asyncio
async def test_clarification_rejects_incomplete_and_unknown_answers() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(
                responses=[
                    _planner_clarification(
                        question_id="target", options=[{"id": "a", "label": "A"}]
                    ),
                    _planner(),
                ]
            ),
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="message")]},
        run_id="invalid-answer-1",
        config=config,
        mode="plan",
    )
    with pytest.raises(
        PlanClarificationResponseError,
        match="does not match the pending form",
    ):
        await _parts(
            definition,
            Command(
                resume={
                    "type": "respond",
                    "answers": {
                        "target": {
                            "status": "answered",
                            "answerType": "single_choice",
                            "optionId": "unknown",
                        }
                    },
                }
            ),
            run_id="invalid-answer-2",
            config=config,
            mode="plan",
        )
    retried = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "target": {
                        "status": "answered",
                        "answerType": "single_choice",
                        "optionId": "a",
                    }
                },
            }
        ),
        run_id="invalid-answer-3",
        config=config,
        mode="plan",
    )
    assert (
        _structured_plan_state(_root_values(retried)[-1]["tinkerfin_plan"]).status
        is PlanStatus.AWAITING_REVIEW
    )


@pytest.mark.asyncio
async def test_review_rejects_stale_revision_and_reject_waits_for_plan_input() -> None:
    config = {"configurable": {"thread_id": "plan-thread"}}
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="review-1",
        config=config,
        mode="plan",
    )
    with pytest.raises(ValueError, match="baseRevision is stale"):
        await _parts(
            definition,
            Command(resume={"type": "approve", "baseRevision": 2}),
            run_id="review-stale",
            config=config,
            mode="default",
        )

    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(
                responses=[
                    _planner(),
                    AIMessage(content="I will keep planning without that draft."),
                    _planner(suffix=" revised"),
                ]
            ),
            tools=[],
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message-2")]},
        run_id="reject-1",
        config=config,
        mode="plan",
    )
    rejected = await _parts(
        definition,
        Command(resume={"type": "reject", "baseRevision": 1}),
        run_id="reject-2",
        config=config,
        mode="plan",
    )
    plan = _structured_plan_state(_root_values(rejected)[-1]["tinkerfin_plan"])
    assert plan.status is PlanStatus.AWAITING_INPUT
    assert plan.effective_mode == "plan"
    assert plan.review_action is PlanReviewAction.REJECT
    assert plan.review_reason is None
    root_messages = [
        cast(tuple[BaseMessage, object], part["data"])[0]
        for part in rejected
        if part["type"] == "messages" and part["ns"] == ()
    ]
    assert (
        sum(
            isinstance(message, AIMessage)
            and message.content == "I will keep planning without that draft."
            for message in root_messages
        )
        == 1
    )

    continued = await _parts(
        definition,
        {"messages": [HumanMessage(content="Use a smaller scope", id="message-3")]},
        run_id="reject-3",
        config=config,
        mode="plan",
    )
    continued_plan = _structured_plan_state(
        _root_values(continued)[-1]["tinkerfin_plan"]
    )
    assert continued_plan.status is PlanStatus.AWAITING_REVIEW
    assert continued_plan.effective_mode == "plan"
    assert continued_plan.revision == 2
    assert continued_plan.request_message_id == "message-3"


@pytest.mark.asyncio
async def test_agui_interrupt_snapshots_precede_the_terminal() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
        )
    )
    events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="agui-review",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    types = [event.type.value for event in events]
    state_index = len(types) - 1 - types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(types) - 1 - types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(types) - 1 - types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index
    snapshots = [event for event in events if isinstance(event, StateSnapshotEvent)]
    assert all(
        "_tinkerfin_plan_clarification_schema" not in event.snapshot
        for event in snapshots
    )


def _planner_reply() -> AIMessage:
    return AIMessage(content="Here is why.", id="discussion-reply")


@pytest.mark.asyncio
async def test_discussion_and_approval_cannot_both_consume_the_same_card() -> None:
    from tinkerfin.coordination import InMemoryRunCoordinator

    model = _BlockingNativeModel(responses=[_planner()])
    runtime = (
        TinkerFin(
            checkpointer=InMemorySaver(), run_coordinator=InMemoryRunCoordinator()
        )
        .with_namespace("test")
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
    discussion = _plan_binding(
        _terminal(opening),
        payload={"type": "respond", "baseRevision": 1, "message": "Explain"},
    )
    approval = _plan_binding(
        _terminal(opening), payload={"type": "approve", "baseRevision": 1}
    )
    discussing = asyncio.create_task(
        _agui_events(
            runtime,
            None,
            run_id="discuss",
            config=config,
            mode="plan",
            resume=discussion,
        )
    )
    attempted = asyncio.Event()

    async def approve() -> list[BaseEvent]:
        attempted.set()
        return await _agui_events(
            runtime, None, run_id="approve", config=config, mode="plan", resume=approval
        )

    approving: asyncio.Task[list[BaseEvent]] | None = None
    try:
        await model.started.wait()
        approving = asyncio.create_task(approve())
        await attempted.wait()
        model.release.set()
        discussed, approved = await asyncio.gather(discussing, approving)
        assert any(isinstance(event, RunErrorEvent) for event in discussed)
        approval_error = next(
            event for event in approved if isinstance(event, RunErrorEvent)
        )
        assert isinstance(approval_error.raw_event, dict)
        assert approval_error.raw_event.get("initializationFailed") is True
        assert len(model.model_inputs) == 1
    finally:
        model.release.set()
        await asyncio.gather(
            discussing, *([approving] if approving else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_cancelling_a_plan_reply_closes_its_model_call() -> None:
    model = _BlockingNativeModel(responses=[_planner_reply()], block_first_call=True)
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan()
        .build(model=model)
    )
    stream = runtime.open_agui_run(
        thread_id="cancel-reply",
        run_id="reply",
        mode="plan",
        messages=[{"id": "request", "role": "user", "content": "Explain"}],
    )

    async def consume() -> None:
        async for _ in stream:
            pass

    consuming = asyncio.create_task(consume())
    try:
        await model.started.wait()
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming
        assert model.settled.is_set()
    finally:
        model.release.set()
        await stream.aclose()
        await asyncio.gather(consuming, return_exceptions=True)
