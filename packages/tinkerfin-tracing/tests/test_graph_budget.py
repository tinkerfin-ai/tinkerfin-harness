"""Graph byte limits preserve the meaning and validity of retained nodes."""

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from tinkerfin_tracing import (
    TraceGraph,
    TraceGraphDelta,
    TraceGraphNode,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphPage,
    TraceGraphTurn,
)
from tinkerfin_tracing.graph import bound_graph, bound_graph_page, graph_delta


@pytest.mark.parametrize("content", ["", [], None])
@pytest.mark.parametrize("boundary", ["history", "page", "update"])
def test_omitting_details_preserves_observed_tool_only_content(
    content: JsonValue, boundary: str
) -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    assistant = TraceGraphNode(
        id="assistant",
        turn_id="turn",
        kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
        status=TraceGraphNodeStatus.SUCCEEDED,
        name="AssistantMessage",
        run_id="run",
        started_at=now,
        completed_at=now,
        started_seq=1,
        updated_seq=1,
        content=content,
        tool_call_only=True,
        response_metadata={"public_metadata": "x" * 2048},
    )
    graph = TraceGraph(
        turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=now),),
        nodes=(assistant,),
        ordered_node_ids=(assistant.id,),
        matched_node_ids=(assistant.id,),
        as_of_seq=1,
    )
    if boundary == "history":
        limited = bound_graph(graph, max_bytes=2048)
        restored = TraceGraph.model_validate_json(limited.model_dump_json())
    elif boundary == "page":
        page = TraceGraphPage(**graph.model_dump(), next_cursor=None)
        limited = bound_graph_page(page, max_bytes=2048)
        restored = TraceGraphPage.model_validate_json(limited.model_dump_json())
    else:
        limited = graph_delta(
            TraceGraph(as_of_seq=1, matched_node_ids=()), graph, max_bytes=2048
        )
        restored = TraceGraphDelta.model_validate_json(limited.model_dump_json())

    nodes = (
        restored.node_upserts
        if isinstance(restored, TraceGraphDelta)
        else restored.nodes
    )
    assert len(nodes) == 1
    assert nodes[0].tool_call_only
    assert not nodes[0].content_omitted
    assert nodes[0].content == content
    assert nodes[0].response_metadata is None
    assert len(limited.model_dump_json(by_alias=True).encode()) <= 2048
