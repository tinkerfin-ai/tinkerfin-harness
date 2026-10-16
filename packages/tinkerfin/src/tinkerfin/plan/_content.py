"""Validated bindings for host-selectable Plan content schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pydantic import ConfigDict, JsonValue, TypeAdapter, create_model

from ._clarification import _require_concrete_model, _schema_fingerprint
from .errors import PlanModeConfigurationError, PlanStructuredOutputError
from .models import (
    ConfirmedPlan,
    MarkdownPlanContent,
    PlanContentModel,
    PlanDiscussionContext,
    PlanDraft,
    PlanSchemaReference,
    PlanState,
)

_JSON_OBJECT = TypeAdapter(
    dict[str, JsonValue],
    config=ConfigDict(allow_inf_nan=False),
)
_SUPPORTED_MEDIA_TYPES = frozenset({"application/json", "text/markdown"})


@dataclass(frozen=True, slots=True)
class PlanContentBinding:
    """Concrete content and state models frozen for one agent Definition."""

    schema: type[PlanContentModel]
    reference: PlanSchemaReference
    draft_type: type[PlanDraft[PlanContentModel]]
    confirmed_type: type[ConfirmedPlan[PlanContentModel]]
    state_type: type[PlanState[PlanContentModel]]
    discussion_type: type[PlanDiscussionContext[PlanContentModel]]


def _validate_content_schema(value: object) -> type[PlanContentModel]:
    if not isinstance(value, type) or not issubclass(value, PlanContentModel):
        raise PlanModeConfigurationError(
            "content_schema must be a PlanContentModel subclass"
        )
    if value is PlanContentModel:
        raise PlanModeConfigurationError(
            "content_schema must be a concrete content model"
        )
    _require_concrete_model(value, source="content_schema")
    media_type = value.media_type
    if media_type not in _SUPPORTED_MEDIA_TYPES:
        raise PlanModeConfigurationError(
            "content_schema.media_type must be 'application/json' or 'text/markdown'"
        )
    if media_type == "text/markdown" and not issubclass(value, MarkdownPlanContent):
        raise PlanModeConfigurationError(
            "text/markdown content_schema must inherit MarkdownPlanContent"
        )
    if value.model_config.get("extra") != "forbid" or not value.model_config.get(
        "frozen"
    ):
        raise PlanModeConfigurationError(
            "content_schema must preserve frozen=True and extra='forbid'"
        )
    try:
        _JSON_OBJECT.validate_python(value.model_json_schema(by_alias=True))
    except Exception as error:
        raise PlanModeConfigurationError(
            "content_schema could not produce a JSON object Schema",
            cause=error,
        ) from error
    return value


def create_plan_content_binding(content_schema: object) -> PlanContentBinding:
    """Validate one content schema and build its immutable runtime model family."""

    content_type = _validate_content_schema(content_schema)
    reference = PlanSchemaReference(
        fingerprint=_schema_fingerprint(content_type),
        media_type=content_type.media_type,
    )
    draft_type = create_model(
        "PlanDraft",
        __base__=PlanDraft,
        content=(content_type, ...),
    )
    confirmed_type = create_model(
        "ConfirmedPlan",
        __base__=ConfirmedPlan,
        content=(content_type, ...),
    )
    discussion_type = create_model(
        "PlanDiscussionContext",
        __base__=PlanDiscussionContext[PlanContentModel],
        draft=(draft_type | None, None),
        submitted_edit=(content_type | None, None),
    )
    state_type = create_model(
        "PlanState",
        __base__=PlanState,
        draft=(draft_type | None, None),
        pending_edit=(content_type | None, None),
        discussion_history=(tuple[discussion_type, ...], ()),
        confirmed_plan=(confirmed_type | None, None),
    )
    return PlanContentBinding(
        schema=content_type,
        reference=reference,
        draft_type=cast(type[PlanDraft[PlanContentModel]], draft_type),
        confirmed_type=cast(type[ConfirmedPlan[PlanContentModel]], confirmed_type),
        state_type=cast(type[PlanState[PlanContentModel]], state_type),
        discussion_type=discussion_type,
    )


def serialize_plan_content(
    binding: PlanContentBinding,
    value: object,
) -> tuple[PlanContentModel, dict[str, JsonValue]]:
    """Validate and round-trip model output before checkpoint persistence."""

    if not isinstance(value, binding.schema):
        raise PlanStructuredOutputError(
            "structured draft did not use the configured content_schema"
        )
    try:
        payload = _JSON_OBJECT.validate_python(
            value.model_dump(mode="json", by_alias=True, exclude_none=False)
        )
        restored = binding.schema.model_validate(payload)
    except Exception as error:
        raise PlanStructuredOutputError(
            "structured draft failed its JSON checkpoint round-trip",
            cause=error,
        ) from error
    return restored, payload


__all__ = [
    "PlanContentBinding",
    "create_plan_content_binding",
    "serialize_plan_content",
]
