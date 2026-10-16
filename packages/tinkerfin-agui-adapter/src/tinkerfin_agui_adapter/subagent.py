"""Stable protocol projection of Deep Agents subagent task input."""

from __future__ import annotations

import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import RunIdentity
from .ids import ScopedIdCodec

SUBAGENT_PROVENANCE_SCHEMA = "tinkerfin.subagent-provenance"


class SubagentTaskInput(BaseModel):
    """Effective locked projection of the Deep Agents `task` Tool input.

    Deep Agents 0.7.5 validates model-produced Tool arguments with
    ``TaskToolSchema``, whose Pydantic boundary ignores unknown fields. LangGraph's
    v2 ``tasks`` start record retains the pre-validation argument object, so this
    projection must apply the same rule while the Adapter keeps the complete native
    input in sanitized task provenance.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    description: str = Field(
        min_length=1,
        description="Task description passed to the subagent",
    )
    subagent_type: str = Field(
        min_length=1,
        description="Target subagent type",
    )


class SubagentProvenance(BaseModel):
    """Stable identity and native provenance for one logical subagent invocation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_id: Literal["tinkerfin.subagent-provenance"] = Field(
        alias="schema",
        description="Exact TinkerFin subagent provenance schema",
    )
    subagent_invocation_id: str = Field(
        alias="subagentInvocationId",
        min_length=1,
        description="Logical invocation ID stable across checkpoint resume",
    )
    parent_graph_namespace: tuple[str, ...] = Field(
        alias="parentGraphNamespace",
        description="Complete graph namespace that issued the parent task Tool",
    )
    agent_name: str = Field(
        alias="agentName",
        min_length=1,
        description="Validated Deep Agents subagent type",
    )
    parent_tool_call_id: str = Field(
        alias="parentToolCallId",
        min_length=1,
        description="Namespace-scoped parent task Tool call ID",
    )
    description: str = Field(
        min_length=1,
        description="Task description supplied to the subagent",
    )
    request_run_id: str = Field(
        alias="requestRunId",
        min_length=1,
        description="Main AG-UI request currently carrying this event",
    )

    @field_validator("parent_graph_namespace")
    @classmethod
    def namespace_parts_are_canonical(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Reject empty or whitespace-padded graph namespace components."""

        if any(not part or part != part.strip() for part in value):
            raise ValueError("subagent namespaces require canonical components")
        return value

    @model_validator(mode="after")
    def native_relationships_are_consistent(self) -> SubagentProvenance:
        """Cross-check the logical request's complete parent Tool scope."""

        kind, parent_tool_namespace, _raw_id = ScopedIdCodec().decode(
            self.parent_tool_call_id
        )
        if kind != "tool" or parent_tool_namespace != self.parent_graph_namespace:
            raise ValueError("parentToolCallId does not match parentGraphNamespace")
        return self


def subagent_invocation_id(
    *,
    identity: RunIdentity,
    parent_tool_call_id: str,
    subagent_id: str,
) -> str:
    """Return the canonical logical invocation ID.

    Args:
        identity: Current request identity; only its stable thread ID participates.
        parent_tool_call_id: Complete scoped parent `task` Tool call ID.
        subagent_id: Stable logical delegation identity supplied by the graph source.

    Returns:
        A `subagent-` prefixed UUID5 derived from the frozen canonical JSON array.

    Raises:
        TypeError: The identity is not the public Adapter identity model.
        ValueError: The parent ID is not a complete scoped Tool ID.
    """

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be a RunIdentity")
    if not isinstance(subagent_id, str):
        raise TypeError("subagent_id must be a canonical string")
    if not subagent_id or subagent_id != subagent_id.strip():
        raise ValueError("subagent_id must be a canonical non-empty identity")
    try:
        kind, _namespace, _raw_id = ScopedIdCodec().decode(parent_tool_call_id)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "parent_tool_call_id must identify a scoped Tool call"
        ) from error
    if kind != "tool":
        raise ValueError("parent_tool_call_id must identify a scoped Tool call")
    canonical = json.dumps(
        [
            SUBAGENT_PROVENANCE_SCHEMA,
            identity.thread_id,
            subagent_id,
            parent_tool_call_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"subagent-{uuid5(NAMESPACE_URL, canonical)}"


def create_subagent_provenance(
    *,
    identity: RunIdentity,
    parent_graph_namespace: tuple[str, ...],
    subagent_id: str,
    agent_name: str,
    parent_tool_call_id: str,
    description: str,
) -> SubagentProvenance:
    """Create one validated public provenance value for the current request."""

    return SubagentProvenance(
        schema=SUBAGENT_PROVENANCE_SCHEMA,
        subagentInvocationId=subagent_invocation_id(
            identity=identity,
            parent_tool_call_id=parent_tool_call_id,
            subagent_id=subagent_id,
        ),
        parentGraphNamespace=parent_graph_namespace,
        agentName=agent_name,
        parentToolCallId=parent_tool_call_id,
        description=description,
        requestRunId=identity.run_id,
    )


__all__ = [
    "SUBAGENT_PROVENANCE_SCHEMA",
    "SubagentProvenance",
    "create_subagent_provenance",
    "subagent_invocation_id",
]
