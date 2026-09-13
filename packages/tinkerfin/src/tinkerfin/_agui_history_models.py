"""Immutable AG-UI conversation history returned by the Runtime integration."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from ag_ui.core import Interrupt
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from tinkerfin_tracing import (
    TraceEntityDelta,
    TraceGraph,
    TraceGraphDelta,
    TraceGraphNode,
    TraceGraphPage,
    TraceInteraction,
    TraceMessage,
    TraceReasoning,
    TraceState,
    TraceSummary,
    TraceUpdate,
)


class _HistoryModel(BaseModel, frozen=True):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class AgUiMessageReference(_HistoryModel, frozen=True):
    """Identify a retained message in live AG-UI message events and snapshots."""

    kind: Literal["message"] = "message"
    message_id: str = Field(min_length=1)


class AgUiToolMessageReference(_HistoryModel, frozen=True):
    """Identify a Tool result message and the call whose result it carries."""

    kind: Literal["tool_message"] = "tool_message"
    message_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)


class AgUiToolReference(_HistoryModel, frozen=True):
    """Identify one Tool proposal across live delivery and checkpoint resume."""

    kind: Literal["tool"] = "tool"
    tool_call_id: str = Field(min_length=1)


class AgUiSubagentReference(_HistoryModel, frozen=True):
    """Identify a logical delegate and the parent Tool call that opened it."""

    kind: Literal["subagent"] = "subagent"
    parent_tool_call_id: str = Field(min_length=1)
    subagent_invocation_id: str = Field(min_length=1)


class AgUiTraceMessage(TraceMessage, frozen=True):
    """Retain a native message with its live reference when source IDs are known."""

    agui: (
        Annotated[
            AgUiMessageReference | AgUiToolMessageReference, Field(discriminator="kind")
        ]
        | None
    )


class AgUiTraceGraphNode(TraceGraphNode, frozen=True):
    """Retain a graph node with references for verified Tools and Subagents."""

    agui: (
        Annotated[
            AgUiToolReference | AgUiSubagentReference, Field(discriminator="kind")
        ]
        | None
    )


class AgUiTraceInteraction(TraceInteraction, frozen=True):
    """Retain human input with public actions, or null when capture is insufficient.

    Pending actions describe the retained request. Settled interactions export an
    empty tuple because their payload records the decision, not the request.
    Tool approval needs full inline argument capture for every action; selected
    content remains display data and is never interpreted as complete arguments.
    Exported actions do not authorize resume or replace checkpoint validation.
    """

    agui: tuple[Interrupt, ...] | None


class AgUiTraceGraph(TraceGraph, frozen=True):
    """Export an ordered graph with AG-UI references attached to its nodes."""

    nodes: tuple[AgUiTraceGraphNode, ...] = ()


class AgUiTraceGraphPage(TraceGraphPage, frozen=True):
    """Export one fixed-prefix graph page without altering its cursor."""

    nodes: tuple[AgUiTraceGraphNode, ...] = ()


class AgUiTraceGraphDelta(TraceGraphDelta, frozen=True):
    """Export changed nodes with references removed by the existing node IDs."""

    node_upserts: tuple[AgUiTraceGraphNode, ...] = ()


class AgUiTraceSummary(TraceSummary, frozen=True):
    """Retain cumulative execution status and exported pending human input."""

    pending_interactions: tuple[AgUiTraceInteraction, ...] = ()


class AgUiTraceUpdate(TraceUpdate, frozen=True):
    """Export one committed update without adding a separate identity delta."""

    messages: TraceEntityDelta[AgUiTraceMessage]
    graph: AgUiTraceGraphDelta
    interactions: TraceEntityDelta[AgUiTraceInteraction]
    summary: AgUiTraceSummary


class AgUiTraceHistory(_HistoryModel, frozen=True):
    """Export the loaded history entities with live protocol references.

    Reasoning references messages by their retained Trace ID. The snapshot keeps
    its original thread, branch, generation, observation time, and history cursor.
    """

    namespace: str
    thread_id: str
    generation: str
    as_of_seq: int
    observed_at: datetime
    head_run_id: str
    available_heads: tuple[str, ...]
    history_cursor: str | None
    state: TraceState
    messages: tuple[AgUiTraceMessage, ...]
    reasoning: tuple[TraceReasoning, ...]
    graph: AgUiTraceGraph
    interactions: tuple[AgUiTraceInteraction, ...]
    summary: AgUiTraceSummary
