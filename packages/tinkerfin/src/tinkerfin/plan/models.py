"""Stable Plan Mode values stored in checkpoints and exposed through AG-UI state."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PlanStepId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
PlanDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PlanStatus(StrEnum):
    """Observable phase of the standalone Planning workflow."""

    PLANNING = "planning"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_INPUT = "awaiting_input"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanReviewAction(StrEnum):
    """Resolved user action for the current Plan draft."""

    APPROVE = "approve"
    CANCEL = "cancel"
    EDIT = "edit"
    RESPOND = "respond"
    REJECT = "reject"


class PlanHandoffPhase(StrEnum):
    """Durable progress from approval to native Graph completion."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    COMPLETED = "completed"


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class PlanContentModel(_PlanModel):
    """Base for one immutable, host-selectable Plan content contract."""

    media_type: ClassVar[str] = "application/json"


class StructuredPlanStep(_PlanModel):
    """One ordered, independently verifiable structured Plan step."""

    id: PlanStepId = Field(description="Stable step ID within one Plan revision")
    title: NonBlankText = Field(description="Concise step title")
    description: NonBlankText = Field(
        description="Implementation intent and boundary for this step"
    )
    verification: tuple[NonBlankText, ...] = Field(
        min_length=1,
        description="Observable checks that prove this step is complete",
    )


class StructuredPlanContent(PlanContentModel):
    """Built-in structured Plan content used when no host schema is selected."""

    goal: NonBlankText = Field(description="Operational goal of the Plan")
    assumptions: tuple[NonBlankText, ...] = Field(
        default=(),
        description="Assumptions that materially constrain execution",
    )
    steps: tuple[StructuredPlanStep, ...] = Field(
        min_length=1,
        description="Ordered implementation steps",
    )
    acceptance_criteria: tuple[NonBlankText, ...] = Field(
        min_length=1,
        description="Final observable conditions for accepting the result",
    )

    @model_validator(mode="after")
    def step_ids_are_unique(self) -> StructuredPlanContent:
        """Require stable, unambiguous step addressing within one Plan."""

        step_ids = tuple(step.id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan step IDs must be unique")
        return self


class MarkdownPlanContent(PlanContentModel):
    """Built-in Markdown Plan content preserved without whitespace rewriting."""

    media_type = "text/markdown"

    markdown: str = Field(description="Complete Markdown Plan shown to the user")

    @field_validator("markdown")
    @classmethod
    def markdown_is_not_blank(cls, value: str) -> str:
        """Reject blank text while preserving the exact accepted Markdown string."""

        if not value.strip():
            raise ValueError("markdown must not be blank")
        return value


class PlanSchemaReference(_PlanModel):
    """Exact current content contract bound to one Plan draft."""

    fingerprint: PlanDigest = Field(
        description="SHA-256 of the canonical content JSON Schema"
    )
    media_type: NonBlankText = Field(
        description="Media type used for the approved execution handoff"
    )


PlanContentT = TypeVar("PlanContentT", bound=PlanContentModel)


class PlanDraft(_PlanModel, Generic[PlanContentT]):
    """Schema-bound Plan content proposed for human review."""

    revision: int = Field(
        ge=1,
        strict=True,
        description="Monotonic draft revision",
    )
    content_schema: PlanSchemaReference = Field(
        description="Content contract frozen for this Plan cycle"
    )
    content: PlanContentT = Field(description="Validated host-selected Plan content")


class ConfirmedPlan(PlanDraft[PlanContentT], Generic[PlanContentT]):
    """Immutable Plan revision approved for native Deep Agent execution."""

    @classmethod
    def from_draft(cls, draft: PlanDraft[PlanContentT]) -> Self:
        """Freeze a validated draft without changing its revision or content."""

        if not isinstance(draft, PlanDraft):
            raise TypeError("draft must be a PlanDraft")
        return cls.model_validate(
            draft.model_dump(mode="python", by_alias=False, exclude_none=False)
        )


class PlanHandoff(_PlanModel):
    """Deterministic boundary from an approved Plan to native execution."""

    message_id: NonBlankText = Field(
        description="Original user message ID reused for native execution"
    )
    digest: PlanDigest = Field(
        description="SHA-256 of the approved Plan and bound message identity"
    )
    phase: PlanHandoffPhase = Field(
        default=PlanHandoffPhase.PENDING,
        description="Verified durable progress of the native handoff",
    )
    native_checkpoint_id: NonBlankText | None = Field(
        default=None,
        description="Native checkpoint containing the approved handoff marker",
    )
    completed_checkpoint_id: NonBlankText | None = Field(
        default=None,
        description="Terminal native checkpoint after handoff execution",
    )

    @model_validator(mode="after")
    def checkpoint_evidence_matches_phase(self) -> PlanHandoff:
        """Require checkpoint evidence for accepted and completed phases."""

        if self.phase is PlanHandoffPhase.PENDING:
            if (
                self.native_checkpoint_id is not None
                or self.completed_checkpoint_id is not None
            ):
                raise ValueError(
                    "pending handoff cannot contain native checkpoint evidence"
                )
        elif self.native_checkpoint_id is None:
            raise ValueError("accepted handoff requires native checkpoint evidence")
        if self.phase is PlanHandoffPhase.COMPLETED:
            if self.completed_checkpoint_id is None:
                raise ValueError(
                    "completed handoff requires terminal checkpoint evidence"
                )
        elif self.completed_checkpoint_id is not None:
            raise ValueError("only completed handoff can contain terminal evidence")
        return self


class RequirementAnswer(_PlanModel):
    """One trusted normalized answer or explicit optional skip."""

    question_id: PlanStepId = Field(description="Checkpoint question being resolved")
    answer_type: NonBlankText = Field(
        description="Semantic type derived from the checkpoint question"
    )
    value: dict[str, JsonValue] | None = Field(
        default=None,
        description="Canonical trusted JSON value, or None when explicitly skipped",
    )
    skipped: bool = Field(
        default=False,
        description="Whether the user explicitly skipped an optional question",
    )

    @model_validator(mode="after")
    def value_matches_skip_state(self) -> RequirementAnswer:
        """Keep normalized values and explicit skips mutually exclusive."""

        if self.skipped == (self.value is not None):
            raise ValueError("exactly one of value or skipped must resolve an answer")
        return self


class PendingClarification(_PlanModel):
    """Concrete form waiting for trusted user input at one interrupt."""

    form: dict[str, JsonValue] = Field(
        description="JSON-only form validated before checkpoint persistence"
    )
    response_schema: dict[str, JsonValue] = Field(
        description="Exact response JSON Schema bound to this form instance"
    )
    contract_digest: PlanDigest = Field(
        description="SHA-256 binding the exact form and response Schema"
    )


class ClarificationExchange(_PlanModel):
    """Resolved form and normalized answers retained as trusted Plan context."""

    form: dict[str, JsonValue] = Field(
        description="Resolved form retained as trusted Planner context"
    )
    answers: tuple[RequirementAnswer, ...] = Field(
        min_length=1,
        description="Answers normalized against the checkpoint form",
    )


class PlanDiscussionContext(_PlanModel, Generic[PlanContentT]):
    """Retain the closed card that gives a discussion message its meaning.

    Exactly one card is recorded. This context is never an answer or execution grant.
    """

    message_id: NonBlankText | None = Field(
        default=None,
        description="Discussion message ID; absent when the card was closed without a message",
    )
    clarification: dict[str, JsonValue] | None = Field(
        default=None,
        description="Trusted closed clarification form, without unsubmitted answers",
    )
    draft: PlanDraft[PlanContentT] | None = Field(
        default=None,
        description="Trusted draft whose review ended when discussion began",
    )
    submitted_edit: PlanContentT | None = Field(
        default=None,
        description="Previously submitted edit retained as context when its clarification ends",
    )

    @model_validator(mode="after")
    def one_card(self) -> Self:
        """Require one unambiguous source card."""
        if (self.clarification is None) == (self.draft is None):
            raise ValueError("discussion context requires exactly one source card")
        return self


class PlanState(_PlanModel, Generic[PlanContentT]):
    """Complete standalone Planning state projected through the AG-UI channel."""

    status: PlanStatus = PlanStatus.PLANNING
    effective_mode: Literal["default", "plan"] = "plan"
    request_message_id: NonBlankText | None = None
    pending_clarification: PendingClarification | None = None
    clarification_history: tuple[ClarificationExchange, ...] = ()
    discussion_history: tuple[PlanDiscussionContext[PlanContentT], ...] = ()
    draft: PlanDraft[PlanContentT] | None = None
    pending_edit: PlanContentT | None = None
    confirmed_plan: ConfirmedPlan[PlanContentT] | None = None
    handoff: PlanHandoff | None = None
    feedback: tuple[NonBlankText, ...] = ()
    revision: int = Field(default=0, ge=0, strict=True)
    review_action: PlanReviewAction | None = None
    review_reason: NonBlankText | None = None


__all__ = [
    "ClarificationExchange",
    "ConfirmedPlan",
    "MarkdownPlanContent",
    "PendingClarification",
    "PlanContentModel",
    "PlanContentT",
    "PlanDiscussionContext",
    "PlanDraft",
    "PlanHandoff",
    "PlanHandoffPhase",
    "PlanReviewAction",
    "PlanSchemaReference",
    "PlanState",
    "PlanStatus",
    "RequirementAnswer",
    "StructuredPlanContent",
    "StructuredPlanStep",
]
