"""Public contract for Deep Agents Tool review interrupts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from ag_ui.core.types import Interrupt as AgUiInterrupt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import AgUiAdapterErrorCode, AgUiConversionError
from .hitl import HitlRequest
from .ids import ScopedIdCodec
from .models import JsonObject
from .reasoning import json_values_equal, normalize_operational_data

TOOL_REVIEW_SCHEMA = "tinkerfin.deepagents.tool-review"
ToolReviewDecision = Literal["approve", "edit", "reject", "respond"]


class ToolReviewContractError(AgUiConversionError):
    """A complete AG-UI interrupt violates the current Tool review contract."""

    code = AgUiAdapterErrorCode.TOOL_REVIEW_CONTRACT_INVALID


class ToolReviewInterruptMetadata(BaseModel):
    """Persisted Tool review facts required for display and native resume.

    The model is the complete public shape of `metadata.deepagents`. It is
    immutable at the protocol boundary and does not contain host UI state,
    database fields, or client-submitted decisions.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_id: Literal["tinkerfin.deepagents.tool-review"] = Field(
        alias="schema", description="Exact TinkerFin Tool review metadata schema"
    )
    native_interrupt_id: str = Field(
        alias="nativeInterruptId",
        strict=True,
        min_length=1,
        description="Native LangGraph interrupt group ID",
    )
    action_index: int = Field(
        alias="actionIndex",
        strict=True,
        ge=0,
        description="Action position within the native interrupt group",
    )
    tool_name: str = Field(
        alias="toolName",
        strict=True,
        min_length=1,
        description="Reviewed Deep Agents Tool name",
    )
    allowed_decisions: tuple[ToolReviewDecision, ...] = Field(
        alias="allowedDecisions",
        min_length=1,
        description="Decisions allowed for this exact action",
    )
    original_args: JsonObject = Field(
        alias="originalArgs",
        description="Original reviewed Tool arguments as a JSON object",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_json_graph(cls, value: object) -> object:
        """Reject opaque and non-finite values before field validation."""

        return normalize_operational_data(value)

    @field_validator("native_interrupt_id", "tool_name")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        """Reject blank or whitespace-padded identifiers."""

        if value != value.strip():
            raise ValueError("Tool review identifiers cannot contain outer whitespace")
        return value

    @field_validator("allowed_decisions")
    @classmethod
    def decisions_are_unique(
        cls,
        value: tuple[ToolReviewDecision, ...],
    ) -> tuple[ToolReviewDecision, ...]:
        """Reject duplicate decisions that would make policy display ambiguous."""

        if len(set(value)) != len(value):
            raise ValueError("allowedDecisions cannot contain duplicates")
        return value


@dataclass(frozen=True, slots=True)
class _ParsedToolReview:
    metadata: ToolReviewInterruptMetadata
    request: HitlRequest


def _parse_tool_review_interrupt(interrupt: AgUiInterrupt) -> _ParsedToolReview:
    """Validate the complete persisted interrupt and return native correlation."""

    try:
        if not isinstance(interrupt, AgUiInterrupt):
            raise TypeError("interrupt must be an ag_ui.core.Interrupt")
        if interrupt.reason != "tool_call":
            raise ValueError("interrupt reason must be tool_call")
        tool_call_id = interrupt.tool_call_id
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("Tool review interrupt requires toolCallId")
        kind, _namespace, _raw_id = ScopedIdCodec().decode(tool_call_id)
        if kind != "tool":
            raise ValueError("toolCallId must identify a scoped Tool call")

        raw_metadata = interrupt.metadata
        if not isinstance(raw_metadata, Mapping):
            raise TypeError("interrupt metadata must be an object")
        metadata_mapping = cast(Mapping[object, object], raw_metadata)
        review = ToolReviewInterruptMetadata.model_validate(
            metadata_mapping.get("deepagents")
        )
        request_value = normalize_operational_data(
            metadata_mapping.get("langgraphValue")
        )
        request = HitlRequest.model_validate(request_value)

        if review.action_index >= len(request.action_requests):
            raise ValueError("actionIndex is outside the native interrupt group")
        multi_action = len(request.action_requests) > 1
        expected_public_id = (
            f"{review.native_interrupt_id}#{review.action_index}"
            if multi_action
            else review.native_interrupt_id
        )
        if interrupt.id != expected_public_id:
            raise ValueError("interrupt ID does not match its native action position")

        action = request.action_requests[review.action_index]
        policy = request.review_configs[review.action_index]
        if review.tool_name != action.name:
            raise ValueError("toolName does not match the native action")
        if review.allowed_decisions != tuple(policy.allowed_decisions):
            raise ValueError("allowedDecisions does not match the native review policy")
        if not json_values_equal(review.original_args.root, action.args.root):
            raise ValueError("originalArgs does not match the native action")
        return _ParsedToolReview(metadata=review, request=request)
    except ToolReviewContractError:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        raise ToolReviewContractError(
            f"invalid Tool review interrupt: {getattr(interrupt, 'id', '<unknown>')}",
            cause=error,
        ) from error


def parse_tool_review_interrupt(
    interrupt: AgUiInterrupt,
) -> ToolReviewInterruptMetadata:
    """Parse one complete AG-UI interrupt into validated Tool review facts.

    Args:
        interrupt: Trusted complete interrupt persisted from an Adapter terminal.

    Returns:
        The immutable current metadata after cross-checking the native HITL request,
        public interrupt ID, action position, policy, arguments, and scoped Tool ID.

    Raises:
        ToolReviewContractError: The interrupt is not a valid Tool review value.
    """

    return _parse_tool_review_interrupt(interrupt).metadata


__all__ = [
    "TOOL_REVIEW_SCHEMA",
    "ToolReviewContractError",
    "ToolReviewDecision",
    "ToolReviewInterruptMetadata",
    "parse_tool_review_interrupt",
]
