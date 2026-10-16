"""Immutable configuration and run-mode contracts for Plan-capable agents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias, TypeGuard

from langchain_core.language_models import BaseChatModel

from ._clarification import ClarificationSchemaBinding
from ._content import PlanContentBinding
from ._contracts import PlanContractBinding
from .errors import PlanModeConfigurationError
from .models import PlanReviewAction

AgentMode: TypeAlias = Literal["default", "plan"]
DEFAULT_ALLOWED_REVIEW_ACTIONS = (
    PlanReviewAction.APPROVE,
    PlanReviewAction.RESPOND,
    PlanReviewAction.REJECT,
)


@dataclass(frozen=True, slots=True)
class PlanOptions:
    """Definition-level options shared by every run of one Plan-capable agent."""

    clarification: ClarificationSchemaBinding
    content: PlanContentBinding
    contracts: PlanContractBinding
    allowed_review_actions: tuple[PlanReviewAction, ...]
    default_mode: AgentMode = "default"
    planner_model: str | BaseChatModel | None = None


def _is_object_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence)


def validate_allowed_review_actions(
    value: object,
) -> tuple[PlanReviewAction, ...]:
    """Freeze one ordered, non-empty Plan review action configuration.

    Args:
        value: Candidate action sequence supplied by a Plan-capable host.

    Returns:
        The validated actions in caller-defined order.

    Raises:
        TypeError: The value is not a sequence of ``PlanReviewAction`` members.
        PlanModeConfigurationError: The sequence is empty or contains duplicates.
    """

    if isinstance(value, (str, bytes)) or not _is_object_sequence(value):
        raise TypeError(
            "allowed_review_actions must be a sequence of PlanReviewAction values"
        )
    normalized: list[PlanReviewAction] = []
    for action in value:
        if not isinstance(action, PlanReviewAction):
            raise TypeError(
                "allowed_review_actions must contain only PlanReviewAction values"
            )
        normalized.append(action)
    actions = tuple(normalized)
    if not actions:
        raise PlanModeConfigurationError("allowed_review_actions must not be empty")
    if len(actions) != len(set(actions)):
        raise PlanModeConfigurationError(
            "allowed_review_actions must not contain duplicates"
        )
    return actions


def validate_agent_mode(value: object, *, name: str = "mode") -> AgentMode:
    """Return one supported mode or raise a public configuration error."""

    if value not in ("default", "plan"):
        raise PlanModeConfigurationError(f"{name} must be either 'default' or 'plan'")
    return value


def resolve_agent_mode(
    value: object | None,
    *,
    options: PlanOptions | None,
) -> AgentMode:
    """Resolve the requested mode against the Runtime's configured capabilities."""

    if value is None:
        mode: AgentMode = "default" if options is None else options.default_mode
    else:
        mode = validate_agent_mode(value)
    if mode == "plan" and options is None:
        raise PlanModeConfigurationError(
            "mode='plan' requires a Plan-capable AgentRuntime"
        )
    return mode


__all__ = [
    "DEFAULT_ALLOWED_REVIEW_ACTIONS",
    "AgentMode",
    "PlanOptions",
    "resolve_agent_mode",
    "validate_agent_mode",
    "validate_allowed_review_actions",
]
