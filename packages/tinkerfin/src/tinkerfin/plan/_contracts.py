"""Tool argument and resume contracts for Plan Mode."""

from __future__ import annotations

from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    create_model,
)
from pydantic.alias_generators import to_camel

from ._clarification import ClarificationSchemaBinding
from ._content import PlanContentBinding
from .models import NonBlankText, PlanReviewAction


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class PlanClarificationPayload(_ContractModel):
    """Public Plan clarification payload projected through runtime metadata."""

    form: dict[str, JsonValue]


class PlanClarificationMetadata(_ContractModel):
    """Public metadata for one Planner clarification interrupt."""

    origin: Literal["plan"] = "plan"
    clarification: PlanClarificationPayload


class ApprovePlan(_ContractModel):
    """Approve the current Plan revision without edits."""

    type: Literal["approve"]
    base_revision: int = Field(ge=1, strict=True)


class CancelPlan(_ContractModel):
    """Discard the current draft while keeping the Planning conversation active."""

    type: Literal["cancel"]
    base_revision: int = Field(ge=1, strict=True)


class DismissPlanReview(_ContractModel):
    """Close the current review without a model reply or execution grant."""

    type: Literal["dismiss"]
    base_revision: int = Field(ge=1, strict=True)


class EditPlanBase(_ContractModel):
    """Identify an edit decision for one current Plan revision."""

    type: Literal["edit"]
    base_revision: int = Field(ge=1, strict=True)


class RespondToPlan(_ContractModel):
    """Return user feedback for one current Plan revision."""

    type: Literal["respond"]
    base_revision: int = Field(ge=1, strict=True)
    # Publish the constraint so invalid feedback cannot consume a pending review.
    message: NonBlankText = Field(pattern=r"\S")


class RejectPlan(_ContractModel):
    """Reject one current Plan revision with optional feedback."""

    type: Literal["reject"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText | None = None


class PlanReviewPayloadBase(_ContractModel):
    """Review payload specialized with one concrete draft type."""


class PlanReviewMetadataBase(_ContractModel):
    """Public metadata wrapper specialized with one concrete review payload."""

    origin: Literal["plan"] = "plan"


@dataclass(frozen=True, slots=True)
class PlanContractBinding:
    """Dynamic Planner and review contracts frozen for one Definition."""

    question_args: type[BaseModel]
    draft_args: type[BaseModel]
    review_response: TypeAdapter[object]
    review_payload_type: type[_ContractModel]
    review_metadata_type: type[_ContractModel]


def validate_review_response(
    binding: PlanContractBinding, response: object, *, revision: int
) -> (
    ApprovePlan
    | CancelPlan
    | DismissPlanReview
    | EditPlanBase
    | RespondToPlan
    | RejectPlan
):
    """Reject invalid decisions before they consume the current review interrupt."""

    decision = cast(
        ApprovePlan
        | CancelPlan
        | DismissPlanReview
        | EditPlanBase
        | RespondToPlan
        | RejectPlan,
        binding.review_response.validate_python(response),
    )
    if decision.base_revision != revision:
        raise ValueError("Plan review baseRevision is stale")
    return decision


def review_response_schema(
    binding: PlanContractBinding, *, revision: int
) -> dict[str, JsonValue]:
    """Bind approval to the current revision before AG-UI persists a resume intent.

    AgUiResumeBinding.from_native validates this exact interrupt schema before
    stage_agui_resume_intent can consume the card. Native Commands additionally
    pass validate_review_response before entering the planning graph.
    """

    schema = TypeAdapter(dict[str, JsonValue]).validate_python(
        binding.review_response.json_schema(by_alias=True)
    )
    schema["properties"] = {"baseRevision": {"const": revision}}
    return schema


def _review_model_union(
    review_types: tuple[type[BaseModel], ...],
) -> type[BaseModel] | UnionType:
    """Combine configured review models into one runtime type expression."""

    review_union = review_types[0] | review_types[1]
    for review_type in review_types[2:]:
        review_union = review_union | review_type
    return review_union


def create_plan_contract_binding(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    *,
    allowed_review_actions: tuple[PlanReviewAction, ...],
) -> PlanContractBinding:
    """Bind one clarification form and one Plan content schema atomically."""

    question_args = create_model(
        "PlanQuestionArguments",
        __base__=_ContractModel,
        form=(clarification.form_schema, ...),
    )
    draft_args = create_model(
        "PlanDraftArguments",
        __base__=_ContractModel,
        content=(content.schema, ...),
    )
    edit_type = create_model(
        "EditPlan",
        __base__=EditPlanBase,
        content=(content.schema, ...),
    )
    review_types = tuple(
        {
            PlanReviewAction.APPROVE: ApprovePlan,
            PlanReviewAction.CANCEL: CancelPlan,
            PlanReviewAction.EDIT: edit_type,
            PlanReviewAction.RESPOND: RespondToPlan,
            PlanReviewAction.REJECT: RejectPlan,
        }[action]
        for action in allowed_review_actions
    )
    review_types = (*review_types, DismissPlanReview)
    review_union = _review_model_union(review_types)
    review_annotation = (
        Annotated[
            review_union,
            Field(discriminator="type"),
        ]  # pyright: ignore[reportInvalidTypeForm]
    )
    review_response = cast(
        TypeAdapter[object],
        TypeAdapter(review_annotation),
    )
    review_payload_type = create_model(
        "PlanReviewPayload",
        __base__=PlanReviewPayloadBase,
        draft=(content.draft_type, ...),
    )
    review_metadata_type = create_model(
        "PlanReviewMetadata",
        __base__=PlanReviewMetadataBase,
        review=(review_payload_type, ...),
    )
    return PlanContractBinding(
        question_args=question_args,
        draft_args=draft_args,
        review_response=review_response,
        review_payload_type=review_payload_type,
        review_metadata_type=review_metadata_type,
    )


__all__ = [
    "ApprovePlan",
    "CancelPlan",
    "EditPlanBase",
    "PlanClarificationMetadata",
    "PlanClarificationPayload",
    "PlanContractBinding",
    "PlanReviewMetadataBase",
    "PlanReviewPayloadBase",
    "RejectPlan",
    "RespondToPlan",
    "create_plan_contract_binding",
]
