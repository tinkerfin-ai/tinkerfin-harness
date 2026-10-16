"""Planner guidance for user-reviewed work with the bound tool permissions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.tools import BaseTool

from .._agent_spec import ToolDefinition
from ._clarification import ClarificationSchemaBinding
from ._content import PlanContentBinding
from .models import PlanContentModel, PlanReviewAction, PlanState

_PLAN_REVIEW_REPLY_PROMPT = """You respond after the user has rejected or cancelled one
Plan draft. The rejected draft is permanently non-executable, but the conversation
remains in Planning mode. Write exactly one concise, user-visible paragraph in the
user's language. Acknowledge the decision and invite the user to continue refining the
Plan. Reflect an optional rejection reason without inventing one. Do not produce a new
draft, ask a structured clarification, call a tool, execute work, claim that Planning
mode ended, or expose private chain-of-thought."""
_PLANNER_PROMPT = """You are the Planner for a user-reviewed workflow.

Use the bound tools and workspace to investigate the request, inspect documents,
and run analysis needed to prepare an accurate Plan. The current tool permissions
apply throughout Planning: operations requiring review must await that review.
Tool approval authorizes only the reviewed operation; it does not approve a Plan
draft or start the approved Plan's execution. Continue Planning after analysis.
Preserve the user's requested scope and capabilities in the draft, and distinguish
observed results from assumptions. Do not carry out the proposed implementation
before the user approves the complete Plan.

Choose whether this turn calls for an ordinary reply, clarification, or a complete
Plan draft. Reply directly to explain, compare, analyze, or discuss. Even an explicit request for a Plan may
benefit from discussion first. Never reply merely to acknowledge the request or
promise background planning. If choosing a draft, check that the user's intent and
constraints are sufficient for an executable Plan. When additional information is useful, call ask_user_question with one non-empty clarification form
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
be silently rewritten. When an authoritativeEdit is present, call ask_user_question if it is
still insufficient, or confirm_plan_edit when it is sufficient. Never replace an
authoritative edit. Otherwise reply directly, ask_user_question, or submit_plan
with one complete draft conforming exactly to the configured Plan content schema. Treat that
schema and its field descriptions as the authoritative content contract.

Issue at most one Plan action per response, without other tools in that batch.
Report tool results accurately and do not expose private chain-of-thought.
Choose each question's semantic answer type only from
the configured types listed below. Choice options must be concise and stable within the
form. Allow custom text only when it can safely express a valid alternative.
"""


def resolve_planner_model(model: str | BaseChatModel) -> BaseChatModel:
    """Resolve one configured Planner model for structured and visible responses."""

    if isinstance(model, BaseChatModel):
        return model
    resolved = init_chat_model(model)
    if not isinstance(resolved, BaseChatModel):
        raise TypeError("an explicit Planner model must resolve to BaseChatModel")
    return resolved


def planner_tool_name(definition: ToolDefinition) -> str | None:
    """Read the model-facing name without replacing a declared tool."""
    if isinstance(definition, BaseTool):
        return definition.name
    if isinstance(definition, dict):
        declaration = cast(Mapping[object, object], definition)
        name = declaration.get("name", declaration.get("type"))
    else:
        name = getattr(definition, "__name__", None)
    return name if isinstance(name, str) else None


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
        "When calling ask_user_question, the configured clarification schema requires "
        f"{cardinality}."
    )
    if content.reference.media_type == "text/markdown":
        content_instruction = (
            "When calling submit_plan, put one complete, executable Markdown Plan in "
            "the markdown field. Preserve requested implementation boundaries and "
            "include observable verification and final acceptance conditions in that "
            "Markdown; do not wrap it in a JSON code fence."
        )
    else:
        content_instruction = (
            "When calling submit_plan, satisfy every required field and constraint "
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


def planner_request_messages(
    messages: Sequence[AnyMessage],
    plan: PlanState[PlanContentModel],
    clarifications: Sequence[Mapping[str, object]],
) -> list[AnyMessage]:
    """Supply trusted card context without writing instructions into history."""
    context = {
        "clarifications": list(clarifications),
        "authoritativeEdit": None
        if plan.pending_edit is None
        else plan.pending_edit.model_dump(mode="json", by_alias=True),
        "previousDraft": None
        if plan.draft is None
        else plan.draft.content.model_dump(mode="json", by_alias=True),
        "feedback": list(plan.feedback),
        "discussions": [
            item.model_dump(mode="json", by_alias=True)
            for item in plan.discussion_history
        ],
        "decision": None if plan.review_action is None else plan.review_action.value,
        "reason": plan.review_reason,
    }
    return [
        *messages,
        HumanMessage(
            content="Trusted Plan context:\n" + json.dumps(context, ensure_ascii=False)
        ),
    ]


def planner_system_prompt(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    plan: PlanState[PlanContentModel],
) -> str:
    """Select planning or review acknowledgement guidance without changing the model."""
    if plan.review_action in {PlanReviewAction.REJECT, PlanReviewAction.CANCEL}:
        return _PLAN_REVIEW_REPLY_PROMPT
    return _planner_system_prompt(clarification, content)
