"""Explicit graph ancestry and logical delegation identities for observers."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from pydantic import Field, ValidationInfo, field_validator

from ._models import ContractModel


class _GraphReference(ContractModel):
    @field_validator("*", mode="after")
    @classmethod
    def canonical_references(cls, value: object, info: ValidationInfo) -> object:
        if info.field_name not in {
            "id",
            "task_id",
            "node_name",
            "parent_tool_call_id",
            "graph_task_id",
            "graph_namespace",
            "parent_graph_namespace",
        }:
            return value
        if isinstance(value, str) and (not value or value != value.strip()):
            raise ValueError("graph references require canonical non-empty strings")
        if isinstance(value, tuple):
            value = cast(tuple[object, ...], value)
            if any(
                not isinstance(part, str) or not part or part != part.strip()
                for part in value
            ):
                raise ValueError("graph namespaces require canonical components")
        return value


class GraphTaskReference(_GraphReference):
    """Identify the actual Native task that opened a physical graph scope."""

    graph_namespace: tuple[str, ...] = Field(
        description="Native task event scope, which can differ from its child's immediate physical parent"
    )
    task_id: str = Field(min_length=1)
    node_name: str = Field(min_length=1)


class SubagentRequestReference(_GraphReference):
    """Identify one delegation independently of its attempts and child scopes.

    The original Graph task anchor is retained across checkpoint resume. Actual
    child Graph tasks never replace this identity or the parent Tool scope.
    """

    id: str = Field(min_length=1)
    parent_graph_namespace: tuple[str, ...]
    parent_tool_call_id: str = Field(min_length=1)
    graph_task_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class GraphOrigin(_GraphReference):
    """Keep physical graph ancestry separate from the owning logical delegate."""

    parent_task: GraphTaskReference | None = None
    subagent_request: SubagentRequestReference | None = None


def subagent_request_id(task_namespace: tuple[str, ...]) -> str:
    """Return the stable Subagent node ID of an original delegation task scope.

    Args:
        task_namespace: Proven complete scope of the original delegating Graph task.

    Returns:
        The same opaque node identity for every attempt and resumed execution.

    Raises:
        ValueError: The task scope is empty or contains non-canonical components.
    """

    if not task_namespace or any(
        not part or part != part.strip() for part in task_namespace
    ):
        raise ValueError("delegation identity requires a canonical task namespace")
    encoded = json.dumps(
        list(task_namespace), ensure_ascii=False, separators=(",", ":")
    ).encode()
    scope = hashlib.sha256(encoded).hexdigest()[:24]
    return f"subagent:{scope}:{task_namespace[-1]}"


__all__ = [
    "GraphOrigin",
    "GraphTaskReference",
    "SubagentRequestReference",
    "subagent_request_id",
]
