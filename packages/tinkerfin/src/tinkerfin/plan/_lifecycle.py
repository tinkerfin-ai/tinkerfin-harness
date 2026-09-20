"""Planning lifecycle within the ordinary root Agent loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from pydantic import BaseModel, JsonValue

from ._config import PlanOptions
from ._planner import planner_request_messages, planner_system_prompt
from ._state import PlanningWorkflowNodeState, plan_state_update, read_plan_state
from .errors import PlanModeConfigurationError, PlanStructuredOutputError
from .models import PlanContentModel, PlanReviewAction, PlanState, PlanStatus


class PlanLifecycle(AgentMiddleware[PlanningWorkflowNodeState, ContextT, Any]):
    """Persist card state before waiting and end approval before another model call.

    Business tools finish before this middleware interrupts. The pending Plan
    owns the card independently of a Tool call, including restored checkpoints.
    """

    state_schema = PlanningWorkflowNodeState

    def __init__(
        self,
        options: PlanOptions,
        *,
        initialize: Callable[[PlanningWorkflowNodeState], dict[str, object]],
        answer: Callable[
            [PlanningWorkflowNodeState, RunnableConfig], dict[str, object]
        ],
        review: Callable[
            [PlanningWorkflowNodeState, RunnableConfig], dict[str, object]
        ],
        validate_state: Callable[[PlanningWorkflowNodeState], None],
        clarifications: Callable[
            [PlanState[PlanContentModel]], tuple[dict[str, JsonValue], ...]
        ],
        argument_schemas: Mapping[str, type[BaseModel]],
        known_tools: set[str],
    ) -> None:
        self._options = options
        self._initialize = initialize
        self._answer = answer
        self._review = review
        self._validate_state = validate_state
        self._clarifications = clarifications
        self._argument_schemas = argument_schemas
        self._known_tools = known_tools

    @property
    def name(self) -> str:
        return "plan_lifecycle"

    def before_agent(
        self, state: PlanningWorkflowNodeState, runtime: Runtime[ContextT]
    ) -> dict[str, object]:
        # A restored pending card is already initialized and must not be lost.
        plan = read_plan_state(state, self._options.content)
        if plan.status in {
            PlanStatus.AWAITING_CLARIFICATION,
            PlanStatus.AWAITING_REVIEW,
        }:
            return {}
        return self._initialize(state)

    @hook_config(can_jump_to=["end"])
    def before_model(
        self, state: PlanningWorkflowNodeState, runtime: Runtime[ContextT]
    ) -> dict[str, object]:
        config = get_config()
        self._validate_state(state)
        current = read_plan_state(state, self._options.content)
        update: dict[str, object] = {}
        if current.status is PlanStatus.AWAITING_CLARIFICATION:
            update = self._answer(state, config)
        elif current.status is PlanStatus.AWAITING_REVIEW:
            update = self._review(state, config)
        current = read_plan_state({**state, **update}, self._options.content)
        if current.status is PlanStatus.APPROVED or (
            current.status is PlanStatus.AWAITING_INPUT
            and current.review_action
            not in {PlanReviewAction.REJECT, PlanReviewAction.CANCEL}
        ):
            return {**update, "jump_to": "end"}
        run_id = config.get("configurable", {}).get("_plan_invocation_id")
        if not isinstance(run_id, str) or not run_id:
            raise PlanModeConfigurationError("Planning requires a run identity")
        same_run = state.get("_plan_run_id") == run_id
        calls = state.get("_plan_model_calls", 0) if same_run else 0
        if calls >= 6:
            raise PlanStructuredOutputError(
                "Planning did not produce a response within six model calls"
            )
        return {
            **update,
            "_plan_run_id": run_id,
            "_plan_model_calls": calls + 1,
            "_plan_format_corrections": state.get("_plan_format_corrections", 0)
            if same_run
            else 0,
        }

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        current = read_plan_state(request.state, self._options.content)
        allowed = self._allowed_tools(current)
        selected = [
            tool
            for tool in request.tools
            if (tool.name if isinstance(tool, BaseTool) else tool.get("name"))
            in allowed
        ]
        return await handler(
            request.override(
                tools=selected,
                system_message=SystemMessage(
                    content=planner_system_prompt(
                        self._options.clarification, self._options.content, current
                    )
                ),
                messages=planner_request_messages(
                    request.messages, current, self._clarifications(current)
                ),
            )
        )

    def _allowed_tools(self, plan: PlanState[PlanContentModel]) -> set[str]:
        if plan.review_action in {PlanReviewAction.REJECT, PlanReviewAction.CANCEL}:
            return set()
        excluded = (
            "submit_plan" if plan.pending_edit is not None else "confirm_plan_edit"
        )
        return self._known_tools - {excluded}

    @hook_config(can_jump_to=["model"])
    def after_model(
        self, state: PlanningWorkflowNodeState, runtime: Runtime[ContextT]
    ) -> dict[str, object] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            raise PlanStructuredOutputError(
                "Planning model did not return an assistant message"
            )
        current = read_plan_state(state, self._options.content)
        allowed = self._allowed_tools(current)
        error: str | None = None
        if last.invalid_tool_calls:
            error = "Tool arguments must be valid JSON matching the provided schema."
        elif (
            any(call["name"] in self._argument_schemas for call in last.tool_calls)
            and len(last.tool_calls) != 1
        ):
            error = "Submit exactly one Plan action without other tools in the same response."
        else:
            for call in last.tool_calls:
                name = call["name"]
                if name not in allowed:
                    error = "Use only the tools provided for this planning request."
                    break
                if name in self._argument_schemas:
                    try:
                        self._argument_schemas[name].model_validate(call["args"])
                    except ValueError as invalid:
                        error = str(invalid)
        if error is not None:
            corrections = state.get("_plan_format_corrections", 0)
            if corrections >= 2:
                raise PlanStructuredOutputError(
                    "Planning tool arguments remained invalid after two corrections"
                )
            rejected = [*last.tool_calls, *last.invalid_tool_calls]
            messages: list[ToolMessage] = []
            for call in rejected:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise PlanStructuredOutputError(
                        "Invalid planning tool call has no stable ID"
                    )
                messages.append(
                    ToolMessage(content=error, tool_call_id=call_id, status="error")
                )
            return {
                "messages": messages,
                "_plan_format_corrections": corrections + 1,
                "jump_to": "model",
            }
        if not last.tool_calls:
            if not last.text.strip() or current.pending_edit is not None:
                raise PlanStructuredOutputError(
                    "Planning requires visible text or a valid Plan action"
                )
            return plan_state_update(
                current.model_copy(update={"status": PlanStatus.AWAITING_INPUT})
            )
        return None

    def after_agent(
        self, state: PlanningWorkflowNodeState, runtime: Runtime[ContextT]
    ) -> dict[str, object] | None:
        # LangChain's return_direct Tool branch ends at after_agent (factory.py).
        # Settle a completed read-only result without demanding another model call.
        # This node also owns the stable handoff checkpoint update site.
        current = read_plan_state(state, self._options.content)
        if current.status is PlanStatus.PLANNING:
            if not state["messages"] or not isinstance(
                state["messages"][-1], ToolMessage
            ):
                raise PlanStructuredOutputError(
                    "Planning ended without a completed response"
                )
            history = current.discussion_history
            if current.pending_edit is not None:
                history = (
                    *history,
                    self._options.content.discussion_type(
                        draft=current.draft, submitted_edit=current.pending_edit
                    ),
                )
            return plan_state_update(
                current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_INPUT,
                        "pending_edit": None,
                        "discussion_history": history,
                    }
                )
            )
        return None
