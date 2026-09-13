"""AG-UI views retain precise references and satisfy native immutable contracts."""

from typing import assert_type

from tinkerfin.agui import (
    AgUiTraceGraph,
    AgUiTraceGraphNode,
    AgUiTraceGraphPage,
    AgUiTraceInteraction,
    AgUiTraceMessage,
    AgUiTraceUpdate,
)
from tinkerfin_tracing import (
    TraceEntityDelta,
    TraceGraph,
    TraceGraphPage,
    TraceInteraction,
    TraceMessage,
    TraceUpdate,
)


def native_graph(graph: AgUiTraceGraph) -> TraceGraph:
    assert_type(graph.nodes, tuple[AgUiTraceGraphNode, ...])
    return graph


def native_page(page: AgUiTraceGraphPage) -> TraceGraphPage:
    assert_type(page.nodes, tuple[AgUiTraceGraphNode, ...])
    return page


def native_update(update: AgUiTraceUpdate) -> TraceUpdate:
    assert_type(update.messages, TraceEntityDelta[AgUiTraceMessage])
    assert_type(update.interactions, TraceEntityDelta[AgUiTraceInteraction])
    return update


def native_messages(
    changes: TraceEntityDelta[AgUiTraceMessage],
) -> TraceEntityDelta[TraceMessage]:
    return changes


def native_interactions(
    changes: TraceEntityDelta[AgUiTraceInteraction],
) -> TraceEntityDelta[TraceInteraction]:
    return changes
