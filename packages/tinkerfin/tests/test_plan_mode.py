"""Public Plan Mode contracts and native Deep Agent handoff behavior."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import time
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

import pytest
from ag_ui.core import (
    BaseEvent,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
)
from ag_ui.core.types import ResumeEntry
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.graph import DeepAgentState
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    InputAgentState,
)
from langchain.tools import tool
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
    ToolMessage,
)
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Interrupt, interrupt
from pydantic import Field, PrivateAttr, ValidationError, field_serializer
from redis.asyncio import Redis
from redis.exceptions import ResponseError

import tinkerfin.plan as plan_api
import tinkerfin.plan._runtime as plan_runtime_module
from tinkerfin import (
    AgUiResumeCheckpoint,
    AgUiResumeRequest,
    RunIdentity,
    RunObservationError,
    TinkerFin,
)
from tinkerfin._agui_lineage import (
    RUN_ID_METADATA_KEY,
)
from tinkerfin.deep_agent import create_graph
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOption,
    DefaultClarificationForm,
    MarkdownPlanContent,
    PlanClarificationResponseError,
    PlanContentModel,
    PlanDraft,
    PlanHandoffPhase,
    PlanModeConfigurationError,
    PlanReviewAction,
    PlanSchemaReference,
    PlanState,
    PlanStatus,
    PlanStructuredOutputError,
    SingleChoiceQuestion,
    StructuredPlanContent,
    StructuredPlanStep,
    TextQuestion,
    TimeQuestion,
)
from tinkerfin.plan._clarification import (
    build_response_schema,
    create_clarification_binding,
    validate_and_normalize_response,
)
from tinkerfin.plan._workflow import PlanningWorkflowGraph
from tinkerfin_agui_adapter import (
    ResumeMapper,
    RuntimeInterruptEnvelope,
    ScopedIdCodec,
)
from tinkerfin_contracts import (
    ModelCallObservation,
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RunTerminalObservation,
    RuntimeObservation,
    ToolExecutionObservation,
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


class _GlobalState(DeepAgentState):
    global_marker: str


class _DefinitionState(DeepAgentState):
    definition_marker: str


class _RuntimeContext(TypedDict):
    tenant: str


class _ContextAwareModel(_FakeModel):
    _contexts: list[_RuntimeContext] = PrivateAttr(default_factory=list)

    @property
    def contexts(self) -> tuple[_RuntimeContext, ...]:
        return tuple(self._contexts)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        from langgraph.runtime import get_runtime

        context = get_runtime(_RuntimeContext).context
        assert context is not None
        self._contexts.append(context)
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


class _QuestionAttributes(ClarificationModel):
    category: str


class _OptionAttributes(ClarificationModel):
    priority: int


class _CustomOption(ClarificationOption[_OptionAttributes]):
    pass


class _CustomQuestion(SingleChoiceQuestion[_QuestionAttributes, _CustomOption]):
    pass


class _CustomForm(ClarificationForm[_CustomQuestion]):
    pass


class _BoundedForm(ClarificationForm[_CustomQuestion]):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=2, max_length=5)


class _ExactFourForm(ClarificationForm[_CustomQuestion]):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=4, max_length=4)


class _TextOnlyForm(ClarificationForm[TextQuestion[ClarificationModel]]):
    pass


class _MissingMinimumForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...]


class _DefaultedQuestionsForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(default=(), min_length=1)


class _AliasedQuestionsForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=1, alias="items")


class _ImpossibleRangeForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=2, max_length=1)


class _OpaqueQuestionAttributes(ClarificationModel):
    token: object

    @field_serializer("token")
    def token_is_not_json(self, value: object) -> object:
        del value
        return object()


class _OpaqueQuestion(TextQuestion[_OpaqueQuestionAttributes]):
    pass


class _OpaqueForm(ClarificationForm[_OpaqueQuestion]):
    pass


class _InvalidListForm(ClarificationFormBase):
    questions: list[TextQuestion[ClarificationModel]] = []


class _CustomPlanContent(PlanContentModel):
    summary: str
    checks: tuple[str, ...]


class _MarkdownImpostor(PlanContentModel):
    media_type = "text/markdown"

    markdown: str


def _identity(run_id: str, *, thread_id: str = "plan-thread") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _structured_plan_state(value: object) -> PlanState[StructuredPlanContent]:
    return PlanState[StructuredPlanContent].model_validate(value)


def _markdown_plan_state(value: object) -> PlanState[MarkdownPlanContent]:
    return PlanState[MarkdownPlanContent].model_validate(value)


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
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "draft": {
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
                    },
                },
                "id": f"planner{suffix or '-1'}",
                "type": "tool_call",
            }
        ],
        id=f"planner-message{suffix or '-1'}",
    )


def _markdown_planner(markdown: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "draft": {"markdown": markdown},
                },
                "id": "markdown-planner",
                "type": "tool_call",
            }
        ],
        id="markdown-planner-message",
    )


def _custom_plan_planner() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "draft": {
                        "summary": "Release safely",
                        "checks": ["Targeted tests pass"],
                    },
                },
                "id": "custom-plan-planner",
                "type": "tool_call",
            }
        ],
        id="custom-plan-planner-message",
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
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "clarification": {"questions": [question]},
                },
                "id": f"clarify-{question_id}",
                "type": "tool_call",
            }
        ],
        id=f"clarify-message-{question_id}",
    )


def _planner_clarification_batch(
    count: int = 4,
    *,
    required: bool = True,
) -> AIMessage:
    questions = [
        {
            "id": f"question-{index}",
            "answer_type": "single_choice" if index % 2 == 0 else "text",
            "prompt": f"Choose value {index}",
            "required": required,
            **(
                {
                    "options": [{"id": f"option-{index}", "label": f"Option {index}"}],
                    "allow_free_text": True,
                }
                if index % 2 == 0
                else {}
            ),
        }
        for index in range(count)
    ]
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "clarification": {"questions": questions},
                },
                "id": "clarify-batch",
                "type": "tool_call",
            }
        ],
        id="clarify-batch-message",
    )


def _custom_planner_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "clarification": {
                        "questions": [
                            {
                                "id": "custom-target",
                                "answer_type": "single_choice",
                                "prompt": "Choose a target",
                                "required": True,
                                "allow_free_text": True,
                                "attributes": {"category": "deployment"},
                                "options": [
                                    {
                                        "id": "target-a",
                                        "label": "Target A",
                                        "description": "Use target A",
                                        "attributes": {"priority": 1},
                                    }
                                ],
                            }
                        ]
                    },
                },
                "id": "custom-clarification",
                "type": "tool_call",
            }
        ],
        id="custom-clarification-message",
    )


def _opaque_planner_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "clarification": {
                        "questions": [
                            {
                                "id": "opaque",
                                "answer_type": "text",
                                "prompt": "Choose",
                                "required": True,
                                "attributes": {"token": "private"},
                            }
                        ]
                    },
                },
                "id": "opaque-clarification",
                "type": "tool_call",
            }
        ],
    )


def _accept_edit() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "accept_edit",
                },
                "id": "accept-edit",
                "type": "tool_call",
            }
        ],
        id="accept-edit-message",
    )


def _invalid_planner_json() -> AIMessage:
    return AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": '{"type":"draft",}',
                "id": "invalid-planner-json",
                "error": "invalid JSON arguments",
                "type": "invalid_tool_call",
            }
        ],
        id="invalid-planner-message",
    )


def _todo_call(*, content: str = "Execute") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": content, "status": "in_progress"}]},
                "id": f"todo-{content}",
                "type": "tool_call",
            }
        ],
        id=f"todo-message-{content}",
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
async def test_replanning_keeps_rejected_reply_as_model_input_not_child_output() -> (
    None
):
    tracer = Tracer()
    reply = "That draft is rejected; planning continues."
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(tracer)
        .with_plan()
        .build(
            model=_FakeModel(
                responses=[
                    _planner(),
                    AIMessage(content=reply, id="rejected-reply"),
                    _planner(suffix=" revised"),
                ]
            )
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        runtime,
        {"messages": [HumanMessage(content="Prepare a report", id="request")]},
        run_id="draft",
        config=config,
        mode="plan",
    )
    await _parts(
        runtime,
        Command(resume={"type": "reject", "baseRevision": 1}),
        run_id="reject",
        config=config,
        mode="plan",
    )
    await _parts(
        runtime,
        {"messages": [HumanMessage(content="Narrow the report", id="revision")]},
        run_id="replan",
        config=config,
        mode="plan",
    )
    identity = runtime.thread_identity("plan-thread")
    graph = await tracer.query(identity, limit=200)
    assert not any(node.link_issues for node in graph.nodes)
    replies = [node for node in graph.nodes if node.source_id == "rejected-reply"]
    assert len(replies) == 1
    assert replies[0].content == reply
    assert replies[0].graph_namespace == ()
    assert replies[0].model_call_id is not None
    requests = [
        node.request
        for node in graph.nodes
        if node.run_id == "replan" and node.request is not None
    ]
    request_messages = [
        messages
        for request in requests
        if isinstance(request, dict)
        and isinstance(messages := request.get("messages"), list)
    ]
    assert any(
        isinstance(message, dict)
        and message.get("id") == "rejected-reply"
        and message.get("content") == reply
        for messages in request_messages
        for message in messages
    )
    snapshot = await tracer.get(identity)
    assert snapshot.status.execution == "waiting"
    assert sum(item.status == "pending" for item in snapshot.interactions) == 1


def test_plan_configuration_is_immutable_and_preserves_build_signature() -> None:
    base = TinkerFin().with_namespace("test")
    planned = base.with_plan(enabled=True, default_mode="plan")

    assert planned is not base
    assert (
        inspect.signature(planned.build).parameters
        == inspect.signature(base.build).parameters
    )
    base.build(model=_FakeModel(responses=[AIMessage(content="ok")]))
    planned.build(model=_FakeModel(responses=[AIMessage(content="default only")]))


def test_plan_package_exports_only_the_current_contract() -> None:
    assert set(plan_api.__all__) == {
        "AgentMode",
        "BuiltInClarificationForm",
        "ClarificationExchange",
        "ClarificationForm",
        "ClarificationFormBase",
        "ClarificationModel",
        "ClarificationOption",
        "ClarificationOptionBase",
        "ClarificationQuestionBase",
        "ClarificationResponseBase",
        "ClarificationType",
        "ConfirmedPlan",
        "DateQuestion",
        "DateTimeQuestion",
        "DefaultClarificationForm",
        "MarkdownPlanContent",
        "MultipleChoiceQuestion",
        "PendingClarification",
        "PlanClarificationResponseError",
        "PlanContentModel",
        "PlanDraft",
        "PlanHandoff",
        "PlanHandoffPhase",
        "PlanModeConfigurationError",
        "PlanReviewAction",
        "PlanSchemaReference",
        "PlanState",
        "PlanStateConflictError",
        "PlanStatus",
        "PlanStructuredOutputError",
        "RequirementAnswer",
        "SingleChoiceQuestion",
        "StructuredPlanContent",
        "StructuredPlanStep",
        "TextQuestion",
        "TimeQuestion",
        "clarification_type",
    }


def test_time_clarification_validates_zone_precision_range_and_response() -> None:
    binding = create_clarification_binding(DefaultClarificationForm)
    form = cast(
        DefaultClarificationForm,
        binding.form_schema.model_validate(
            {
                "questions": [
                    {
                        "id": "deployment-time",
                        "answerType": "time",
                        "prompt": "Choose a deployment time",
                        "required": True,
                        "timeZone": "Asia/Shanghai",
                        "minimum": "09:00",
                        "maximum": "18:00",
                    }
                ]
            }
        ),
    )
    question = form.questions[0]
    assert isinstance(question, TimeQuestion)
    assert question.minimum == time(9, 0)
    assert question.maximum == time(18, 0)
    response_schema = build_response_schema(binding, form)

    answers = validate_and_normalize_response(
        binding,
        form,
        response_schema,
        {
            "type": "respond",
            "answers": {
                "deployment-time": {
                    "status": "answered",
                    "answerType": "time",
                    "time": "09:30:00",
                }
            },
        },
    )

    assert answers[0].value == {
        "time": "09:30",
        "timeZone": "Asia/Shanghai",
    }
    with pytest.raises(PlanClarificationResponseError):
        validate_and_normalize_response(
            binding,
            form,
            response_schema,
            {
                "type": "respond",
                "answers": {
                    "deployment-time": {
                        "status": "answered",
                        "answerType": "time",
                        "time": "08:59:00",
                    }
                },
            },
        )
    with pytest.raises(ValidationError, match="minute precision"):
        binding.form_schema.model_validate(
            {
                "questions": [
                    {
                        "id": "seconds",
                        "answerType": "time",
                        "prompt": "Choose",
                        "required": True,
                        "timeZone": "UTC",
                        "minimum": "09:00:01",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="IANA time zone"):
        binding.form_schema.model_validate(
            {
                "questions": [
                    {
                        "id": "zone",
                        "answerType": "time",
                        "prompt": "Choose",
                        "required": True,
                        "timeZone": "Not/A_Zone",
                    }
                ]
            }
        )


@pytest.mark.parametrize("value", [1, "true", None, object()])
def test_plan_configuration_requires_a_strict_bool(value: object) -> None:
    with pytest.raises(TypeError, match="enabled must be a bool"):
        TinkerFin().with_namespace("test").with_plan(enabled=cast(Any, value))


def test_plan_configuration_rejects_disabled_options_and_invalid_modes() -> None:
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().with_namespace("test").with_plan(enabled=False, default_mode="plan")
    with pytest.raises(PlanModeConfigurationError, match="default_mode"):
        TinkerFin().with_namespace("test").with_plan(
            default_mode=cast(Any, "automatic")
        )


@pytest.mark.parametrize(
    "schema",
    [ClarificationModel, ClarificationForm, _InvalidListForm],
)
def test_plan_configuration_rejects_invalid_clarification_schemas(
    schema: object,
) -> None:
    with pytest.raises(PlanModeConfigurationError):
        TinkerFin().with_namespace("test").with_plan(
            clarification_schema=cast(Any, schema)
        )


def test_plan_configuration_defaults_to_structured_and_freezes_custom_content() -> None:
    default_options = TinkerFin().with_namespace("test").with_plan()._plan_options
    custom_options = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(content_schema=_CustomPlanContent)
        ._plan_options
    )
    editable_options = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(
            content_schema=_CustomPlanContent,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.CANCEL,
                PlanReviewAction.EDIT,
                PlanReviewAction.RESPOND,
                PlanReviewAction.REJECT,
            ),
        )
        ._plan_options
    )

    assert default_options is not None
    assert default_options.allowed_review_actions == (
        PlanReviewAction.APPROVE,
        PlanReviewAction.RESPOND,
        PlanReviewAction.REJECT,
    )
    assert default_options.content.schema is StructuredPlanContent
    assert default_options.content.reference.media_type == "application/json"
    assert custom_options is not None
    assert custom_options.content.schema is _CustomPlanContent
    assert custom_options.content.reference.media_type == "application/json"
    planner_schema = custom_options.contracts.planner_response_type.model_json_schema(
        by_alias=True
    )
    assert planner_schema["type"] == "object"
    assert "_CustomPlanContent" in planner_schema["$defs"]
    assert set(planner_schema["discriminator"]["mapping"]) == {
        "clarify",
        "draft",
    }
    clarify_schema = planner_schema["$defs"]["PlannerClarifyOutcome"]
    draft_schema = planner_schema["$defs"]["PlannerDraftOutcome"]
    assert set(clarify_schema["properties"]) == {"type", "clarification"}
    assert set(draft_schema["properties"]) == {"type", "draft"}
    assert set(clarify_schema["required"]) == {"type", "clarification"}
    assert set(draft_schema["required"]) == {"type", "draft"}
    assert all(
        schema["additionalProperties"] is False
        for schema in (clarify_schema, draft_schema)
    )
    edit_planner_schema = (
        custom_options.contracts.planner_edit_response_type.model_json_schema(
            by_alias=True
        )
    )
    assert set(edit_planner_schema["discriminator"]["mapping"]) == {
        "clarify",
        "accept_edit",
    }
    accept_schema = edit_planner_schema["$defs"]["PlannerAcceptEditOutcome"]
    assert set(accept_schema["properties"]) == {"type"}
    assert accept_schema["required"] == ["type"]
    assert accept_schema["additionalProperties"] is False
    parsed_draft = custom_options.contracts.planner_response_type.model_validate(
        {
            "type": "draft",
            "draft": {"summary": "Release", "checks": ["Tests pass"]},
        }
    )
    assert parsed_draft.type == "draft"
    assert parsed_draft.draft is not None
    assert parsed_draft.clarification is None
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        custom_options.contracts.planner_response_type.model_validate(
            {"type": "accept_edit"}
        )
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        custom_options.contracts.planner_edit_response_type.model_validate(
            {
                "type": "draft",
                "draft": {"summary": "Release", "checks": ["Tests pass"]},
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        custom_options.contracts.planner_response_type.model_validate(
            {
                "type": "draft",
                "draft": {"summary": "Release", "checks": ["Tests pass"]},
                "clarification": None,
            }
        )
    default_review_schema = custom_options.contracts.review_response.json_schema(
        by_alias=True
    )
    assert set(default_review_schema["discriminator"]["mapping"]) == {
        "approve",
        "respond",
        "reject",
    }
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        custom_options.contracts.review_response.validate_python(
            {
                "type": "edit",
                "baseRevision": 1,
                "content": {"goal": "Not accepted by the default contract"},
            }
        )
    assert editable_options is not None
    review_schema = editable_options.contracts.review_response.json_schema(
        by_alias=True
    )
    assert review_schema["discriminator"]["mapping"]["edit"].endswith("/EditPlan")
    assert review_schema["discriminator"]["mapping"]["cancel"].endswith("/CancelPlan")
    assert review_schema["$defs"]["EditPlan"]["properties"]["content"] == {
        "$ref": "#/$defs/_CustomPlanContent"
    }


@pytest.mark.parametrize(
    "value", [None, "approve", [PlanReviewAction.APPROVE, "reject"]]
)
def test_plan_configuration_requires_fixed_review_action_values(value: object) -> None:
    with pytest.raises(TypeError, match="PlanReviewAction"):
        TinkerFin().with_namespace("test").with_plan(
            allowed_review_actions=cast(Any, value)
        )


@pytest.mark.parametrize(
    "value",
    [(), (PlanReviewAction.APPROVE, PlanReviewAction.APPROVE)],
)
def test_plan_configuration_rejects_empty_or_duplicate_allowed_review_actions(
    value: tuple[PlanReviewAction, ...],
) -> None:
    with pytest.raises(PlanModeConfigurationError, match="allowed_review_actions"):
        TinkerFin().with_namespace("test").with_plan(allowed_review_actions=value)


def test_plan_configuration_freezes_a_single_review_action() -> None:
    configured_actions = [PlanReviewAction.APPROVE]
    options = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(allowed_review_actions=configured_actions)
        ._plan_options
    )
    configured_actions.append(PlanReviewAction.EDIT)

    assert options is not None
    assert options.allowed_review_actions == (PlanReviewAction.APPROVE,)
    schema = options.contracts.review_response.json_schema(by_alias=True)
    assert schema["properties"]["type"]["const"] == "approve"
    with pytest.raises(ValidationError, match="literal_error"):
        options.contracts.review_response.validate_python(
            {"type": "edit", "baseRevision": 1, "content": {}}
        )


@pytest.mark.parametrize(
    "schema",
    [object, PlanContentModel, _MarkdownImpostor],
)
def test_plan_configuration_rejects_invalid_content_schemas(schema: object) -> None:
    with pytest.raises(PlanModeConfigurationError):
        TinkerFin().with_namespace("test").with_plan(content_schema=cast(Any, schema))


def test_plan_configuration_does_not_accept_removed_parameter_names() -> None:
    parameters = inspect.signature(TinkerFin.with_plan).parameters
    assert "content_schema" in parameters
    assert "allowed_review_actions" in parameters
    assert "plan_schema" not in parameters
    assert "review_actions" not in parameters


def test_disabled_plan_rejects_a_non_default_content_schema() -> None:
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().with_namespace("test").with_plan(
            enabled=False, content_schema=MarkdownPlanContent
        )
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().with_namespace("test").with_plan(
            enabled=False, allowed_review_actions=(PlanReviewAction.APPROVE,)
        )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (_MissingMinimumForm, "minItems >= 1"),
        (_DefaultedQuestionsForm, "must be required"),
        (_AliasedQuestionsForm, "stable JSON array field"),
        (_ImpossibleRangeForm, "maxItems must be >= minItems"),
    ],
)
def test_plan_configuration_rejects_unsafe_question_count_schemas(
    schema: type[ClarificationFormBase],
    message: str,
) -> None:
    with pytest.raises(PlanModeConfigurationError, match=message):
        TinkerFin().with_namespace("test").with_plan(clarification_schema=schema)


def test_plan_contracts_are_frozen_and_reject_duplicate_steps() -> None:
    step = StructuredPlanStep(
        id="step-1",
        title="Implement",
        description="Implement feature",
        verification=("Tests pass",),
    )
    content = StructuredPlanContent(
        goal="Implement feature",
        steps=(step,),
        acceptance_criteria=("Feature works",),
    )
    draft = PlanDraft[StructuredPlanContent](
        revision=1,
        content_schema=PlanSchemaReference(
            fingerprint="0" * 64,
            media_type=StructuredPlanContent.media_type,
        ),
        content=content,
    )
    draft_payload = draft.model_dump(mode="json", by_alias=True)
    assert set(draft_payload) == {"revision", "contentSchema", "content"}
    assert set(draft_payload["contentSchema"]) == {"fingerprint", "mediaType"}
    with pytest.raises(ValidationError):
        draft.revision = 2  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError, match="step IDs must be unique"):
        StructuredPlanContent(
            goal="Implement",
            steps=(step, step),
            acceptance_criteria=("Works",),
        )
    assert "workflowVersion" not in PlanState().model_dump(mode="json", by_alias=True)


def test_markdown_plan_rejects_blank_text_without_rewriting_content() -> None:
    markdown = "  # Plan\r\n\r\n```python\r\nprint('ok')\r\n```\r\n"

    assert MarkdownPlanContent(markdown=markdown).markdown == markdown
    with pytest.raises(ValidationError, match="markdown must not be blank"):
        MarkdownPlanContent(markdown=" \n\t")


def test_default_clarification_form_is_non_empty_without_an_upper_limit() -> None:
    schema = DefaultClarificationForm.model_json_schema(by_alias=True)
    assert "schemaVersion" not in schema["properties"]
    questions_schema = schema["properties"]["questions"]
    assert questions_schema["minItems"] == 1
    assert "maxItems" not in questions_schema

    for count in (1, 4, 8):
        form = DefaultClarificationForm.model_validate(
            {
                "questions": [
                    {
                        "id": f"question-{index}",
                        "answer_type": "text",
                        "prompt": "Choose",
                        "required": True,
                    }
                    for index in range(count)
                ]
            }
        )
        assert len(form.questions) == count
        assert all(question.required for question in form.questions)
    with pytest.raises(ValidationError, match="required"):
        DefaultClarificationForm.model_validate(
            {"questions": [{"id": "missing-required", "prompt": "Choose"}]}
        )
    with pytest.raises(ValidationError, match="at least 1 item"):
        DefaultClarificationForm.model_validate({"questions": []})
    with pytest.raises(ValidationError, match="at least one question"):
        _MissingMinimumForm.model_validate({"questions": []})


def test_host_schema_owns_question_count_bounds() -> None:
    bounded_schema = _BoundedForm.model_json_schema(by_alias=True)["properties"][
        "questions"
    ]
    assert bounded_schema["minItems"] == 2
    assert bounded_schema["maxItems"] == 5
    TinkerFin().with_namespace("test").with_plan(clarification_schema=_BoundedForm)

    def payload(count: int) -> dict[str, list[dict[str, object]]]:
        return {
            "questions": [
                {
                    "id": f"question-{index}",
                    "answer_type": "single_choice",
                    "prompt": "Choose",
                    "required": True,
                    "options": [
                        {
                            "id": f"option-{index}",
                            "label": f"Option {index}",
                            "attributes": {"priority": index},
                        }
                    ],
                }
                for index in range(count)
            ]
        }

    assert len(_BoundedForm.model_validate(payload(2)).questions) == 2
    assert len(_BoundedForm.model_validate(payload(5)).questions) == 5
    with pytest.raises(ValidationError, match="at least 2 items"):
        _BoundedForm.model_validate(payload(1))
    with pytest.raises(ValidationError, match="at most 5 items"):
        _BoundedForm.model_validate(payload(6))

    TinkerFin().with_namespace("test").with_plan(clarification_schema=_ExactFourForm)
    assert len(_ExactFourForm.model_validate(payload(4)).questions) == 4
    with pytest.raises(ValidationError):
        _ExactFourForm.model_validate(payload(3))
    with pytest.raises(ValidationError):
        _ExactFourForm.model_validate(payload(5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            DefaultClarificationForm,
            "requires at least 1 question and sets no maximum",
        ),
        (_BoundedForm, "requires between 2 and 5 questions, inclusive"),
        (_ExactFourForm, "requires exactly 4 questions"),
    ],
)
async def test_planner_prompt_derives_question_count_from_the_bound_schema(
    schema: type[ClarificationFormBase],
    expected: str,
) -> None:
    model = _FakeModel(responses=[_planner()])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True, clarification_schema=schema)
        .build(model=model, tools=[])
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="dynamic-count-message")]},
        run_id="dynamic-count",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )

    system_message = next(
        message
        for message in model.model_inputs[0]
        if isinstance(message, SystemMessage)
    )
    assert expected in str(system_message.content)


@pytest.mark.asyncio
async def test_planner_prompt_lists_only_reachable_answer_types() -> None:
    model = _FakeModel(responses=[_planner()])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True, clarification_schema=_TextOnlyForm)
        .build(model=model, tools=[])
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="text-only-message")]},
        run_id="text-only",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )

    system_message = next(
        message
        for message in model.model_inputs[0]
        if isinstance(message, SystemMessage)
    )
    prompt = str(system_message.content)
    assert "- text:" in prompt
    assert "- single_choice:" not in prompt
    assert "- multiple_choice:" not in prompt
    assert "- date:" not in prompt


def test_plain_definition_rejects_plan_runs() -> None:
    definition = (
        TinkerFin()
        .with_namespace("test")
        .build(model=_FakeModel(responses=[AIMessage(content="done")]), tools=[])
    )
    with pytest.raises(PlanModeConfigurationError, match="Plan-capable"):
        definition.open_run(
            thread_id="plan-thread",
            run_id="plain-plan",
            mode="plan",
            input={"messages": []},
        )


@pytest.mark.asyncio
async def test_plan_capable_default_is_native_and_needs_no_checkpointer() -> None:
    model = _FakeModel(responses=[AIMessage(content="done", id="native-result")])
    definition = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Run", id="default-message")]},
        run_id="default-no-saver",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )

    assert all("tinkerfin_plan" not in state for state in _root_values(parts))
    task_names = {
        cast(Mapping[str, object], part["data"]).get("name")
        for part in parts
        if part["type"] == "tasks"
    }
    assert "initialize_plan" not in task_names
    assert "create_plan" not in task_names
    assert all(not part["ns"] for part in parts)


@pytest.mark.asyncio
async def test_new_default_run_with_a_saver_does_not_build_the_planning_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_planning_build(_runtime: object) -> PlanningWorkflowGraph[Any]:
        raise AssertionError("a new default Run must not build the Planning graph")

    monkeypatch.setattr(
        plan_runtime_module.PlanCapableGraphRuntime,
        "_planning_graph",
        reject_planning_build,
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[AIMessage(content="done")]),
            tools=[],
        )
    )

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Run", id="default-with-saver")]},
        run_id="default-with-saver",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )

    assert all("tinkerfin_plan" not in state for state in _root_values(parts))


@pytest.mark.asyncio
async def test_plan_run_requires_the_definition_checkpointer_only_when_selected() -> (
    None
):
    definition = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=_FakeModel(responses=[_planner()]), tools=[])
    )
    with pytest.raises(PlanModeConfigurationError, match="BaseCheckpointSaver"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="plan-message")]},
            run_id="plan-no-saver",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


def test_plan_build_requires_an_explicit_agent_model() -> None:
    from test_agent_construction import invalid_call

    builder = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
    )
    with pytest.raises(TypeError, match="model"):
        invalid_call(builder.build, model=None, tools=[])


@pytest.mark.asyncio
async def test_default_preserves_coordinator_and_composed_state() -> None:
    coordinated: list[RunIdentity] = []

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        coordinated.append(identity)
        yield

    class ComposedState(_GlobalState, _DefinitionState):
        pass

    definition = (
        TinkerFin(run_coordinator=coordinate)
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[AIMessage(content="done")]),
            tools=[],
            state_schema=ComposedState,
        )
    )
    parts = await _parts(
        definition,
        {
            "messages": [HumanMessage(content="Run")],
            "global_marker": "global",
            "definition_marker": "definition",
        },
        run_id="composed-default",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )
    assert coordinated == [_identity("composed-default")]
    final = _root_values(parts)[-1]
    assert final["global_marker"] == "global"
    assert final["definition_marker"] == "definition"


@pytest.mark.asyncio
async def test_default_todos_use_the_native_root_without_correlation_extensions() -> (
    None
):
    model = _FakeModel(responses=[_todo_call(), AIMessage(content="done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
            middleware=(TodoListMiddleware(),),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Run todos")]},
        run_id="native-todos",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )
    tool_messages = [
        cast(tuple[BaseMessage, object], part["data"])[0]
        for part in parts
        if part["type"] == "messages" and part["ns"] == ()
    ]
    todo_result = next(
        message
        for message in tool_messages
        if isinstance(message, ToolMessage) and message.tool_call_id == "todo-Execute"
    )
    assert "tinkerfinToolResult" not in todo_result.additional_kwargs
    assert _root_values(parts)[-1]["todos"] == [
        {"content": "Execute", "status": "in_progress"}
    ]


@pytest.mark.asyncio
async def test_planner_creates_review_without_a_deep_agent_parent_graph() -> None:
    saver = InMemorySaver()
    definition = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .with_plan(enabled=True, default_mode="plan")
        .build(model=_FakeModel(responses=[_planner()]), tools=[])
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="request-1")]},
        run_id="review",
        config={"configurable": {"thread_id": "plan-thread"}},
    )
    interrupt = _root_interrupts(parts)[0]
    plan = _structured_plan_state(_root_values(parts)[-1]["tinkerfin_plan"])
    assert interrupt.value["kind"] == "tinkerfin:plan_review"
    review = interrupt.value["metadata"]["review"]
    assert "schema" not in review
    assert review["draft"]["revision"] == 1
    assert set(review["draft"]["contentSchema"]) == {"fingerprint", "mediaType"}
    assert review["draft"]["content"]["goal"] == "Implement feature"
    assert plan.status is PlanStatus.AWAITING_REVIEW
    assert plan.revision == 1
    assert plan.request_message_id == "request-1"
    task_names = {
        cast(Mapping[str, object], part["data"]).get("name")
        for part in parts
        if part["type"] == "tasks"
    }
    assert "execute_deep_agent" not in task_names
    namespaces = {
        str(checkpoint.config.get("configurable", {}).get("checkpoint_ns", ""))
        for checkpoint in saver.list({"configurable": {"thread_id": "plan-thread"}})
    }
    assert not any(
        namespace.startswith("execute_deep_agent:") for namespace in namespaces
    )


@pytest.mark.asyncio
async def test_markdown_plan_review_edit_and_handoff_preserve_exact_text() -> None:
    initial = "  # Release Plan\r\n\r\n```sh\r\necho ready\r\n```\r\n"
    edited = f"{initial}\r\n- Browser regression\r\n"
    model = _FakeModel(
        responses=[
            _markdown_planner(initial),
            _accept_edit(),
            AIMessage(content="native done"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(
            enabled=True,
            content_schema=MarkdownPlanContent,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.EDIT,
                PlanReviewAction.RESPOND,
                PlanReviewAction.REJECT,
            ),
        )
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan release", id="markdown-request")]},
        run_id="markdown-1",
        config=config,
        mode="plan",
    )
    first_interrupt = _root_interrupts(first)[0]
    first_review = first_interrupt.value["metadata"]["review"]
    assert "schema" not in first_review
    assert first_review["draft"]["contentSchema"]["mediaType"] == "text/markdown"
    assert first_review["draft"]["content"]["markdown"] == initial

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "edit",
                "baseRevision": 1,
                "content": {"markdown": edited},
            }
        ),
        run_id="markdown-2",
        config=config,
        mode="plan",
    )
    second_interrupt = _root_interrupts(second)[0]
    second_review = second_interrupt.value["metadata"]["review"]
    assert second_review["draft"]["revision"] == 2
    assert second_review["draft"]["content"]["markdown"] == edited
    pending = _markdown_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert pending.draft is not None
    assert pending.draft.content.markdown == edited

    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 2}),
        run_id="markdown-3",
        config=config,
        mode="default",
    )
    final = _root_values(completed)[-1]
    plan = _markdown_plan_state(final["tinkerfin_plan"])
    assert plan.confirmed_plan is not None
    assert plan.confirmed_plan.content.markdown == edited
    execution_input = model.model_inputs[-1]
    original_user = next(
        message
        for message in execution_input
        if isinstance(message, HumanMessage) and message.id == "markdown-request"
    )
    assert original_user.content == "Plan release"
    handoff = next(
        message
        for message in execution_input
        if isinstance(message, SystemMessage)
        and "<tinkerfin-approved-plan" in str(message.content)
    )
    handoff_text = handoff.text
    assert 'content-type="text/markdown"' in handoff_text
    assert "schema=" not in handoff_text
    assert edited in handoff_text
    assert "\\r\\n" not in handoff_text


@pytest.mark.asyncio
async def test_custom_plan_schema_round_trips_through_review_state() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True, content_schema=_CustomPlanContent)
        .build(
            model=_FakeModel(responses=[_custom_plan_planner()]),
            tools=[],
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan release", id="custom-request")]},
        run_id="custom-plan",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    review = _root_interrupts(parts)[0].value["metadata"]["review"]
    assert set(review["draft"]["contentSchema"]) == {"fingerprint", "mediaType"}
    assert review["draft"]["content"] == {
        "summary": "Release safely",
        "checks": ["Targeted tests pass"],
    }
    state = PlanState[_CustomPlanContent].model_validate(
        _root_values(parts)[-1]["tinkerfin_plan"]
    )
    assert state.draft is not None
    assert state.draft.content.summary == "Release safely"


@pytest.mark.asyncio
async def test_planner_retries_one_provider_invalid_json_tool_call() -> None:
    model = _FakeModel(responses=[_invalid_planner_json(), _planner()])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="invalid-json-retry",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert _root_interrupts(parts)[0].value["kind"] == "tinkerfin:plan_review"
    assert len(model.model_inputs) == 2
    assert any(
        isinstance(message, HumanMessage)
        and "strict JSON without trailing commas" in str(message.content)
        for message in model.model_inputs[-1]
    )


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
@pytest.mark.parametrize(
    ("planned", "observed", "ending"),
    [
        (planned, observed, ending)
        for planned in (False, True)
        for observed in (False, True)
        for ending in (
            "cancel",
            "repeat_cancel",
            "close",
            "timeout",
            "provider",
            "observer",
        )
        if ending != "observer" or observed
    ],
)
async def test_runtime_settles_native_work_after_interruption(
    planned: bool,
    observed: bool,
    ending: str,
) -> None:
    model = _BlockingNativeModel(
        responses=[_planner(), AIMessage(content="unused")],
        block_first_call=not planned,
    )
    observer = _PlanObserver()
    healthy_observer = _PlanObserver()
    factory = TinkerFin(checkpointer=InMemorySaver()).with_namespace("test")
    if observed:
        factory = factory.with_observer(observer).with_observer(healthy_observer)
    if planned:
        factory = factory.with_plan(enabled=True)
    definition = factory.build(model=model, tools=[])
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}
    graph_input: InputAgentState | Command[object] = {
        "messages": [HumanMessage(content="Implement", id="request")]
    }
    if planned:
        await _parts(
            definition,
            graph_input,
            run_id="interruption-review",
            config=config,
            mode="plan",
        )
        graph_input = Command(resume={"type": "approve", "baseRevision": 1})
    stream = definition.open_run(
        thread_id=_identity("interruption-execution").thread_id,
        run_id=_identity("interruption-execution").run_id,
        input=graph_input,
        config=config,
        stream_mode=["messages", "tasks", "values"],
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(model.started.wait(), 5)
        if ending == "provider":
            model.release.set()
            with pytest.raises(RuntimeError, match="native provider failed"):
                await consumer
        elif ending == "observer":
            session = observer.sessions["interruption-execution"]
            session.failure.set_result(RuntimeError("observer delivery failed"))
            with pytest.raises(RunObservationError):
                await consumer
        elif ending == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(consumer, 0.01)
        else:
            if ending == "close":
                await stream.aclose()
            else:
                consumer.cancel()
                if ending == "repeat_cancel":
                    await asyncio.sleep(0)
                    consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        await stream.aclose()
        await stream.aclose()
        assert model.settled.is_set()
        if observed:
            assert observer.sessions["interruption-execution"].closed == 1
            terminals = [
                item
                for item in healthy_observer.sessions[
                    "interruption-execution"
                ].observations
                if isinstance(item, RunTerminalObservation)
            ]
            assert [item.outcome for item in terminals] == [
                "failed" if ending in {"provider", "observer"} else "cancelled"
            ]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [False, True])
async def test_plan_handoff_close_between_parts_is_scoped_and_idempotent(
    observed: bool,
) -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="native response")])
    observer = _PlanObserver()
    factory = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(observer)
        if observed
        else TinkerFin(checkpointer=InMemorySaver()).with_namespace("test")
    )
    definition = factory.with_plan(enabled=True).build(model=model, tools=[])
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="request")]},
        run_id="closing-review",
        config=config,
        mode="plan",
    )
    unopened = definition.open_run(
        thread_id=_identity("unopened-execution").thread_id,
        run_id=_identity("unopened-execution").run_id,
        input=Command(resume={"type": "approve", "baseRevision": 1}),
        config=config,
    )
    await unopened.aclose()
    await unopened.aclose()
    assert len(model.model_inputs) == 1
    assert "unopened-execution" not in observer.sessions
    stream = definition.open_run(
        thread_id=_identity("closing-execution").thread_id,
        run_id=_identity("closing-execution").run_id,
        input=Command(resume={"type": "approve", "baseRevision": 1}),
        config=config,
        stream_mode=["messages", "tasks", "values"],
    )
    try:
        async for part in stream:
            if part["type"] != "messages" or len(model.model_inputs) != 2:
                continue
            unrelated = _FakeModel(responses=[AIMessage(content="ordinary response")])
            ordinary = (
                TinkerFin(checkpointer=InMemorySaver())
                .with_namespace("test")
                .with_plan(enabled=True)
                .build(model=unrelated, tools=[])
            )
            ordinary_stream = ordinary.open_run(
                thread_id=_identity(
                    "unrelated-run", thread_id="unrelated-thread"
                ).thread_id,
                run_id=_identity("unrelated-run", thread_id="unrelated-thread").run_id,
                input={"messages": [HumanMessage(content="Ordinary", id="unrelated")]},
                config={"configurable": {"thread_id": "unrelated-thread"}},
            )
            async for _ordinary_part in ordinary_stream:
                pass
            assert all(
                "<tinkerfin-approved-plan" not in str(message.content)
                for message in unrelated.model_inputs[-1]
            )
            break
        else:
            pytest.fail("native model produced no message part")
    finally:
        await stream.aclose()
        await stream.aclose()
    if observed:
        session = observer.sessions["closing-execution"]
        assert session.closed == 1
        assert [
            item.outcome
            for item in session.observations
            if isinstance(item, RunTerminalObservation)
        ] == ["cancelled"]


@pytest.mark.asyncio
async def test_plan_handoff_preserves_user_content_blocks_exactly() -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    content: list[str | dict[Any, Any]] = [
        {"type": "text", "text": "Implement from blocks"},
        {"type": "text", "text": "Keep this shape"},
    ]
    request = HumanMessage(content=content, id="block-user")
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [request]},
        run_id="block-review",
        config=config,
        mode="plan",
    )
    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="block-execution",
        config=config,
        mode="default",
    )

    execution_user = next(
        message
        for message in model.model_inputs[-1]
        if isinstance(message, HumanMessage) and message.id == "block-user"
    )
    assert execution_user.content == content
    final_user = next(
        message
        for message in cast(
            Sequence[BaseMessage], _root_values(completed)[-1]["messages"]
        )
        if isinstance(message, HumanMessage) and message.id == "block-user"
    )
    assert final_user.content == content
    assert request.content == content


@pytest.mark.asyncio
@pytest.mark.redis_e2e
@pytest.mark.parametrize("observed", [False, True])
async def test_real_redis_plan_approval_executes_native_handoff(
    redis_checkpoint_url: str,
    observed: bool,
) -> None:
    token = uuid4().hex
    thread_id = f"tinkerfin-plan-handoff-{token}"
    checkpoint_prefix = f"tinkerfin:test:plan:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:plan:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    try:
        await saver.asetup()
        model = _InvocationModel(
            responses=[_planner(), AIMessage(content="native done")]
        )
        observer = _PlanObserver()
        factory = (
            TinkerFin(checkpointer=saver).with_namespace("test").with_observer(observer)
            if observed
            else TinkerFin(checkpointer=saver).with_namespace("test")
        )
        definition = factory.with_plan(enabled=True).build(model=model, tools=[])
        config = {"configurable": {"thread_id": thread_id}}
        review = await _agui_events(
            definition,
            {"messages": [HumanMessage(content="Implement", id="request-redis")]},
            run_id="redis-review",
            config=config,
            thread_id=thread_id,
            mode="plan",
        )
        binding = _plan_binding(
            _terminal(review),
            payload={"type": "approve", "baseRevision": 1},
        )
        completed = await _agui_events(
            definition,
            None,
            run_id="redis-execution",
            config=config,
            thread_id=thread_id,
            mode="default",
            resume=binding,
        )

        _assert_success(completed)
        assert len(model.model_inputs) == 2
        assert any(
            getattr(event, "delta", None) == "native done" for event in completed
        )
        execution_input = model.model_inputs[-1]
        assert any(
            isinstance(message, HumanMessage)
            and message.id == "request-redis"
            and message.content == "Implement"
            for message in execution_input
        )
        assert any(
            isinstance(message, SystemMessage)
            and "<tinkerfin-approved-plan" in str(message.content)
            for message in execution_input
        )
        serialized_events = "\n".join(
            event.model_dump_json(by_alias=True) for event in completed
        )
        assert "<tinkerfin-approved-plan" not in serialized_events
        assert (
            model.configs[-1].get("configurable", {}).get(RUN_ID_METADATA_KEY)
            == "redis-execution"
        )
        if observed:
            session = observer.sessions["redis-execution"]
            assert [
                item.phase
                for item in session.observations
                if isinstance(item, ModelCallObservation)
            ] == ["started", "completed"]
            assert session.closed == 1
    finally:
        try:
            await saver.adelete_thread(thread_id)
        finally:
            registry_keys = [
                key
                async for key in client.scan_iter(
                    match=f"write_keys_zset:{thread_id}:*",
                    count=100,
                )
            ]
            latest_keys = [
                key
                async for key in client.scan_iter(
                    match=f"{checkpoint_prefix}_latest:{thread_id}:*",
                    count=100,
                )
            ]
            if registry_keys or latest_keys:
                await client.unlink(*registry_keys, *latest_keys)
            for index_name in (checkpoint_prefix, write_prefix):
                try:
                    await client.execute_command("FT.DROPINDEX", index_name, "DD")
                except ResponseError:
                    pass
            await client.aclose()


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
    checkpoints: list[AgUiResumeCheckpoint] = []

    async def checkpointed(value: AgUiResumeCheckpoint) -> None:
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
async def test_plan_capable_runtime_routes_plan_shaped_generic_resume_to_native() -> (
    None
):
    resumed_payloads: list[object] = []
    envelope = RuntimeInterruptEnvelope(
        kind="vendor:approval",
        message="Review vendor action",
        response_schema={
            "type": "object",
            "properties": {
                "type": {"const": "approve"},
                "baseRevision": {"type": "integer"},
                "vendor": {"type": "string"},
            },
            "required": ["type", "baseRevision", "vendor"],
            "additionalProperties": False,
        },
        metadata={"source": "generic-native-fixture"},
    )

    class _GenericNativeInterrupt(AgentMiddleware):
        def after_model(
            self,
            state: AgentState[Any],
            runtime: Runtime[None],
        ) -> dict[str, object] | None:
            del state, runtime
            resumed_payloads.append(
                interrupt(
                    envelope.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                )
            )
            return None

    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[AIMessage(content="native done")]),
            tools=[],
            middleware=(_GenericNativeInterrupt(),),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Run native", id="message")]},
        run_id="generic-review",
        config=config,
        mode="default",
    )
    outcome = _interrupt_outcome(review)
    public_interrupt = outcome.interrupts[0]
    payload = {"type": "approve", "baseRevision": 999, "vendor": "native"}
    entry = _resume_entry(public_interrupt.id, payload)
    translation = ResumeMapper().map_agui(
        entries=(entry,),
        interrupts=(public_interrupt,),
    )
    assert translation.kind == "runtime"
    identity = _identity("generic-resume")
    binding = AgUiResumeRequest(entries=(entry,))
    runtime = cast(Any, definition)
    resumed = [
        event
        async for event in runtime.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            mode="plan",
            resume=binding,
            config=config,
        )
    ]

    _assert_success(resumed)
    assert resumed_payloads == [payload]


@pytest.mark.asyncio
async def test_parent_run_id_branches_from_completed_plan_lineage() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="planned execution"),
            AIMessage(content="head continuation"),
            AIMessage(content="branched continuation"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Plan", id="plan-user")]},
        run_id="plan-lineage-review",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(review),
        payload={"type": "approve", "baseRevision": 1},
    )
    await _agui_events(
        definition,
        None,
        run_id="plan-lineage-execution",
        config=config,
        mode="default",
        resume=binding,
    )
    await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Head", id="head-user")]},
        run_id="plan-lineage-head",
        config=config,
        mode="default",
    )
    branched = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Branch", id="branch-user")]},
        run_id="plan-lineage-branch",
        parent_run_id="plan-lineage-execution",
        config=config,
        mode="default",
    )

    _assert_success(branched)
    latest_input = model.model_inputs[-1]
    human_ids = {
        message.id for message in latest_input if isinstance(message, HumanMessage)
    }
    assert "branch-user" in human_ids
    assert "head-user" not in human_ids


@pytest.mark.asyncio
async def test_native_conversation_continues_after_plan_handoff() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="first done", id="first-assistant"),
            AIMessage(content="second done", id="second-assistant"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="First", id="first-user")]},
        run_id="continuity-1",
        config=config,
        mode="plan",
    )
    await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="continuity-2",
        config=config,
        mode="default",
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Second", id="second-user")]},
        run_id="continuity-3",
        config=config,
        mode="default",
    )
    final_input = model.model_inputs[-1]
    assert any(
        isinstance(message, AIMessage)
        and message.id == "first-assistant"
        and message.content == "first done"
        for message in final_input
    )
    assert any(
        isinstance(message, HumanMessage) and message.id == "second-user"
        for message in final_input
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_concurrent_plan_handoffs_isolate_transient_system_context(
    managed: bool,
) -> None:
    model = _FakeModel(
        responses=[
            _planner(goal="Execute alpha"),
            _planner(goal="Execute beta"),
            AIMessage(content="alpha done"),
            AIMessage(content="beta done"),
        ]
    )
    factory = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(_PlanObserver())
        if managed
        else TinkerFin(checkpointer=InMemorySaver()).with_namespace("test")
    )
    definition = factory.with_plan(enabled=True).build(model=model, tools=[])
    graph = None if managed else await create_graph(definition, mode="plan")

    async def collect(
        graph_input: InputAgentState | Command[object] | None,
        *,
        thread_id: str,
    ) -> list[Mapping[str, object]]:
        if managed:
            source = definition.open_run(
                thread_id=thread_id, run_id=uuid4().hex, mode="plan", input=graph_input
            )
        else:
            assert graph is not None
            source = graph.astream(
                graph_input,
                config={"configurable": {"thread_id": thread_id}},
                stream_mode=["messages", "tasks", "values"],
            )
        return [part async for part in source]

    await collect(
        {"messages": [HumanMessage(content="Alpha request", id="alpha-user")]},
        thread_id="alpha-thread",
    )
    await collect(
        {"messages": [HumanMessage(content="Beta request", id="beta-user")]},
        thread_id="beta-thread",
    )
    executions = await asyncio.gather(
        collect(
            Command(resume={"type": "approve", "baseRevision": 1}),
            thread_id="alpha-thread",
        ),
        collect(
            Command(resume={"type": "approve", "baseRevision": 1}),
            thread_id="beta-thread",
        ),
    )

    model_inputs = [
        messages
        for messages in model.model_inputs
        if any(
            isinstance(message, SystemMessage)
            and "<tinkerfin-approved-plan" in message.text
            for message in messages
        )
    ]
    assert len(model_inputs) == 2
    by_user_id = {
        cast(str, user.id): (user, system)
        for messages in model_inputs
        for user in messages
        if isinstance(user, HumanMessage) and user.id in {"alpha-user", "beta-user"}
        for system in messages
        if isinstance(system, SystemMessage)
        and "<tinkerfin-approved-plan" in system.text
    }
    alpha_user, alpha_system = by_user_id["alpha-user"]
    beta_user, beta_system = by_user_id["beta-user"]
    assert alpha_user.content == "Alpha request"
    assert beta_user.content == "Beta request"
    assert "Execute alpha" in alpha_system.text
    assert "Execute beta" not in alpha_system.text
    assert "Execute beta" in beta_system.text
    assert "Execute alpha" not in beta_system.text
    assert "<tinkerfin-approved-plan" not in repr(executions)


@pytest.mark.asyncio
async def test_plan_default_plan_with_todos_regresses_historical_correlation_failure() -> (
    None
):
    model = _FakeModel(
        responses=[
            _planner(),
            _todo_call(content="First Plan"),
            AIMessage(content="first done"),
            AIMessage(content="default done"),
            _planner(suffix=" second"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
            middleware=(TodoListMiddleware(),),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="First", id="first-message")]},
        run_id="first-review",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(review),
        payload={"type": "approve", "baseRevision": 1},
    )
    executed = await _agui_events(
        definition,
        None,
        run_id="first-execution",
        config=config,
        mode="default",
        resume=binding,
    )
    default = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Default", id="default-message")]},
        run_id="default-round",
        config=config,
        mode="default",
    )
    second = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Second", id="second-message")]},
        run_id="second-review",
        config=config,
        mode="plan",
    )
    executed_outcome = _terminal(executed).outcome
    default_outcome = _terminal(default).outcome
    second_outcome = _terminal(second).outcome
    assert executed_outcome is not None and executed_outcome.type == "success"
    assert default_outcome is not None and default_outcome.type == "success"
    assert second_outcome is not None and second_outcome.type == "interrupt"
    assert second_outcome.interrupts[0].reason == "tinkerfin:plan_review"
    assert not any(isinstance(event, RunErrorEvent) for event in second)


@pytest.mark.asyncio
async def test_multiple_clarification_rounds_do_not_increment_revision() -> None:
    model = _FakeModel(
        responses=[
            _planner_clarification(
                question_id="environment",
                options=[{"id": "staging", "label": "Staging"}],
            ),
            _planner_clarification(question_id="region", prompt="Which region?"),
            _planner(),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="deploy-message")]},
        run_id="clarify-1",
        config=config,
        mode="plan",
    )
    assert (
        _structured_plan_state(_root_values(first)[-1]["tinkerfin_plan"]).revision == 0
    )
    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "environment": {
                        "status": "answered",
                        "answerType": "single_choice",
                        "optionId": "staging",
                    }
                },
            }
        ),
        run_id="clarify-2",
        config=config,
        mode="plan",
    )
    plan2 = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert plan2.revision == 0
    assert len(plan2.clarification_history) == 1
    third = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "region": {
                        "status": "answered",
                        "answerType": "text",
                        "answer": "EU",
                    }
                },
            }
        ),
        run_id="clarify-3",
        config=config,
        mode="plan",
    )
    plan3 = _structured_plan_state(_root_values(third)[-1]["tinkerfin_plan"])
    assert plan3.revision == 1
    assert len(plan3.clarification_history) == 2
    assert _root_interrupts(third)[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.asyncio
async def test_agui_clarification_resume_uses_the_exact_planning_snapshot() -> None:
    model = _FakeModel(
        responses=[
            _planner_clarification(
                question_id="environment",
                options=[{"id": "staging", "label": "Staging"}],
            ),
            _planner(),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="agui-clarification")]},
        run_id="agui-clarification-1",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(first),
        payload={
            "type": "respond",
            "answers": {
                "environment": {
                    "status": "answered",
                    "answerType": "single_choice",
                    "optionId": "staging",
                }
            },
        },
    )

    resumed = await _agui_events(
        definition,
        None,
        run_id="agui-clarification-2",
        config=config,
        mode="plan",
        resume=binding,
    )

    outcome = _interrupt_outcome(resumed)
    assert outcome.interrupts[0].reason == "tinkerfin:plan_review"


@pytest.mark.asyncio
async def test_completed_resume_uses_the_exact_planning_snapshot() -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="Draft rejected")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="agui-reject")]},
        run_id="agui-reject-1",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(first),
        payload={"type": "reject", "baseRevision": 1},
    )
    resolved = await _agui_events(
        definition,
        None,
        run_id="agui-reject-2",
        config=config,
        mode="plan",
        resume=binding,
    )
    _assert_success(resolved)

    calls = len(model.model_inputs)
    replayed = await _agui_events(
        definition,
        None,
        run_id="agui-reject-2",
        config=config,
        mode="default",
        resume=binding,
    )
    _assert_success(replayed)
    snapshots = [event for event in replayed if isinstance(event, StateSnapshotEvent)]
    assert snapshots
    restored = _structured_plan_state(snapshots[-1].snapshot["tinkerfin_plan"])
    assert restored.status is PlanStatus.AWAITING_INPUT
    assert len(model.model_inputs) == calls


@pytest.mark.asyncio
async def test_four_question_clarification_round_trips_in_one_batch() -> None:
    model = _FakeModel(responses=[_planner_clarification_batch(), _planner()])
    definition = (
        TinkerFin(
            checkpointer=InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False))
        )
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="four-question-message")]},
        run_id="four-question-1",
        config=config,
        mode="plan",
    )

    assert {part["type"] for part in first} == {"messages", "tasks", "values"}
    interrupts = _root_interrupts(first)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "tinkerfin:plan_clarification"
    metadata = interrupts[0].value["metadata"]
    form = metadata["clarification"]["form"]
    assert [question["id"] for question in form["questions"]] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]
    pending = _structured_plan_state(_root_values(first)[-1]["tinkerfin_plan"])
    assert pending.status is PlanStatus.AWAITING_CLARIFICATION
    assert pending.pending_clarification is not None
    pending_questions = pending.pending_clarification.form["questions"]
    assert isinstance(pending_questions, list)
    assert len(pending_questions) == 4

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "question-0": {
                        "status": "answered",
                        "answerType": "single_choice",
                        "optionId": "option-0",
                    },
                    "question-1": {
                        "status": "answered",
                        "answerType": "text",
                        "answer": "Custom 1",
                    },
                    "question-2": {
                        "status": "answered",
                        "answerType": "single_choice",
                        "optionId": "option-2",
                    },
                    "question-3": {
                        "status": "answered",
                        "answerType": "text",
                        "answer": "Custom 3",
                    },
                },
            }
        ),
        run_id="four-question-2",
        config=config,
        mode="plan",
    )
    reviewed = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert reviewed.status is PlanStatus.AWAITING_REVIEW
    assert reviewed.revision == 1
    assert reviewed.pending_clarification is None
    assert len(reviewed.clarification_history) == 1
    exchange = reviewed.clarification_history[0]
    assert tuple(answer.question_id for answer in exchange.answers) == (
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    )
    assert tuple(answer.value for answer in exchange.answers) == (
        {"option": {"id": "option-0", "label": "Option 0"}},
        {"answer": "Custom 1"},
        {"option": {"id": "option-2", "label": "Option 2"}},
        {"answer": "Custom 3"},
    )
    review_interrupts = _root_interrupts(second)
    assert len(review_interrupts) == 1
    assert review_interrupts[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.asyncio
async def test_agui_preserves_every_question_in_a_large_clarification_form() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[_planner_clarification_batch()]),
            tools=[],
        )
    )
    events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="agui-four-message")]},
        run_id="agui-four",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )

    assert not any(isinstance(event, RunErrorEvent) for event in events)
    terminal = _terminal(events)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    assert len(terminal.outcome.interrupts) == 1
    interrupt = terminal.outcome.interrupts[0]
    assert interrupt.reason == "tinkerfin:plan_clarification"
    metadata = cast(dict[str, Any], interrupt.metadata)
    runtime_interrupt = cast(dict[str, Any], metadata["runtimeInterrupt"])
    envelope = cast(dict[str, Any], runtime_interrupt["envelope"])
    public_metadata = cast(dict[str, Any], envelope["metadata"])
    clarification = cast(dict[str, Any], public_metadata["clarification"])
    assert "schema" not in clarification
    form = cast(dict[str, Any], clarification["form"])
    assert "schemaVersion" not in form
    questions = cast(list[dict[str, Any]], form["questions"])
    assert [question["id"] for question in questions] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]
    assert [question["required"] for question in questions] == [True] * 4
    response_schema = cast(dict[str, Any], interrupt.response_schema)
    answers_schema = response_schema["properties"]["answers"]
    assert answers_schema["required"] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]

    types = [event.type.value for event in events]
    state_index = len(types) - 1 - types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(types) - 1 - types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(types) - 1 - types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index


@pytest.mark.asyncio
async def test_all_optional_clarification_questions_can_be_explicitly_skipped() -> None:
    model = _FakeModel(
        responses=[
            _planner_clarification_batch(2, required=False),
            _planner(goal="Proceed with explicit assumptions"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}

    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="optional-message")]},
        run_id="optional-1",
        config=config,
        mode="plan",
    )
    interrupt = _root_interrupts(first)[0]
    metadata = interrupt.value["metadata"]
    assert "schema" not in metadata["clarification"]
    form = metadata["clarification"]["form"]
    assert "schemaVersion" not in form
    assert [question["required"] for question in form["questions"]] == [False, False]

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "question-0": {"status": "skipped"},
                    "question-1": {"status": "skipped"},
                },
            }
        ),
        run_id="optional-2",
        config=config,
        mode="plan",
    )

    reviewed = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert reviewed.status is PlanStatus.AWAITING_REVIEW
    answers = reviewed.clarification_history[0].answers
    assert tuple(answer.question_id for answer in answers) == (
        "question-0",
        "question-1",
    )
    assert all(answer.skipped for answer in answers)
    assert all(answer.value is None for answer in answers)
    planner_context = "\n".join(
        str(message.content)
        for message in model.model_inputs[1]
        if isinstance(message, HumanMessage)
    )
    assert '"skipped": true' in planner_context


@pytest.mark.asyncio
async def test_required_clarification_question_cannot_be_skipped() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(
                responses=[_planner_clarification(required=True), _planner()]
            ),
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="required-message")]},
        run_id="required-1",
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
                    "answers": {"target": {"status": "skipped"}},
                }
            ),
            run_id="required-2",
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
                        "answerType": "text",
                        "answer": "Target A",
                    }
                },
            }
        ),
        run_id="required-3",
        config=config,
        mode="plan",
    )
    assert (
        _structured_plan_state(_root_values(retried)[-1]["tinkerfin_plan"]).status
        is PlanStatus.AWAITING_REVIEW
    )


@pytest.mark.asyncio
async def test_custom_clarification_preserves_attributes_and_trusted_option_label() -> (
    None
):
    model = _FakeModel(responses=[_custom_planner_clarification(), _planner()])
    definition = (
        TinkerFin(
            checkpointer=InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False))
        )
        .with_namespace("test")
        .with_plan(enabled=True, clarification_schema=_CustomForm)
        .build(
            model=model,
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="custom-message")]},
        run_id="custom-1",
        config=config,
        mode="plan",
    )
    interrupt = _root_interrupts(first)[0]
    metadata = interrupt.value["metadata"]
    form = metadata["clarification"]["form"]
    assert form["questions"][0]["attributes"] == {"category": "deployment"}
    assert form["questions"][0]["options"][0]["attributes"] == {"priority": 1}
    assert "schemaFingerprint" not in metadata["clarification"]
    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "custom-target": {
                        "status": "answered",
                        "answerType": "single_choice",
                        "optionId": "target-a",
                    }
                },
            }
        ),
        run_id="custom-2",
        config=config,
        mode="plan",
    )
    answer = (
        _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
        .clarification_history[0]
        .answers[0]
    )
    assert answer.value == {"option": {"id": "target-a", "label": "Target A"}}


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
async def test_clarification_fails_before_checkpoint_for_non_json_attributes() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True, clarification_schema=_OpaqueForm)
        .build(
            model=_FakeModel(responses=[_opaque_planner_clarification()]),
            tools=[],
        )
    )
    with pytest.raises(PlanStructuredOutputError, match="JSON checkpoint round-trip"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="message")]},
            run_id="opaque",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


@pytest.mark.asyncio
async def test_pending_clarification_rejects_schema_drift() -> None:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "plan-thread"}}
    original = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[_planner_clarification()]),
            tools=[],
        )
    )
    await _parts(
        original,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="schema-1",
        config=config,
        mode="plan",
    )
    changed = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .with_plan(enabled=True, clarification_schema=_CustomForm)
        .build(
            model=_FakeModel(responses=[AIMessage(content="unused")]),
            tools=[],
        )
    )
    with pytest.raises(PlanModeConfigurationError, match="does not match"):
        await _parts(
            changed,
            Command(
                resume={
                    "type": "respond",
                    "answers": {
                        "target": {
                            "status": "answered",
                            "answerType": "text",
                            "answer": "A",
                        }
                    },
                }
            ),
            run_id="schema-2",
            config=config,
            mode="plan",
        )


@pytest.mark.asyncio
async def test_edit_is_authoritative_and_reenters_sufficiency_checks() -> None:
    model = _FakeModel(responses=[_planner(), _planner_clarification(), _accept_edit()])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(
            enabled=True,
            allowed_review_actions=(PlanReviewAction.EDIT, PlanReviewAction.RESPOND),
        )
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="edit-1",
        config=config,
        mode="plan",
    )
    edited = {
        "goal": "Edited goal needs a target",
        "assumptions": [],
        "steps": [
            {
                "id": "edited-step",
                "title": "Edited",
                "description": "Wait for the target",
                "verification": ["Target is explicit"],
            }
        ],
        "acceptanceCriteria": ["Edited result works"],
    }
    clarification = await _parts(
        definition,
        Command(
            resume={
                "type": "edit",
                "baseRevision": 1,
                "content": edited,
            }
        ),
        run_id="edit-2",
        config=config,
        mode="plan",
    )
    pending = _structured_plan_state(_root_values(clarification)[-1]["tinkerfin_plan"])
    assert pending.status is PlanStatus.AWAITING_CLARIFICATION
    assert pending.revision == 1
    assert pending.pending_edit is not None
    assert pending.pending_edit.goal == edited["goal"]
    reviewed = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": {
                    "target": {
                        "status": "answered",
                        "answerType": "text",
                        "answer": "Target A",
                    }
                },
            }
        ),
        run_id="edit-3",
        config=config,
        mode="plan",
    )
    final = _structured_plan_state(_root_values(reviewed)[-1]["tinkerfin_plan"])
    assert final.revision == 2
    assert final.draft is not None and final.draft.content.goal == edited["goal"]
    assert final.draft.content.steps[0].id == "edited-step"
    assert final.pending_edit is None


@pytest.mark.asyncio
async def test_natural_language_feedback_creates_one_revised_draft() -> None:
    model = _FakeModel(responses=[_planner(), _planner(suffix=" revised")])
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
        run_id="feedback-1",
        config=config,
        mode="plan",
    )
    revised = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "baseRevision": 1,
                "message": "Add regression coverage",
            }
        ),
        run_id="feedback-2",
        config=config,
        mode="plan",
    )
    plan = _structured_plan_state(_root_values(revised)[-1]["tinkerfin_plan"])
    assert plan.revision == 2
    assert plan.feedback == ("Add regression coverage",)
    assert plan.draft is not None and plan.draft.content.goal.endswith("revised")


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
async def test_review_cancel_keeps_plan_mode_and_replies_once() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="The current draft is cancelled; we can keep planning."),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(
            enabled=True,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.REJECT,
                PlanReviewAction.CANCEL,
            ),
        )
        .build(model=model, tools=[])
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="cancel-message")]},
        run_id="cancel-1",
        config=config,
        mode="plan",
    )
    schema = _root_interrupts(review)[0].value["responseSchema"]
    assert set(schema["discriminator"]["mapping"]) == {
        "approve",
        "reject",
        "cancel",
    }

    cancelled = await _parts(
        definition,
        Command(resume={"type": "cancel", "baseRevision": 1}),
        run_id="cancel-2",
        config=config,
        mode="plan",
    )
    plan = _structured_plan_state(_root_values(cancelled)[-1]["tinkerfin_plan"])
    assert plan.status is PlanStatus.AWAITING_INPUT
    assert plan.effective_mode == "plan"
    assert plan.review_action is PlanReviewAction.CANCEL
    assert plan.review_reason is None
    root_messages = [
        cast(tuple[BaseMessage, object], part["data"])[0]
        for part in cancelled
        if part["type"] == "messages" and part["ns"] == ()
    ]
    assert (
        sum(
            isinstance(message, AIMessage)
            and message.content
            == "The current draft is cancelled; we can keep planning."
            for message in root_messages
        )
        == 1
    )


@pytest.mark.asyncio
async def test_agui_plan_cancel_resolves_the_card_and_streams_one_reply() -> None:
    reply = "The draft is cancelled, and Planning remains active."
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(
            enabled=True,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.REJECT,
                PlanReviewAction.CANCEL,
            ),
        )
        .build(
            model=_FakeModel(responses=[_planner(), AIMessage(content=reply)]),
            tools=[],
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement", id="agui-cancel-message")]},
        run_id="agui-cancel-1",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(review),
        payload={"type": "cancel", "baseRevision": 1},
    )

    cancelled = await _agui_events(
        definition,
        None,
        run_id="agui-cancel-2",
        config=config,
        mode="plan",
        resume=binding,
    )

    _assert_success(cancelled)
    assert (
        "".join(
            event.delta
            for event in cancelled
            if isinstance(event, TextMessageContentEvent)
        )
        == reply
    )
    snapshots = [event for event in cancelled if isinstance(event, StateSnapshotEvent)]
    assert snapshots
    initial_plan = _structured_plan_state(snapshots[-1].snapshot["tinkerfin_plan"])
    assert initial_plan.status is PlanStatus.AWAITING_REVIEW
    operations = [
        operation
        for event in cancelled
        if isinstance(event, StateDeltaEvent)
        for operation in event.delta
    ]
    assert {
        (operation.get("path"), operation.get("value")) for operation in operations
    }.issuperset(
        {
            ("/tinkerfin_plan/status", PlanStatus.AWAITING_INPUT.value),
            ("/tinkerfin_plan/reviewAction", PlanReviewAction.CANCEL.value),
        }
    )


@pytest.mark.asyncio
async def test_plan_rejects_non_sync_durability() -> None:
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
        )
    )
    with pytest.raises(PlanModeConfigurationError, match="durability='sync'"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="message")]},
            run_id="async-plan",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
            durability="async",
        )


@tool
def approved_tool(value: str) -> str:
    """Return a value after human review."""

    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [False, True])
async def test_plan_handoff_tool_interrupt_resumes_as_native_default(
    observed: bool,
) -> None:
    model = _InvocationModel(
        responses=[
            _planner(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "ok"},
                        "id": "approved-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="tool complete"),
        ]
    )
    observer = _PlanObserver()
    factory = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_observer(observer)
        if observed
        else TinkerFin(checkpointer=InMemorySaver()).with_namespace("test")
    )
    definition = factory.with_plan(enabled=True).build(
        model=model,
        tools=[approved_tool],
        interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="tool-review",
        config=config,
        mode="plan",
    )
    plan_binding = _plan_binding(
        _terminal(review),
        payload={"type": "approve", "baseRevision": 1},
    )
    interrupted = await _agui_events(
        definition,
        None,
        run_id="tool-execution",
        config=config,
        mode="default",
        resume=plan_binding,
    )
    tool_terminal = _terminal(interrupted)
    tool_outcome = tool_terminal.outcome
    assert tool_outcome is not None and tool_outcome.type == "interrupt"
    tool_interrupt = tool_outcome.interrupts[0]
    assert tool_interrupt.reason == "tool_call"
    assert tool_interrupt.tool_call_id is not None
    kind, namespace, raw_id = ScopedIdCodec().decode(tool_interrupt.tool_call_id)
    assert (kind, namespace, raw_id) == ("tool", (), "approved-call")
    resume_binding = AgUiResumeRequest(
        entries=(_resume_entry(tool_interrupt.id, {"type": "approve"}),)
    )
    completed = await _agui_events(
        definition,
        None,
        run_id="tool-resume",
        config=config,
        mode="default",
        resume=resume_binding,
    )
    completed_outcome = _terminal(completed).outcome
    assert completed_outcome is not None and completed_outcome.type == "success"
    completed_snapshots = [
        event for event in completed if isinstance(event, StateSnapshotEvent)
    ]
    assert completed_snapshots
    resumed_plan = _structured_plan_state(
        completed_snapshots[-1].snapshot["tinkerfin_plan"]
    )
    assert resumed_plan.status is PlanStatus.APPROVED
    assert resumed_plan.effective_mode == "default"
    assert resumed_plan.handoff is not None
    assert resumed_plan.handoff.phase is PlanHandoffPhase.ACCEPTED
    assert any(
        operation.get("path", "").endswith("/handoff/phase")
        and operation.get("value") == PlanHandoffPhase.COMPLETED.value
        for event in completed
        if isinstance(event, StateDeltaEvent)
        for operation in event.delta
    )
    assert [
        invocation.get("configurable", {}).get(RUN_ID_METADATA_KEY)
        for invocation in model.configs
    ] == ["tool-review", "tool-execution", "tool-resume"]
    for execution_input in model.model_inputs[1:]:
        assert any(
            isinstance(message, SystemMessage)
            and "<tinkerfin-approved-plan" in message.text
            for message in execution_input
        )
    if observed:
        for run_id in ("tool-review", "tool-execution", "tool-resume"):
            session = observer.sessions[run_id]
            calls = [
                item
                for item in session.observations
                if isinstance(item, ModelCallObservation)
            ]
            assert [item.phase for item in calls] == ["started", "completed"]
            assert calls[0].call_id == calls[1].call_id
            assert session.closed == 1
        tools = [
            item
            for item in observer.sessions["tool-resume"].observations
            if isinstance(item, ToolExecutionObservation)
        ]
        assert [item.phase for item in tools] == ["started", "completed"]
        assert tools[0].tool_call_id == tools[1].tool_call_id == "approved-call"


@pytest.mark.asyncio
async def test_plan_enabled_default_subagent_resume_uses_only_the_native_head(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Run the reviewed child tool",
                            "subagent_type": "general-purpose",
                        },
                        "id": "parent-task-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "child"},
                        "id": "child-approved-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="root done"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[approved_tool],
            interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
        )
    )
    thread_id = "plan-enabled-subagent-resume"
    config = {"configurable": {"thread_id": thread_id}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Delegate", id="message")]},
        run_id="subagent-review",
        config=config,
        thread_id=thread_id,
        mode="default",
    )
    outcome = _terminal(review).outcome
    assert outcome is not None and outcome.type == "interrupt"
    interrupt = outcome.interrupts[0]
    assert interrupt.tool_call_id is not None
    assert ScopedIdCodec().decode(interrupt.tool_call_id)[1]
    request = AgUiResumeRequest(
        entries=(_resume_entry(interrupt.id, {"type": "approve"}),),
    )
    binding = request

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="langgraph")
    completed = await _agui_events(
        definition,
        None,
        run_id="subagent-resume",
        config=config,
        thread_id=thread_id,
        mode="default",
        resume=binding,
    )

    _assert_success(completed)
    assert not any(
        "Ignoring unknown node name" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_public_subagent_resume_recovers_dynamic_messages(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    thread_id = f"tinkerfin-subagent-resume-{token}"
    checkpoint_prefix = f"tinkerfin:test:subagent:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:subagent:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    try:
        await saver.asetup()
        model = _FakeModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "Run the reviewed child tool",
                                "subagent_type": "general-purpose",
                            },
                            "id": "redis-parent-task-call",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "approved_tool",
                            "args": {"value": "redis-child"},
                            "id": "redis-child-approved-call",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="child done"),
                AIMessage(content="root done"),
            ]
        )
        definition = (
            TinkerFin(checkpointer=saver)
            .with_namespace("test")
            .build(
                model=model,
                tools=[approved_tool],
                interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
            )
        )
        config = {"configurable": {"thread_id": thread_id}}
        review = await _agui_events(
            definition,
            {"messages": [HumanMessage(content="Delegate", id="redis-message")]},
            run_id="redis-subagent-review",
            config=config,
            thread_id=thread_id,
        )
        outcome = _terminal(review).outcome
        assert outcome is not None and outcome.type == "interrupt"
        interrupt = outcome.interrupts[0]
        request = AgUiResumeRequest(
            entries=(_resume_entry(interrupt.id, {"type": "approve"}),),
        )
        binding = request

        completed = await _agui_events(
            definition,
            None,
            run_id="redis-subagent-resume",
            config=config,
            thread_id=thread_id,
            resume=binding,
        )
        _assert_success(completed)
    finally:
        try:
            await saver.adelete_thread(thread_id)
        finally:
            registry_keys = [
                key
                async for key in client.scan_iter(
                    match=f"write_keys_zset:{thread_id}:*",
                    count=100,
                )
            ]
            latest_keys = [
                key
                async for key in client.scan_iter(
                    match=f"{checkpoint_prefix}_latest:{thread_id}:*",
                    count=100,
                )
            ]
            if registry_keys or latest_keys:
                await client.unlink(*registry_keys, *latest_keys)
            for index_name in (checkpoint_prefix, write_prefix):
                try:
                    await client.execute_command("FT.DROPINDEX", index_name, "DD")
                except ResponseError:
                    pass
            await client.aclose()


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


@pytest.mark.asyncio
async def test_read_only_planner_inherits_store_and_has_no_write_tools() -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/context.txt"},
                        "id": "planner-read",
                        "type": "tool_call",
                    }
                ],
            ),
            _planner(),
        ]
    )
    store = InMemoryStore()
    await store.aput(
        ("planner-files",),
        "/context.txt",
        dict(create_file_data("unscoped data must not be visible")),
    )
    await store.aput(
        ("dGVzdA", "planner-files"),
        "/context.txt",
        dict(create_file_data("planning context")),
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver(), store=store)
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
            backend=StoreBackend(namespace=lambda _runtime: ("planner-files",)),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-store",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert any(
        isinstance(cast(tuple[BaseMessage, object], part["data"])[0], ToolMessage)
        and "planning context"
        in str(cast(tuple[BaseMessage, object], part["data"])[0].content)
        for part in parts
        if part["type"] == "messages" and part["ns"] != ()
    )
    planner_bindings = [
        set(names) for names in model.bound_tool_names if "PlannerOutcome" in names
    ]
    assert planner_bindings
    assert all(
        {"ls", "read_file", "glob", "grep", "PlannerOutcome"} <= names
        for names in planner_bindings
    )
    assert all(
        {"write_file", "edit_file", "delete", "execute"}.isdisjoint(names)
        for names in planner_bindings
    )


@pytest.mark.asyncio
async def test_read_only_planner_has_a_bounded_model_call_budget() -> None:
    repeated: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": "/"},
                    "id": f"ls-{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(6)
    ]
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=_FakeModel(responses=repeated), tools=[])
    )
    with pytest.raises(ModelCallLimitExceededError, match=r"run limit \(6/6\)"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Inspect", id="message")]},
            run_id="budget",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


@pytest.mark.asyncio
async def test_planner_and_native_preserve_runtime_context() -> None:
    model = _ContextAwareModel(responses=[_planner(), AIMessage(content="done")])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
            context_schema=_RuntimeContext,
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="context-1",
        config=config,
        mode="plan",
        context={"tenant": "tenant-1"},
    )
    await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="context-2",
        config=config,
        mode="default",
        context={"tenant": "tenant-1"},
    )
    assert model.contexts == ({"tenant": "tenant-1"}, {"tenant": "tenant-1"})


@pytest.mark.asyncio
async def test_planner_receives_only_explicit_read_only_host_tools() -> None:
    """Planning includes declared safe host tools while excluding write-capable tools."""
    from langchain_core.tools import tool

    @tool
    async def inspect_report() -> str:
        """Read a report without changing stored content."""
        return "report"

    @tool
    async def publish_report() -> str:
        """Publish a report."""
        raise AssertionError("Planning cannot publish")

    inspect_report.metadata = {"read_only": True}
    model = _FakeModel(responses=[_planner()])
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[inspect_report, publish_report],
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-host-tools",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    planner_bindings = [
        set(names) for names in model.bound_tool_names if "PlannerOutcome" in names
    ]
    assert planner_bindings
    assert all(
        "inspect_report" in names and "publish_report" not in names
        for names in planner_bindings
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("planner_images", [True, False])
async def test_attachment_capabilities_follow_planner_and_review_model(planner_images):
    """Planner and rejected-review calls use their own destination capability."""
    from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport

    attachment = Attachment(
        id="chart", name="chart.png", mime_type="image/png", size_bytes=4
    )
    root = _FakeModel(
        responses=[AIMessage(content="unused")],
        profile={"image_inputs": not planner_images},
    )
    planner = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="Please clarify the requested revision"),
        ],
        profile={"image_inputs": planner_images},
    )
    reads = []

    async def read_image(item):
        reads.append(item.id)
        return AttachmentContent(data=b"test", mime_type="image/png")

    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .with_plan(planner_model=planner)
        .build(model=root)
    )
    message = HumanMessage(
        content=[attachment.content_block()], id="attachment-message"
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [message]},
        run_id="attachment-1",
        config=config,
        mode="plan",
    )
    assert _root_interrupts(first)
    await _parts(
        definition,
        Command(resume={"type": "reject", "baseRevision": 1}),
        run_id="attachment-2",
        config=config,
        mode="plan",
    )
    assert len(planner.model_inputs) == 2
    assert reads == ([attachment.id, attachment.id] if planner_images else [])
    for request in planner.model_inputs:
        assert ("data:image/png;base64" in str(request)) == planner_images
        assert (
            "does not support this file format" in str(request)
        ) != planner_images, [m.content for m in request if isinstance(m, HumanMessage)]
    assert not root.model_inputs
    assert message.content == [attachment.content_block()]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ["general-purpose", "researcher"])
async def test_root_and_subagents_resolve_tool_images_with_host_capabilities(
    agent_name,
):
    """Native delegation retains structured responses and per-call attachment access."""
    from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport

    attachment = Attachment(
        id="chart", name="chart.png", mime_type="image/png", size_bytes=4
    )
    reads = []

    async def read_image(item):
        reads.append(item.id)
        return AttachmentContent(data=b"test", mime_type="image/png")

    @tool
    async def picture():
        """Return a chart reference."""
        return [attachment.content_block()]

    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "subagent_type": agent_name,
                            "description": "Call picture and summarize it",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"id": "image-tool", "name": "picture", "args": {}}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "summary",
                        "name": "PictureSummary",
                        "args": {"summary": "chart reviewed"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ],
        profile={"image_inputs": False},
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_attachments(
            AttachmentSupport(
                read_content=read_image,
                supports_content=lambda candidate, mime_type: candidate is model,
            )
        )
        .build(
            model=model,
            tools=[picture],
            subagents=[]
            if agent_name == "general-purpose"
            else [
                {
                    "name": agent_name,
                    "description": "Research charts",
                    "system_prompt": "Read charts",
                }
            ],
        )
    )
    message = HumanMessage(content=[attachment.content_block()], id="root-image")
    graph = await create_graph(definition)
    result = await graph.ainvoke(
        {"messages": [message]},
        config={
            "configurable": {
                "thread_id": "media-delegation",
                "__deepagents_subagent_response_format": {
                    "title": "PictureSummary",
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            }
        },
    )
    assert len(model.model_inputs) == 4
    assert "data:image/png;base64" in str(model.model_inputs[0])
    assert "data:image/png;base64" in str(model.model_inputs[2])
    assert len(reads) == 3
    assert "chart reviewed" in str(result)
    assert "data:image" not in str(result)
    assert "tinkerfin_attachment" not in str(result)
    assert message.content == [attachment.content_block()]


@pytest.mark.asyncio
async def test_attachment_projection_preserves_filesystem_eviction_and_custom_hooks():
    """Only request copies carry image bytes; eviction retains durable descriptors."""
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport

    hooks = []

    class ObservedFilesystem(FilesystemMiddleware):
        @property
        def name(self):
            return "FilesystemMiddleware"

        async def abefore_model(self, state, runtime):
            hooks.append("before")
            return None

    backend = StateBackend()
    filesystem = ObservedFilesystem(
        backend=backend, tools=["read_file"], human_message_token_limit_before_evict=10
    )
    model = _FakeModel(
        responses=[AIMessage(content="done")], profile={"image_inputs": True}
    )
    attachment = Attachment(
        id="chart", name="chart.png", mime_type="image/png", size_bytes=4
    )

    async def read_image(item):
        return AttachmentContent(data=b"test", mime_type="image/png")

    graph = await create_graph(
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .build(
            model=model,
            backend=backend,
            middleware=[filesystem],
        )
    )
    original = HumanMessage(
        content=[
            {"type": "text", "text": "large request " * 100},
            attachment.content_block(),
        ],
        id="large-input",
    )
    result = await graph.ainvoke(
        {"messages": [original]},
        config={"configurable": {"thread_id": "attachment-eviction"}},
    )
    assert hooks == ["before"]
    assert "read_file" in model.bound_tool_names[0]
    assert "write_file" not in model.bound_tool_names[0]
    assert "data:image/png;base64" in str(model.model_inputs[0])
    assert "tinkerfin_attachment" not in str(result)
    assert "data:image" not in str(result)
    saved_messages = result["messages"]
    assert isinstance(saved_messages, list)
    saved = saved_messages[0]
    assert isinstance(saved, HumanMessage)
    assert saved.content == original.content
    assert saved.additional_kwargs.get("lc_evicted_to")
    assert not original.additional_kwargs


@pytest.mark.asyncio
async def test_explicit_general_purpose_prompt_with_attachments():
    from tinkerfin.media import AttachmentSupport

    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "subagent_type": "general-purpose",
                            "description": "Read instructions",
                        },
                    }
                ],
            ),
            AIMessage(content="GP complete"),
            AIMessage(content="done"),
        ]
    )

    async def read_content(item):
        pytest.fail("text-only runs must not read attachments")

    graph = await create_graph(
        TinkerFin()
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_content))
        .build(
            model=model,
            subagents=[
                {
                    "name": "general-purpose",
                    "description": "Chart researcher",
                    "system_prompt": "Dedicated GP instructions",
                }
            ],
        )
    )
    await graph.ainvoke({"messages": [HumanMessage(content="hello")]})
    assert "task" in model.bound_tool_names[0]
    assert "Dedicated GP instructions" in str(model.model_inputs[1][0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_images", [True, False])
async def test_attachment_capability_uses_final_model_after_routing(destination_images):
    """Root and automatic GP projection occurs after model-routing middleware."""
    from tinkerfin.media import Attachment, AttachmentContent, AttachmentSupport

    attachment = Attachment(
        id="chart", name="chart.png", mime_type="image/png", size_bytes=4
    )
    initial = _FakeModel(
        responses=[AIMessage(content="must not run")],
        profile={"image_inputs": not destination_images},
    )
    destination = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "subagent_type": "general-purpose",
                            "description": "Call picture",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[{"id": "chart-tool", "name": "picture", "args": {}}],
            ),
            AIMessage(content="chart reviewed"),
            AIMessage(content="done"),
        ],
        profile={"image_inputs": destination_images},
    )

    class Route(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            return await handler(request.override(model=destination))

    reads = []

    async def read_image(item):
        reads.append(item.id)
        return AttachmentContent(data=b"test", mime_type="image/png")

    @tool
    async def picture():
        """Return a chart reference."""
        return [attachment.content_block()]

    graph = await create_graph(
        TinkerFin()
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .build(
            model=initial,
            tools=[picture],
            middleware=[Route()],
            subagents=[
                {
                    "name": "general-purpose",
                    "description": "Read charts",
                    "system_prompt": "Read charts",
                    "middleware": [Route()],
                }
            ],
        )
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=[attachment.content_block()])]}
    )
    assert not initial.model_inputs
    assert len(destination.model_inputs) == 4
    for index in [0, 2, 3]:
        assert (
            "data:image/png;base64" in str(destination.model_inputs[index])
        ) == destination_images
        assert (
            "does not support this file format" in str(destination.model_inputs[index])
        ) != destination_images
    assert len(reads) == (3 if destination_images else 0)
    assert "data:image" not in str(result)
    assert "tinkerfin_attachment" not in str(result)


@pytest.mark.asyncio
async def test_attachment_support_is_independent_of_instrumented_upstream_factory(
    monkeypatch,
):
    """Attachment integration does not use the upstream construction entry point."""
    from functools import wraps

    from deepagents import graph as native_graph

    from tinkerfin.media import AttachmentSupport

    original = native_graph.create_deep_agent
    calls = []

    @wraps(original)
    def instrumented(*args, **kwargs):
        calls.append("build")
        return original(*args, **kwargs)

    monkeypatch.setattr(native_graph, "create_deep_agent", instrumented)

    async def read_image(item):
        pytest.fail("text-only calls must not read images")

    model = _FakeModel(responses=[AIMessage(content="done")])
    await create_graph(TinkerFin().with_namespace("test").build(model=model))
    graph = await create_graph(
        TinkerFin()
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .build(model=model)
    )
    await graph.ainvoke({"messages": [HumanMessage(content="hello")]})
    assert calls == []
    assert native_graph.create_deep_agent is instrumented
    assert instrumented.__wrapped__ is original


@pytest.mark.asyncio
async def test_native_summarization_history_preserves_attachment_discovery():
    """Compacted history keeps attachment IDs and names without image bytes."""
    from deepagents.backends import StateBackend
    from deepagents.middleware.summarization import SummarizationMiddleware

    from tinkerfin.media import Attachment, AttachmentSupport

    backend = StateBackend()
    summary_model = _FakeModel(
        responses=[
            AIMessage(content="The chart remains available by its attachment ID")
        ]
    )
    summary = SummarizationMiddleware(
        summary_model, backend=backend, trigger=("messages", 2), keep=("messages", 1)
    )
    model = _FakeModel(responses=[AIMessage(content="done")])
    attachment = Attachment(
        id="chart-for-followup", name="revenue.png", mime_type="image/png", size_bytes=4
    )

    async def read_image(item):
        pytest.fail("compacted image must remain a reference")

    graph = await create_graph(
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .build(
            model=model,
            backend=backend,
            middleware=[summary],
        )
    )
    original = HumanMessage(content=[attachment.content_block()], id="old-image")
    result = await graph.ainvoke(
        {
            "messages": [
                original,
                AIMessage(content="ack"),
                HumanMessage(content="continue"),
            ]
        },
        config={"configurable": {"thread_id": "attachment-summary"}},
    )
    assert attachment.id in str(result["files"])
    assert attachment.name in str(result["files"])
    assert attachment.id in str(summary_model.model_inputs)
    assert attachment.name in str(summary_model.model_inputs)
    assert "tinkerfin_attachment" not in str(result)
    assert "data:image" not in str(result)
    assert original.content == [attachment.content_block()]


@pytest.mark.asyncio
async def test_automatic_attachments_do_not_construct_extra_model_clients(monkeypatch):
    """The native builder resolves a string model once for the whole agent tree."""
    from tinkerfin.media import AttachmentSupport

    model = _FakeModel(responses=[AIMessage(content="done")])
    resolutions = []

    def resolve(spec):
        resolutions.append(spec)
        return model

    monkeypatch.setattr("tinkerfin._agent_construction.init_chat_model", resolve)

    async def read_image(item):
        pytest.fail("text run must not resolve images")

    graph = await create_graph(
        TinkerFin()
        .with_namespace("test")
        .with_attachments(AttachmentSupport(read_content=read_image))
        .build(model="test:attachment-model")
    )
    await graph.ainvoke({"messages": [HumanMessage(content="hello")]})
    assert resolutions == ["test:attachment-model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_policy", [False, True])
async def test_attachment_filesystem_retains_trace_policy_and_hook_owner(custom_policy):
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    from tinkerfin.media import AttachmentSupport

    seen = []
    hooks = []

    class ObservedFilesystem(FilesystemMiddleware):
        @property
        def name(self):
            return "FilesystemMiddleware"

        async def abefore_model(self, state, runtime):
            hooks.append(self)
            return None

    class Capture(AsyncCallbackHandler):
        async def on_chain_start(self, serialized, inputs, **kwargs):
            if kwargs.get("name") == "FilesystemMiddleware.before_model":
                seen.append(inputs)

    filesystem = ObservedFilesystem(backend=StateBackend())
    if custom_policy:
        try:
            from langgraph.types import TracePolicy
        except ImportError:
            pytest.skip("This graph runtime does not expose middleware trace policies")
        filesystem.trace_policy = TracePolicy(
            process_inputs=lambda value: {"file_access": "summary"}
        )

    async def read_content(attachment):
        pytest.fail("text-only calls must not read attachments")

    for attachments in (False, True):
        builder = TinkerFin().with_namespace("trace")
        if attachments:
            builder = builder.with_attachments(
                AttachmentSupport(read_content=read_content)
            )
        graph = await create_graph(
            builder.build(
                model=_FakeModel(responses=[AIMessage(content="done")]),
                middleware=[filesystem],
            )
        )
        await graph.ainvoke(
            {"messages": [HumanMessage(id="request", content="private input")]},
            config={"callbacks": [Capture()]},
        )
    assert hooks == [filesystem, filesystem]
    assert len(seen) == 2 and seen[0] == seen[1]
    if custom_policy:
        assert seen == [{"file_access": "summary"}] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent_backend", [False, True])
async def test_plan_file_channels_follow_the_configured_filesystem(persistent_backend):
    from typing import get_type_hints

    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    backend = (
        StoreBackend(namespace=lambda runtime: ("plan-files",))
        if persistent_backend
        else StateBackend()
    )
    expected_files = "files" in get_type_hints(
        FilesystemMiddleware(backend=backend).state_schema
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver(), store=InMemoryStore())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=_FakeModel(responses=[_planner()]), backend=backend)
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")], "files": {}},
        run_id="file-state",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    values = [
        part["data"] for part in parts if part["type"] == "values" and part["ns"] == ()
    ]
    assert values
    for value in values:
        assert isinstance(value, Mapping)
        assert ("files" in value) is expected_files
