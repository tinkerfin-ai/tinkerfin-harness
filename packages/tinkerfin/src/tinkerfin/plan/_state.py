"""Typed parent-state composition for the optional Plan workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.todo import PlanningState
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from pydantic import JsonValue

from .._agui_lineage_state import PLANNING_CHECKPOINT_RUN_ID
from .._state_schema import (
    StateSchemaCompositionError,
    StateSchemaSource,
    compose_state_schema,
    middleware_state_sources,
    state_schema_field_names,
)
from ._content import PlanContentBinding
from ._handoff import PLAN_HANDOFF_STATE_KEY
from .errors import PlanModeConfigurationError
from .models import PlanContentModel, PlanContentT, PlanState

PLAN_STATE_KEY = "tinkerfin_plan"
PLAN_SCHEMA_FINGERPRINT_KEY = "_tinkerfin_plan_clarification_schema"
PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY = "_tinkerfin_plan_content_schema"
PLAN_CHECKPOINT_RUN_ID = PLANNING_CHECKPOINT_RUN_ID
PLAN_PRIVATE_STATE_KEYS = frozenset(
    {
        PLAN_SCHEMA_FINGERPRINT_KEY,
        PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY,
        PLAN_HANDOFF_STATE_KEY,
        "_plan_run_id",
        "_plan_format_corrections",
    }
)


class PlanningWorkflowNodeState(DeepAgentState, total=False):
    """Stable node-input subset shared by standalone Planning nodes."""

    _plan_run_id: NotRequired[str]
    _plan_format_corrections: NotRequired[int]
    tinkerfin_plan: NotRequired[dict[str, JsonValue]]
    _tinkerfin_plan_clarification_schema: NotRequired[str]
    _tinkerfin_plan_content_schema: NotRequired[str]


def create_plan_state_schema(
    base_schema: type[DeepAgentState] | None,
    *,
    middleware: Sequence[AgentMiddleware],  # pyright: ignore[reportMissingTypeArgument]
) -> type[DeepAgentState]:
    """Compose standalone Planning state without compiled-graph introspection."""

    base = DeepAgentState if base_schema is None else base_schema
    try:
        agent_fields = state_schema_field_names(
            AgentState,
            source="AgentState inherited fields",
        )
        deep_agent_fields = state_schema_field_names(
            DeepAgentState,
            source="DeepAgentState inherited fields",
        )
        sources = [StateSchemaSource("Deep Agent base state", base)]
        sources.extend(
            (
                StateSchemaSource(
                    "Deep Agents todo state",
                    PlanningState,
                    agent_fields,
                ),
                *middleware_state_sources(
                    middleware,
                    inherited_schema=AgentState,
                ),
                StateSchemaSource(
                    "TinkerFin Plan state",
                    PlanningWorkflowNodeState,
                    deep_agent_fields,
                ),
            )
        )
        return compose_state_schema(
            tuple(sources),
            name=f"{base.__name__}WithTinkerFinPlan",
        )
    except StateSchemaCompositionError as error:
        raise PlanModeConfigurationError(
            str(error),
            cause=error,
        ) from error


def read_plan_state(
    state: Mapping[str, object],
    content: PlanContentBinding,
) -> PlanState[PlanContentModel]:
    """Return the validated Plan state stored at the reserved root key."""

    value = state.get(PLAN_STATE_KEY)
    if value is None:
        return content.state_type()
    return content.state_type.model_validate(value)


def plan_state_update(plan: PlanState[PlanContentT]) -> dict[str, object]:
    """Serialize Plan state to the JSON-only durable checkpoint contract."""

    if not isinstance(plan, PlanState):
        raise TypeError("plan must be a PlanState")
    return {
        PLAN_STATE_KEY: plan.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    }


__all__ = [
    "PLAN_CHECKPOINT_RUN_ID",
    "PLAN_CONTENT_SCHEMA_FINGERPRINT_KEY",
    "PLAN_PRIVATE_STATE_KEYS",
    "PLAN_SCHEMA_FINGERPRINT_KEY",
    "PLAN_STATE_KEY",
    "PlanningWorkflowNodeState",
    "create_plan_state_schema",
    "plan_state_update",
    "read_plan_state",
]
