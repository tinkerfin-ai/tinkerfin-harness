"""Read-only Planner agent used by the standalone Planning workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
    FsToolName,
)
from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.typing import ContextT

from tinkerfin._attachment_agents import _AttachmentMiddleware, attachment_filesystem
from tinkerfin._tool_runtime import _ToolRuntimeMiddleware
from tinkerfin.media import AttachmentSupport
from tinkerfin.tools import _ToolRunScope

from ._clarification import ClarificationSchemaBinding, stateless_child_config
from ._content import PlanContentBinding
from ._contracts import PlannerOutcomeBase
from .errors import PlanStructuredOutputError
from .models import PlanContentModel, PlanReviewAction, PlanState

_READ_ONLY_TOOLS: list[FsToolName] = [
    "ls",
    "read_file",
    "glob",
    "grep",
]
_PLANNER_MODEL_CALL_LIMIT = 6
_PLAN_REVIEW_REPLY_PROMPT = """You respond after the user has rejected or cancelled one
Plan draft. The rejected draft is permanently non-executable, but the conversation
remains in Planning mode. Write exactly one concise, user-visible paragraph in the
user's language. Acknowledge the decision and invite the user to continue refining the
Plan. Reflect an optional rejection reason without inventing one. Do not produce a new
draft, ask a structured clarification, call a tool, execute work, claim that Planning
mode ended, or expose private chain-of-thought."""
_PLANNER_PROMPT = """You are the single read-only Planner for a user-reviewed workflow.

You create a Plan for a separate execution Deep Agent. Your deliberately restricted
tool list exists only for optional workspace inspection; it neither describes nor
limits the execution Agent's tools. Preserve explicitly requested execution tools and
capabilities in the draft even when they are absent here. Never call, simulate, or test
an execution tool yourself, and never claim it is unavailable merely because the
Planner does not bind it.

Use read-only filesystem tools only when existing workspace evidence can materially
change the Plan. Start with one targeted listing, read, or search. If that inspection
shows no relevant artifact, stop inspecting; do not broaden the search, repeat an
equivalent query, or guess file paths. State the resulting assumption in the draft.
Spend no more than three model turns on filesystem inspection, then return the
structured outcome.

First decide whether the user's intent and constraints are sufficient for an executable
Plan. When additional information is useful, return one non-empty clarification form
that conforms to the configured structured response schema. Set required=true only when
planning cannot safely continue without that answer. Set required=false for useful but
non-blocking refinements the user may skip. A form may contain only optional questions,
but do not pause merely to collect low-value detail. Reassess sufficiency after every
complete answer batch; multiple clarification rounds are allowed.

An explicitly skipped optional question means the user chose not to provide that detail.
Do not ask the same optional question again in this Plan cycle. Continue from available
evidence and state any material assumption in the draft unless a different required
blocker is discovered.

The trusted context can contain an authoritativeEdit. It is user-authored and must never
be silently rewritten. When an authoritativeEdit is present, return clarify if it is
still insufficient, or accept_edit when it is sufficient. Do not return a replacement
draft for an authoritative edit. Without an authoritativeEdit, return clarify or one
complete draft conforming exactly to the configured Plan content schema. Treat that
schema and its field descriptions as the authoritative content contract.

Never claim to have modified state and never request a write or execution tool. Do not
expose private chain-of-thought. Choose each question's semantic answer type only from
the configured types listed below. Choice options must be concise and stable within the
form. Allow custom text only when it can safely express a valid alternative.
"""


class _StructuredAgent(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, object],
        config: RunnableConfig | None = None,
    ) -> Mapping[str, object]: ...


def resolve_planner_model(model: str | BaseChatModel) -> BaseChatModel:
    """Resolve one configured Planner model for structured and visible responses."""

    if isinstance(model, BaseChatModel):
        return model
    resolved = init_chat_model(model)
    if not isinstance(resolved, BaseChatModel):
        raise TypeError("an explicit Planner model must resolve to BaseChatModel")
    return resolved


def _planner_system_prompt(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
) -> str:
    """Add guidance derived from both configured structured response schemas."""

    count = clarification.question_count
    if count.maximum is None:
        cardinality = (
            f"at least {count.minimum} "
            f"{'question' if count.minimum == 1 else 'questions'} and sets no maximum"
        )
    elif count.minimum == count.maximum:
        cardinality = (
            f"exactly {count.minimum} "
            f"{'question' if count.minimum == 1 else 'questions'}"
        )
    else:
        cardinality = (
            f"between {count.minimum} and {count.maximum} questions, inclusive"
        )
    instruction = (
        "Whenever you return clarify, the configured clarification schema requires "
        f"{cardinality}."
    )
    if content.reference.media_type == "text/markdown":
        content_instruction = (
            "Whenever you return draft, put one complete, executable Markdown Plan in "
            "the markdown field. Preserve requested implementation boundaries and "
            "include observable verification and final acceptance conditions in that "
            "Markdown; do not wrap it in a JSON code fence."
        )
    else:
        content_instruction = (
            "Whenever you return draft, satisfy every required field and constraint "
            "of the configured Plan content schema."
        )
    type_descriptions = "\n".join(
        f"- {type_id}: {clarification.types[type_id].description}"
        for type_id in sorted(clarification.types)
    )
    type_instruction = (
        "\n\nThe configured form supports only these semantic answer types:\n"
        f"{type_descriptions}"
    )
    return (
        f"{_PLANNER_PROMPT.rstrip()}\n\n{instruction}\n\n{content_instruction}"
        f"{type_instruction}"
    )


def _invalid_structured_call_messages(
    result: Mapping[str, object],
) -> tuple[BaseMessage, ...] | None:
    """Return validated state only for a provider-invalid Planner tool call."""

    raw_messages = result.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return None
    values = cast(Sequence[object], raw_messages)
    messages = tuple(item for item in values if isinstance(item, BaseMessage))
    if len(messages) != len(values):
        return None
    last_ai = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    if last_ai is None or not any(
        call.get("name") == "PlannerOutcome" for call in last_ai.invalid_tool_calls
    ):
        return None
    return messages


def create_planner_filesystem(
    backend: BackendProtocol,
    *,
    filesystem_instructions: str | None,
    permissions: Sequence[FilesystemPermission],
) -> FilesystemMiddleware:
    """Build read-only access whose state schema also defines the parent channels.

    Planning has no file approval step. Read-interrupt rules deny access using
    the native permission ordering. The same stateless middleware can serve the
    draft and edit planners; each invocation receives its own graph state.
    """
    return FilesystemMiddleware(
        backend=backend,
        tools=_READ_ONLY_TOOLS,
        system_prompt=filesystem_instructions,
        _permissions=[
            FilesystemPermission(
                operations=["read"],
                paths=list(rule.paths),
                mode="deny" if rule.mode == "interrupt" else rule.mode,
            )
            for rule in permissions
            if "read" in rule.operations
        ],
    )


def create_planner_agent(
    model: str | BaseChatModel,
    *,
    filesystem: FilesystemMiddleware,
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    response_type: type[PlannerOutcomeBase],
    attachments: AttachmentSupport | None = None,
    read_only_tools: Sequence[BaseTool] = (),
    tool_scope: _ToolRunScope[object] | None = None,
    context_schema: type[ContextT] | None,
) -> _StructuredAgent:
    """Build a structured Planner with explicit read-only file access."""

    # LangChain composes heterogeneous middleware state schemas at runtime, but its
    # invariant generic cannot express their intersection.
    middleware = cast(
        tuple[AgentMiddleware[Any, ContextT, Any], ...],
        (
            (
                attachment_filesystem(filesystem, attachments)
                if attachments is not None
                else filesystem
            ),
            ModelCallLimitMiddleware[ContextT, Any](
                run_limit=_PLANNER_MODEL_CALL_LIMIT,
                exit_behavior="error",
            ),
            _ToolRuntimeMiddleware(tool_scope),
            *((_AttachmentMiddleware(attachments),) if attachments is not None else ()),
        ),
    )
    return cast(
        _StructuredAgent,
        create_agent(
            model=model,
            tools=read_only_tools,
            system_prompt=_planner_system_prompt(clarification, content),
            middleware=middleware,
            response_format=ToolStrategy(
                response_type,
                handle_errors=True,
            ),
            context_schema=context_schema,
            checkpointer=False,
            store=None,
            cache=None,
            name="tinkerfin_read_only_planner",
        ),
    )


async def invoke_planner(
    agent: _StructuredAgent,
    messages: Sequence[BaseMessage],
    plan: PlanState[PlanContentModel],
    *,
    response_type: type[PlannerOutcomeBase],
    clarification_history: Sequence[Mapping[str, object]],
    config: RunnableConfig,
    files: object | None,
) -> PlannerOutcomeBase:
    """Run the Planner with current requirements and the previous reviewed draft."""

    context = {
        "clarifications": list(clarification_history),
        "authoritativeEdit": (
            None
            if plan.pending_edit is None
            else plan.pending_edit.model_dump(mode="json", by_alias=True)
        ),
        "previousDraft": (
            None
            if plan.draft is None
            else plan.draft.content.model_dump(mode="json", by_alias=True)
        ),
        "feedback": list(plan.feedback),
    }
    planner_messages = [
        *messages,
        HumanMessage(
            content=(
                "Trusted Plan workflow context:\n"
                + json.dumps(context, ensure_ascii=False, indent=2)
            )
        ),
    ]
    planner_input: dict[str, object] = {"messages": planner_messages}
    if files is not None:
        planner_input["files"] = files
    result = await agent.ainvoke(
        planner_input,
        config=stateless_child_config(config),
    )
    response = result.get("structured_response")
    if isinstance(response, response_type):
        return response

    invalid_messages = _invalid_structured_call_messages(result)
    if invalid_messages is not None:
        retry_input: dict[str, object] = {
            "messages": [
                *invalid_messages,
                HumanMessage(
                    content=(
                        "Your previous PlannerOutcome tool call had invalid JSON "
                        "arguments and was not executed. Return exactly one valid "
                        "PlannerOutcome tool call for the same planning decision. "
                        "Use strict JSON without trailing commas or comments."
                    )
                ),
            ]
        }
        retry_files = result.get("files", files)
        if retry_files is not None:
            retry_input["files"] = retry_files
        result = await agent.ainvoke(
            retry_input,
            config=stateless_child_config(config),
        )
        response = result.get("structured_response")
        if isinstance(response, response_type):
            return response

    raise PlanStructuredOutputError(
        "Planner did not return the configured structured response type"
    )


async def invoke_plan_review_reply(
    model: BaseChatModel,
    messages: Sequence[BaseMessage],
    plan: PlanState[PlanContentModel],
    *,
    config: RunnableConfig,
    attachments: AttachmentSupport | None = None,
) -> AIMessage:
    """Generate the sole visible reply after a rejected or cancelled draft.

    The direct model call deliberately binds no Tool and runs in the parent Planning
    node, so its message is visible in the current stream without entering the native
    execution Graph. The durable Plan state remains the authority for subsequent input.

    Args:
        model: Resolved Planner chat model shared by the Planning definition.
        messages: Current user-visible Planning conversation.
        plan: Durable state containing the resolved review decision.
        config: Current parent Runnable configuration.
        attachments: Optional authorized request-time attachment access.

    Returns:
        One non-empty assistant message to append to Planning state.

    Raises:
        PlanStructuredOutputError: The model returns no visible text or attempts a Tool
            call despite the reply-only contract.
    """

    decision = plan.review_action
    if decision not in {PlanReviewAction.REJECT, PlanReviewAction.CANCEL}:
        raise PlanStructuredOutputError(
            "Plan review reply requires a reject or cancel decision"
        )
    assert decision is not None
    draft = plan.draft
    context = {
        "decision": decision.value,
        "reason": plan.review_reason,
        "rejectedDraft": (
            None
            if draft is None
            else draft.content.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
        ),
    }
    if attachments is not None:
        messages = await attachments._prepare_messages(messages, model=model)
    response = await model.ainvoke(
        [
            SystemMessage(content=_PLAN_REVIEW_REPLY_PROMPT),
            *messages,
            HumanMessage(
                content=(
                    "Trusted Plan review decision:\n"
                    + json.dumps(context, ensure_ascii=False, indent=2)
                )
            ),
        ],
        config=stateless_child_config(config),
    )
    if response.tool_calls or response.invalid_tool_calls or not response.text.strip():
        raise PlanStructuredOutputError(
            "Plan review reply must contain visible text without Tool calls"
        )
    return response


__all__ = [
    "create_planner_agent",
    "invoke_plan_review_reply",
    "invoke_planner",
    "resolve_planner_model",
]
