"""Static inference checks for the generated public façade stubs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    TypedDict,
    assert_type,
    cast,
    reveal_type,
)

from deepagents import CompiledSubAgent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import (
    AgentRuntime,
    AgUiRunStream,
    NativeRunStream,
    RunIdentity,
    TinkerFin,
)
from tinkerfin.agui_resume import AgUiResumeRequest
from tinkerfin.deep_agent import DeepAgentGraph, create_graph
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestionBase,
    ClarificationResponseBase,
    ClarificationType,
    PlanReviewAction,
    SingleChoiceQuestion,
    clarification_type,
)
from tinkerfin_contracts import RuntimeObserver


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _Context(TypedDict):
    tenant: str


class _QuestionAttributes(ClarificationModel):
    category: str


class _OptionAttributes(ClarificationModel):
    priority: int


class _Option(ClarificationOption[_OptionAttributes]):
    pass


class _Question(SingleChoiceQuestion[_QuestionAttributes, _Option]):
    pass


class _Form(ClarificationForm[_Question]):
    pass


class _RatingQuestion(ClarificationQuestionBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    maximum: int


class _RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    rating: int


if TYPE_CHECKING:
    tinkerfin = TinkerFin(checkpointer=InMemorySaver()).with_namespace("test")
    observer = cast(RuntimeObserver, object())
    observed = tinkerfin.with_observer(observer)
    assert_type(observed, TinkerFin)
    observed_definition = observed.build(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(observed_definition, AgentRuntime[_Context])
    definition = tinkerfin.build(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(definition, AgentRuntime[_Context])

    planned = tinkerfin.with_plan(enabled=True)
    assert_type(planned, TinkerFin)
    editable_planned = tinkerfin.with_plan(
        allowed_review_actions=(PlanReviewAction.APPROVE, PlanReviewAction.EDIT)
    )
    assert_type(editable_planned, TinkerFin)
    planned_definition = planned.build(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(planned_definition, AgentRuntime[_Context])

    custom_planned = tinkerfin.with_plan(clarification_schema=_Form)
    assert_type(custom_planned, TinkerFin)
    custom_option = _Option(id="option", label="Option", attributes=None)
    assert_type(custom_option.attributes, _OptionAttributes | None)
    rating_type = clarification_type(
        type_id="acme:rating",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    assert_type(
        rating_type,
        ClarificationType[_RatingQuestion, _RatingResponse],
    )
    assert_type(tinkerfin.with_plan(clarification_types=(rating_type,)), TinkerFin)

    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")
    native = definition
    agui = definition
    resumed_agui = definition
    planned_native = planned_definition
    planned_agui = planned_definition
    assert_type(native, AgentRuntime[_Context])
    assert_type(agui, AgentRuntime[_Context])
    assert_type(resumed_agui, AgentRuntime[_Context])
    assert_type(planned_native, AgentRuntime[_Context])
    assert_type(planned_agui, AgentRuntime[_Context])

    graph_input = cast(InputAgentState, {"messages": []})
    assert_type(
        native.open_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=graph_input,
            context={"tenant": "tenant-1"},
        ),
        NativeRunStream,
    )
    assert_type(
        agui.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=graph_input,
            context={"tenant": "tenant-1"},
        ),
        AgUiRunStream,
    )
    assert_type(
        resumed_agui.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            resume=AgUiResumeRequest.model_validate(
                {"entries": [{"interruptId": "i", "status": "cancelled"}]}
            ),
            context={"tenant": "tenant-1"},
        ),
        AgUiRunStream,
    )

    async def check_managed_and_direct_graphs() -> None:
        graph = await create_graph(definition)
        assert_type(graph, DeepAgentGraph)
        subagent: CompiledSubAgent = {
            "name": "typed-subagent",
            "description": "Exercises the public async Runnable contract.",
            "runnable": graph,
        }
        assert_type(subagent["runnable"], Runnable)
        assert_type(
            definition.open_run(
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                input=graph_input,
                context={"tenant": "tenant-1"},
            ),
            NativeRunStream,
        )
        assert_type(
            definition.open_agui_run(
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                input=graph_input,
                context={"tenant": "tenant-1"},
            ),
            AgUiRunStream,
        )

    reveal_type(tinkerfin.build)
    reveal_type(native.open_run)
    reveal_type(agui.open_agui_run)
    reveal_type(resumed_agui.open_agui_run)
