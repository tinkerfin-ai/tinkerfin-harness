"""Bind retained Trace evidence to the same identities used by live AG-UI."""

from __future__ import annotations

from ag_ui.core import Interrupt
from pydantic import JsonValue

from tinkerfin_agui_adapter import (
    AgentRuntimeInterrupt,
    ScopedIdCodec,
    project_interrupt,
    subagent_invocation_id,
)
from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    CapturedValue,
    TraceEntityDelta,
    TraceGraphDelta,
    TraceGraphNode,
    TraceGraphPage,
    TraceInteraction,
    TraceMessage,
    TraceStoreProtocolError,
    TraceSummary,
    TraceThread,
    TraceUpdate,
)

from ._agui_history_models import (
    AgUiMessageReference,
    AgUiSubagentReference,
    AgUiToolMessageReference,
    AgUiToolReference,
    AgUiTraceGraph,
    AgUiTraceGraphDelta,
    AgUiTraceGraphNode,
    AgUiTraceGraphPage,
    AgUiTraceHistory,
    AgUiTraceInteraction,
    AgUiTraceMessage,
    AgUiTraceSummary,
    AgUiTraceUpdate,
)


def _message(message: TraceMessage) -> AgUiTraceMessage:
    reference: AgUiMessageReference | AgUiToolMessageReference | None = None
    if message.source_id is not None:
        message_id = ScopedIdCodec().encode(
            "message", message.graph_namespace, message.source_id
        )
        if message.role == "tool":
            if message.tool_call_id is not None:
                reference = AgUiToolMessageReference(
                    message_id=message_id,
                    tool_call_id=ScopedIdCodec().encode(
                        "tool", message.graph_namespace, message.tool_call_id
                    ),
                )
        else:
            reference = AgUiMessageReference(message_id=message_id)
    return AgUiTraceMessage(**message.model_dump(exclude={"agui"}), agui=reference)


def _node(node: TraceGraphNode, identity: ThreadIdentity) -> AgUiTraceGraphNode:
    reference: AgUiToolReference | AgUiSubagentReference | None = None
    if node.source_id is not None and node.kind == "tool":
        reference = AgUiToolReference(
            tool_call_id=ScopedIdCodec().encode(
                "tool", node.graph_namespace, node.source_id
            )
        )
    elif node.source_id is not None and node.kind == "subagent":
        if not node.graph_namespace:
            raise TraceStoreProtocolError("Subagent references require a child scope")
        # Trace Subagent source_id is its parent Tool ID; the node itself uses the
        # child scope. Keep this projection paired with Adapter provenance tests.
        parent_tool_call_id = ScopedIdCodec().encode(
            "tool", node.graph_namespace[:-1], node.source_id
        )
        reference = AgUiSubagentReference(
            parent_tool_call_id=parent_tool_call_id,
            subagent_invocation_id=subagent_invocation_id(
                identity=RunIdentity(
                    namespace=identity.namespace,
                    thread_id=identity.thread_id,
                    run_id=node.run_id,
                ),
                parent_tool_call_id=parent_tool_call_id,
            ),
        )
    return AgUiTraceGraphNode(**node.model_dump(exclude={"agui"}), agui=reference)


def _interaction(interaction: TraceInteraction) -> AgUiTraceInteraction:
    actions: tuple[Interrupt, ...] | None = None
    payload = interaction.payload
    if interaction.status != "pending":
        return AgUiTraceInteraction(**interaction.model_dump(exclude={"agui"}), agui=())
    if not interaction.payload_omitted:
        if interaction.kind == "tool_approval":
            payload = _review_payload(payload)
        if payload is not None or interaction.kind != "tool_approval":
            actions = project_interrupt(
                AgentRuntimeInterrupt(id=interaction.source_id, value=payload),
                tool_call_ids=tuple(
                    ScopedIdCodec().encode("tool", interaction.graph_namespace, tool_id)
                    for tool_id in interaction.tool_call_ids
                ),
                source={"graphNamespace": list(interaction.graph_namespace)},
            )
    return AgUiTraceInteraction(
        **interaction.model_dump(exclude={"agui"}), agui=actions
    )


def _review_payload(payload: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("action_requests"), list
    ):
        raise TraceStoreProtocolError("Retained Tool approval requires ordered actions")
    raw_actions = payload["action_requests"]
    assert isinstance(raw_actions, list)
    actions: list[JsonValue] = []
    available = True
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict) or "arguments" not in raw_action:
            raise TraceStoreProtocolError(
                "Retained Tool approval arguments are missing"
            )
        # The capture policy records this mode at write time. Selected values
        # are JSON Pointer maps, so their shape cannot prove complete Tool args.
        retention = raw_action.get("arguments_retention")
        if retention not in ("full", "selected", "none"):
            raise TraceStoreProtocolError(
                "Retained Tool approval requires an explicit argument retention mode"
            )
        captured = CapturedValue.model_validate(raw_action["arguments"])
        available = (
            available and retention == "full" and captured.disposition == "inline"
        )
        action = {
            key: value
            for key, value in raw_action.items()
            if key not in ("arguments", "arguments_retention")
        }
        action["args"] = captured.value
        actions.append(action)
    return {**payload, "action_requests": actions} if available else None


def _summary(summary: TraceSummary) -> AgUiTraceSummary:
    return AgUiTraceSummary(
        **summary.model_dump(exclude={"pending_interactions"}),
        pending_interactions=tuple(
            _interaction(item) for item in summary.pending_interactions
        ),
    )


def _project_history(trace: TraceThread) -> AgUiTraceHistory:
    """Export loaded history for use alongside the same thread's live AG-UI stream.

    Args:
        trace: Borrowed fixed-prefix Trace handle. This function does not query,
            advance, close, or take ownership of the handle or its Store.

    Returns:
        Exported entities with unchanged native IDs and capture boundaries.
        Reasoning references the corresponding entity in ``messages`` by Trace ID.

    Raises:
        TraceStoreProtocolError: Retained source relationships are malformed.
        ValueError: Retained interrupt data violates its public protocol contract.
    """

    identity = ThreadIdentity(
        namespace=trace.key.namespace, thread_id=trace.key.thread_id
    )
    graph = trace.graph
    return AgUiTraceHistory(
        namespace=trace.key.namespace,
        thread_id=trace.key.thread_id,
        generation=trace.key.generation,
        as_of_seq=trace.as_of_seq,
        observed_at=trace.observed_at,
        head_run_id=trace.head_run_id,
        available_heads=trace.available_heads,
        history_cursor=trace.history_cursor,
        state=trace.state,
        messages=tuple(_message(message) for message in trace.messages),
        reasoning=trace.reasoning,
        graph=AgUiTraceGraph(
            **graph.model_dump(exclude={"nodes"}),
            nodes=tuple(_node(node, identity) for node in graph.nodes),
        ),
        interactions=tuple(_interaction(item) for item in trace.interactions),
        summary=_summary(trace.summary),
    )


def _project_update(
    update: TraceUpdate, *, identity: ThreadIdentity
) -> AgUiTraceUpdate:
    """Export an entity update using the identity of the followed Trace thread.

    Args:
        update: Existing committed Trace update, including same-sequence status changes.
        identity: Thread to which the query or follow subscription belongs.

    Returns:
        The same update with references on upserted entities. Existing remove IDs,
        sequence boundaries, facts, and execution status are preserved.

    Raises:
        TraceStoreProtocolError: Retained source relationships are malformed.
        ValueError: Retained interrupt data violates its public protocol contract.
    """

    return AgUiTraceUpdate(
        **update.model_dump(
            exclude={
                "messages",
                "graph",
                "interactions",
                "summary",
                "status",
                "completeness",
                "message_count",
                "tool_call_count",
            }
        ),
        messages=TraceEntityDelta[AgUiTraceMessage](
            upserts=tuple(_message(item) for item in update.messages.upserts),
            removes=update.messages.removes,
        ),
        graph=_project_graph_delta(update.graph, identity=identity),
        interactions=TraceEntityDelta[AgUiTraceInteraction](
            upserts=tuple(_interaction(item) for item in update.interactions.upserts),
            removes=update.interactions.removes,
        ),
        summary=_summary(update.summary),
    )


def _project_graph_page(
    page: TraceGraphPage, *, identity: ThreadIdentity
) -> AgUiTraceGraphPage:
    """Export a graph query page without changing pagination or parent relationships.

    Args:
        page: One validated graph page from the existing Trace query.
        identity: Thread selected by that query.

    Returns:
        Nodes with live references and the original cursor and ordering.

    Raises:
        TraceStoreProtocolError: A Subagent source has no child scope.
    """

    return AgUiTraceGraphPage(
        **page.model_dump(exclude={"nodes"}),
        nodes=tuple(_node(node, identity) for node in page.nodes),
    )


def _project_graph_delta(
    delta: TraceGraphDelta, *, identity: ThreadIdentity
) -> AgUiTraceGraphDelta:
    """Export changed graph nodes while preserving all existing remove operations.

    Args:
        delta: A committed graph update from an existing query or follow.
        identity: Thread selected by that query.

    Returns:
        Upserted nodes with references and unchanged removal and ordering fields.

    Raises:
        TraceStoreProtocolError: A Subagent source has no child scope.
    """

    return AgUiTraceGraphDelta(
        **delta.model_dump(exclude={"node_upserts"}),
        node_upserts=tuple(_node(node, identity) for node in delta.node_upserts),
    )


__all__ = [
    "_project_graph_delta",
    "_project_graph_page",
    "_project_history",
    "_project_update",
]
