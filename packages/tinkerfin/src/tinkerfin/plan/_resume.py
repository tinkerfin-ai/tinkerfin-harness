"""Validate complete Plan decisions before durable resume acceptance."""

from __future__ import annotations

from collections.abc import Mapping

from ._clarification import (
    pending_contract_digest,
    restore_form,
    validate_clarification_response,
)
from ._config import PlanOptions
from ._contracts import validate_review_response
from ._state import (
    PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY,
    PLAN_SCHEMA_FINGERPRINT_KEY,
    read_plan_state,
)
from .errors import PlanModeConfigurationError, PlanStateConflictError
from .models import PlanStatus


def require_plan_schemas(state: Mapping[str, object], options: PlanOptions) -> None:
    """Require the same form and content definitions that opened this Plan."""
    if state.get(PLAN_SCHEMA_FINGERPRINT_KEY) != options.clarification.fingerprint:
        raise PlanModeConfigurationError(
            "checkpoint clarification schema does not match this Runtime"
        )
    if (
        state.get(PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY)
        != options.content.reference.fingerprint
    ):
        raise PlanModeConfigurationError(
            "checkpoint Plan content schema does not match this Runtime"
        )


def validate_plan_resume_response(
    state: Mapping[str, object], options: PlanOptions, response: object
) -> None:
    """Check the original pending state before a decision can consume its card.

    JSON Schema cannot express every Pydantic or question-dependent validator.
    Both AG-UI preparation and native Commands use this check; accepted retries
    validate against their original checkpoint anchor, never a later Plan revision.
    """
    require_plan_schemas(state, options)
    plan = read_plan_state(state, options.content)
    if plan.status is PlanStatus.AWAITING_CLARIFICATION:
        pending = plan.pending_clarification
        if pending is None:
            raise PlanStateConflictError("clarification resume has no pending form")
        if pending.contract_digest != pending_contract_digest(
            pending.form, pending.response_schema
        ):
            raise PlanStateConflictError(
                "clarification resume has an invalid contract digest"
            )
        form = restore_form(options.clarification, pending.form)
        validate_clarification_response(
            options.clarification, form, pending.response_schema, response
        )
    elif plan.status is PlanStatus.AWAITING_REVIEW:
        if plan.draft is None:
            raise PlanStateConflictError("review resume has no draft")
        validate_review_response(
            options.contracts, response, revision=plan.draft.revision
        )
    else:
        raise PlanStateConflictError("Plan response has no pending card")
