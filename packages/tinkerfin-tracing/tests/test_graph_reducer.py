"""Flat timeline reduction, scope ordering, and locator ownership contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from pydantic import JsonValue, ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    ContextContributionFact,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    PlanRevisionFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceGraph,
    TraceGraphCompleteness,
    TraceGraphFailure,
    TraceGraphNode,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphTurn,
    TraceSemanticFact,
    TurnFact,
)
from tinkerfin_tracing._graph_projection import (
    project_trace_graph_node,
    project_trace_graph_records,
    reduce_trace_graph_records,
)
from tinkerfin_tracing._graph_reducer import (
    graph_node_mutations,
    reduce_graph_mutations,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.errors import TraceStoreProtocolError
from tinkerfin_tracing.facts import TraceEvent

NOW = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
IDENTITY = RunIdentity(namespace="test", thread_id="thread", run_id="run")


class _CommonFactArgs(TypedDict):
    source_observation_id: str
    identity: RunIdentity
    graph_namespace: tuple[str, ...]
    occurred_at: datetime
    monotonic_ns: int


def _captured(value: JsonValue) -> CapturedValue:
    import json

    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ),
        value=value,
    )


def _common(
    sequence: int,
    *,
    namespace: tuple[str, ...] = (),
) -> _CommonFactArgs:
    return {
        "source_observation_id": f"observation-{sequence}",
        "identity": IDENTITY,
        "graph_namespace": namespace,
        "occurred_at": NOW + timedelta(milliseconds=sequence),
        "monotonic_ns": sequence,
    }


def _event(sequence: int, fact: TraceSemanticFact) -> TraceEvent:
    return TraceEvent.model_validate(
        {
            "eventId": f"event-{sequence}",
            "traceSeq": sequence,
            "generation": "generation",
            "fact": fact,
            "persistedBytes": 1,
        }
    )


def test_turn_fact_is_the_only_root_human_creator() -> None:
    user_id = scope_id("message", (), "user")
    events = (
        _event(
            1,
            TurnFact(
                **_common(1),
                turn_id="turn",
                user_message_id="user",
            ),
        ),
        _event(
            2,
            MessageFact(
                **_common(2),
                phase="reconciled",
                message_id=user_id,
                source_message_id="user",
                role="user",
                content=_captured("real input"),
            ),
        ),
        _event(
            3,
            MessageFact(
                **_common(3),
                phase="reconciled",
                message_id=scope_id("message", (), "internal"),
                source_message_id="internal",
                role="user",
                content=_captured("internal input"),
            ),
        ),
    )

    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))

    humans = [
        record for record in records if record.kind is TraceGraphNodeKind.HUMAN_MESSAGE
    ]
    assert [record.node_id for record in humans] == [user_id]
    assert humans[0].result_seq == 2


def test_turn_without_user_input_does_not_create_a_human_message() -> None:
    events = (_event(1, TurnFact(**_common(1), turn_id="maintenance")),)
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    assert not records


def test_model_tool_and_assistant_are_flat_siblings_with_explicit_model_links() -> None:
    model_id = "model-call"
    context_id = scope_id("context", (), model_id)
    tool_id = scope_id("tool", (), "tool-call")
    assistant_id = scope_id("message", (), "assistant")
    events = (
        _event(
            1,
            TurnFact(**_common(1), turn_id="turn", user_message_id="user"),
        ),
        _event(
            2,
            ModelCallFact(
                **_common(2),
                phase="started",
                context_started_at=NOW + timedelta(milliseconds=1),
                call_id=model_id,
                request=_captured({"messages": []}),
                system_message_positions=(),
                output_message_ids=(),
            ),
        ),
        _event(
            3,
            ModelCallFact(
                **_common(3),
                phase="completed",
                call_id=model_id,
                system_message_positions=(),
                output_message_ids=("assistant",),
                tool_call_ids=("tool-call",),
            ),
        ),
        _event(
            4,
            ToolFact(
                **_common(4),
                phase="started",
                tool_call_id=tool_id,
                source_tool_call_id="tool-call",
                parent_call_id=model_id,
                tool_name="read_file",
            ),
        ),
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    turns = (TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),)
    nodes, ordered = project_trace_graph_records(
        records,
        turns=turns,
        run_turns={"run": "turn"},
        selected_run_ids=frozenset({"run"}),
    )
    by_id = {node.id: node for node in nodes}

    assert by_id[model_id].parent_subagent_id is None
    assert by_id[context_id].parent_subagent_id is None
    assert by_id[context_id].started_at == NOW + timedelta(milliseconds=1)
    assert by_id[context_id].completed_at == NOW + timedelta(milliseconds=2)
    assert by_id[tool_id].parent_subagent_id is None
    assert by_id[assistant_id].parent_subagent_id is None
    assert by_id[tool_id].model_call_id == model_id
    assert by_id[assistant_id].model_call_id == model_id
    assert by_id[assistant_id].tool_call_only is True
    assert (
        ordered.index(context_id)
        < ordered.index(model_id)
        < ordered.index(assistant_id)
        < ordered.index(tool_id)
    )


def test_tool_call_only_is_restricted_to_observed_assistant_content() -> None:
    with pytest.raises(ValueError, match="AssistantMessage"):
        TraceGraphNode(
            id="tool",
            turn_id="turn",
            kind=TraceGraphNodeKind.TOOL,
            status=TraceGraphNodeStatus.SUCCEEDED,
            name="read_file",
            run_id="run",
            started_at=NOW,
            completed_at=NOW,
            started_seq=1,
            updated_seq=1,
            content_omitted=False,
            tool_call_only=True,
            request_omitted=False,
            result_omitted=False,
        )

    with pytest.raises(ValidationError, match="failed event status"):
        TraceGraphNode(
            id="model",
            turn_id="turn",
            kind=TraceGraphNodeKind.MODEL,
            status=TraceGraphNodeStatus.SUCCEEDED,
            name="model",
            run_id="run",
            started_at=NOW,
            completed_at=NOW,
            started_seq=1,
            updated_seq=1,
            failure=TraceGraphFailure(error_type="provider_error"),
            content_omitted=False,
            request_omitted=False,
            result_omitted=False,
        )

    with pytest.raises(ValueError, match="observed"):
        TraceGraphNode(
            id="assistant",
            turn_id="turn",
            kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
            status=TraceGraphNodeStatus.SUCCEEDED,
            name="AssistantMessage",
            run_id="run",
            started_at=NOW,
            completed_at=NOW,
            started_seq=1,
            updated_seq=1,
            content_omitted=True,
            tool_call_only=True,
            request_omitted=False,
            result_omitted=False,
        )


def test_task_tool_produces_only_one_subagent_with_an_input_child() -> None:
    namespace = ("tools:native-task",)
    subagent_id = scope_id("subagent", namespace, namespace[-1])
    events = (
        _event(
            1,
            ToolFact(
                **_common(1),
                phase="started",
                tool_call_id=scope_id("tool", (), "task-call"),
                source_tool_call_id="task-call",
                parent_call_id="model-call",
                tool_name="task",
            ),
        ),
        _event(
            2,
            ToolExecutionFact(
                **_common(2),
                phase="started",
                execution_id="task-execution",
                parent_call_id="model-call",
                source_tool_call_id="task-call",
                tool_name="task",
                input=_captured({"description": "inspect"}),
            ),
        ),
        _event(
            3,
            SubagentFact(
                **_common(3, namespace=namespace),
                phase="started",
                subagent_id=subagent_id,
                agent_name="researcher",
                parent_tool_call_id="task-call",
                parent_execution_id="task-execution",
                model_call_id="model-call",
                input=_captured({"description": "inspect"}),
                status="running",
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    assert not any(
        mutation.kind is TraceGraphNodeKind.TOOL and mutation.name == "task"
        for mutation in mutations
    )
    subagents = [
        mutation
        for mutation in mutations
        if mutation.kind is TraceGraphNodeKind.SUBAGENT
    ]
    assert len(subagents) == 1
    assert subagents[0].node_id == subagent_id
    assert subagents[0].model_call_id == "model-call"
    input_nodes = [
        mutation
        for mutation in mutations
        if mutation.kind is TraceGraphNodeKind.HUMAN_MESSAGE
    ]
    assert len(input_nodes) == 1
    assert input_nodes[0].parent_subagent_id == subagent_id


def test_nested_subagent_scope_uses_only_nearest_subagent_owner() -> None:
    outer_namespace = ("tools:outer",)
    inner_namespace = (*outer_namespace, "tools:inner")
    outer_id = scope_id("subagent", outer_namespace, outer_namespace[-1])
    inner_id = scope_id("subagent", inner_namespace, inner_namespace[-1])
    events = (
        _event(
            1,
            SubagentFact(
                **_common(1, namespace=outer_namespace),
                phase="started",
                subagent_id=outer_id,
                agent_name="outer",
                parent_tool_call_id="outer-call",
                status="running",
            ),
        ),
        _event(
            2,
            SubagentFact(
                **_common(2, namespace=inner_namespace),
                phase="started",
                subagent_id=inner_id,
                agent_name="inner",
                parent_tool_call_id="inner-call",
                status="running",
            ),
        ),
    )
    mutations = graph_node_mutations(events)
    inner = next(mutation for mutation in mutations if mutation.node_id == inner_id)

    assert inner.parent_subagent_id == outer_id


def test_tool_execution_start_replaces_proposal_time_and_interrupt_has_no_completion() -> (
    None
):
    tool_id = scope_id("tool", (), "call")
    events = (
        _event(
            1,
            ToolFact(
                **_common(1),
                phase="started",
                tool_call_id=tool_id,
                source_tool_call_id="call",
                parent_call_id="model",
                tool_name="read_file",
            ),
        ),
        _event(
            2,
            ToolExecutionFact(
                **_common(2),
                phase="started",
                execution_id="execution",
                parent_call_id="model",
                source_tool_call_id="call",
                tool_name="read_file",
                input=_captured({"file_path": "/tmp/a"}),
            ),
        ),
        _event(
            3,
            ToolExecutionFact(
                **_common(3),
                phase="interrupted",
                execution_id="execution",
                parent_call_id="model",
                source_tool_call_id="call",
                tool_name="read_file",
            ),
        ),
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    tool = next(record for record in records if record.node_id == tool_id)

    assert tool.started_seq == 2
    assert tool.started_at == events[1].fact.occurred_at
    assert tool.status is TraceGraphNodeStatus.WAITING
    assert tool.completed_at is None


def test_rebuild_reduction_preserves_scope_and_model_relations() -> None:
    events = (
        _event(
            1,
            ToolFact(
                **_common(1, namespace=("tools:parent",)),
                in_subagent_scope=True,
                phase="started",
                tool_call_id=scope_id("tool", ("tools:parent",), "call"),
                source_tool_call_id="call",
                parent_call_id="model",
                tool_name="read_file",
            ),
        ),
    )
    mutations = graph_node_mutations(events)
    reduced = reduce_graph_mutations(
        mutations, source_events={event.trace_seq: event for event in events}
    )

    assert len(reduced) == 1
    assert reduced[0].parent_subagent_id == scope_id(
        "subagent", ("tools:parent",), "tools:parent"
    )
    assert reduced[0].model_call_id == "model"


def _subagent_node(
    index: int,
    *,
    parent: str | None,
    namespace_depth: int | None = None,
) -> TraceGraphNode:
    timestamp = NOW + timedelta(milliseconds=index)
    return TraceGraphNode(
        id=f"subagent-{index}",
        turn_id="turn",
        parent_subagent_id=parent,
        kind=TraceGraphNodeKind.SUBAGENT,
        status=TraceGraphNodeStatus.SUCCEEDED,
        name=f"subagent-{index}",
        run_id="run",
        graph_namespace=tuple(
            f"tools:{value}"
            for value in range(
                namespace_depth if namespace_depth is not None else index + 1
            )
        ),
        started_at=timestamp,
        completed_at=timestamp,
        started_seq=index + 1,
        updated_seq=index + 1,
        content_omitted=False,
        request_omitted=False,
        result_omitted=False,
        link_issues=(),
    )


def test_subagent_scope_accepts_64_levels_without_language_recursion() -> None:
    nodes: list[TraceGraphNode] = []
    parent: str | None = None
    for index in range(64):
        node = _subagent_node(index, parent=parent)
        nodes.append(node)
        parent = node.id
    graph = TraceGraph(
        turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
        nodes=tuple(nodes),
        ordered_node_ids=tuple(node.id for node in nodes),
        matched_node_ids=tuple(node.id for node in nodes),
        as_of_seq=64,
        completeness=TraceGraphCompleteness(),
    )

    assert len(graph.nodes) == 64


def test_flat_scope_orders_the_four_thousand_node_store_boundary_iteratively() -> None:
    nodes = tuple(
        TraceGraphNode(
            id=f"context-{index:04d}",
            turn_id="turn",
            kind=TraceGraphNodeKind.CUSTOM,
            status=TraceGraphNodeStatus.SUCCEEDED,
            name="Context",
            run_id="run",
            started_at=NOW + timedelta(microseconds=index),
            completed_at=NOW + timedelta(microseconds=index),
            started_seq=index + 1,
            updated_seq=index + 1,
            content_omitted=False,
            request_omitted=False,
            result_omitted=False,
            link_issues=(),
        )
        for index in range(4000)
    )
    graph = TraceGraph(
        turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
        nodes=nodes,
        ordered_node_ids=tuple(node.id for node in nodes),
        matched_node_ids=tuple(node.id for node in nodes),
        as_of_seq=4000,
        completeness=TraceGraphCompleteness(),
    )

    assert len(graph.nodes) == 4000


def test_subagent_scope_accepts_a_leaf_inside_level_64() -> None:
    nodes: list[TraceGraphNode] = []
    parent: str | None = None
    for index in range(64):
        node = _subagent_node(index, parent=parent)
        nodes.append(node)
        parent = node.id
    assistant = TraceGraphNode(
        id="assistant-at-limit",
        turn_id="turn",
        parent_subagent_id=parent,
        kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
        status=TraceGraphNodeStatus.SUCCEEDED,
        name="AssistantMessage",
        run_id="run",
        graph_namespace=tuple(f"tools:{value}" for value in range(64)),
        started_at=NOW + timedelta(milliseconds=64),
        completed_at=NOW + timedelta(milliseconds=64),
        started_seq=65,
        updated_seq=65,
        content_omitted=False,
        request_omitted=False,
        result_omitted=False,
        link_issues=(),
    )
    nodes.append(assistant)

    graph = TraceGraph(
        turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
        nodes=tuple(nodes),
        ordered_node_ids=tuple(node.id for node in nodes),
        matched_node_ids=tuple(node.id for node in nodes),
        as_of_seq=65,
        completeness=TraceGraphCompleteness(),
    )

    assert graph.nodes[-1].parent_subagent_id == "subagent-63"


def test_subagent_scope_rejects_more_than_64_levels() -> None:
    nodes: list[TraceGraphNode] = []
    parent: str | None = None
    for index in range(65):
        node = _subagent_node(
            index,
            parent=parent,
            namespace_depth=min(index + 1, 64),
        )
        nodes.append(node)
        parent = node.id

    with pytest.raises(ValidationError, match="64 levels"):
        TraceGraph(
            turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
            nodes=tuple(nodes),
            ordered_node_ids=tuple(node.id for node in nodes),
            matched_node_ids=tuple(node.id for node in nodes),
            as_of_seq=65,
            completeness=TraceGraphCompleteness(),
        )


def test_subagent_scope_rejects_a_cycle() -> None:
    first = _subagent_node(0, parent="subagent-1")
    second = _subagent_node(1, parent="subagent-0")

    with pytest.raises(ValidationError, match="cannot form a cycle"):
        TraceGraph(
            turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
            nodes=(first, second),
            ordered_node_ids=(first.id, second.id),
            matched_node_ids=(first.id, second.id),
            as_of_seq=2,
            completeness=TraceGraphCompleteness(),
        )


def test_subagent_scope_rejects_a_non_subagent_parent() -> None:
    parent = TraceGraphNode(
        id="tool-parent",
        turn_id="turn",
        kind=TraceGraphNodeKind.TOOL,
        status=TraceGraphNodeStatus.SUCCEEDED,
        name="read_file",
        run_id="run",
        started_at=NOW,
        completed_at=NOW,
        started_seq=1,
        updated_seq=1,
        content_omitted=False,
        request_omitted=False,
        result_omitted=False,
        link_issues=(),
    )
    child = _subagent_node(1, parent=parent.id)

    with pytest.raises(ValidationError, match="returned Subagents"):
        TraceGraph(
            turns=(TraceGraphTurn(id="turn", ordinal=1, started_at=NOW),),
            nodes=(parent, child),
            ordered_node_ids=(parent.id, child.id),
            matched_node_ids=(parent.id, child.id),
            as_of_seq=2,
            completeness=TraceGraphCompleteness(),
        )


def test_locator_cannot_reuse_another_tool_result() -> None:
    events = (
        _event(
            1,
            ToolExecutionFact(
                **_common(1),
                phase="started",
                execution_id="a",
                source_tool_call_id="a",
                tool_name="read_file",
                input=_captured({"file_path": "a"}),
            ),
        ),
        _event(
            2,
            ToolExecutionFact(
                **_common(2),
                phase="completed",
                execution_id="b",
                source_tool_call_id="b",
                tool_name="read_file",
                output=_captured("b"),
            ),
        ),
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    record = next(record for record in records if record.node_id.endswith(":a"))
    corrupted = replace(record, result_seq=2, result_event=events[1])

    with pytest.raises(TraceStoreProtocolError, match="another call"):
        project_trace_graph_node(
            corrupted,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


def test_model_context_locator_cannot_reuse_another_model_request() -> None:
    events = tuple(
        _event(
            index,
            ModelCallFact(
                **_common(index),
                phase="started",
                context_started_at=NOW + timedelta(milliseconds=index - 1),
                call_id=f"model-{index}",
                request=_captured(
                    {
                        "messages": [
                            {
                                "messageType": "system",
                                "content": f"system-{index}",
                            }
                        ]
                    }
                ),
                system_message_positions=(0,),
                output_message_ids=(),
            ),
        )
        for index in (1, 2)
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    first = next(
        record
        for record in records
        if record.kind is TraceGraphNodeKind.CONTEXT and "model-1" in record.node_id
    )
    projected = project_trace_graph_node(
        first,
        turn_id="turn",
        parent_subagent_id=None,
        relationship_missing=False,
        allowed_run_ids=frozenset({"run"}),
    )
    assert projected.content == "system-1"
    assert projected.started_at == NOW
    assert projected.completed_at == NOW + timedelta(milliseconds=1)
    corrupted = replace(first, request_seq=2, request_event=events[1])

    with pytest.raises(TraceStoreProtocolError, match="another action"):
        project_trace_graph_node(
            corrupted,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


def test_model_result_locator_rejects_the_started_phase_of_the_same_call() -> None:
    events = (
        _event(
            1,
            ModelCallFact(
                **_common(1),
                phase="started",
                context_started_at=NOW,
                call_id="model",
                request=_captured({"messages": []}),
                system_message_positions=(),
                output_message_ids=(),
            ),
        ),
        _event(
            2,
            ModelCallFact(
                **_common(2),
                phase="completed",
                call_id="model",
                system_message_positions=(),
                output_message_ids=(),
            ),
        ),
    )
    record = next(
        item
        for item in reduce_trace_graph_records(
            events,
            run_ids=frozenset({"run"}),
        )
        if item.node_id == "model"
    )

    with pytest.raises(TraceStoreProtocolError, match="result locator"):
        project_trace_graph_node(
            replace(record, result_seq=1, result_event=events[0]),
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )

    for corrupted in (
        replace(record, status=TraceGraphNodeStatus.RUNNING, completed_at=None),
        replace(record, started_at=NOW),
        replace(record, completed_at=NOW + timedelta(seconds=1)),
    ):
        with pytest.raises(TraceStoreProtocolError, match="conflicts with"):
            project_trace_graph_node(
                corrupted,
                turn_id="turn",
                parent_subagent_id=None,
                relationship_missing=False,
                allowed_run_ids=frozenset({"run"}),
            )


def test_model_failure_locator_rejects_a_completed_phase_of_the_same_call() -> None:
    started = _event(
        1,
        ModelCallFact(
            **_common(1),
            phase="started",
            context_started_at=NOW,
            call_id="model",
            request=_captured({"messages": []}),
            system_message_positions=(),
            output_message_ids=(),
        ),
    )
    failed = _event(
        2,
        ModelCallFact(
            **_common(2),
            phase="failed",
            call_id="model",
            system_message_positions=(),
            output_message_ids=(),
            error_type="builtins.RuntimeError",
            failure_origin=True,
        ),
    )
    completed = _event(
        2,
        ModelCallFact(
            **_common(2),
            phase="completed",
            call_id="model",
            system_message_positions=(),
            output_message_ids=(),
        ),
    )
    record = next(
        item
        for item in reduce_trace_graph_records(
            (started, failed),
            run_ids=frozenset({"run"}),
        )
        if item.node_id == "model"
    )

    with pytest.raises(TraceStoreProtocolError, match="failure locator"):
        project_trace_graph_node(
            replace(record, failure_event=completed),
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


def test_context_locator_cannot_reuse_another_contribution() -> None:
    events = tuple(
        _event(
            index,
            ContextContributionFact(
                **_common(index),
                phase="started",
                contribution_id=f"context-{index}",
                context_kind="custom",
                name=f"context-{index}",
                input=_captured({"value": index}),
            ),
        )
        for index in (1, 2)
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    first = next(record for record in records if record.node_id == "context-1")
    corrupted = replace(first, request_seq=2, request_event=events[1])

    with pytest.raises(TraceStoreProtocolError, match="another event"):
        project_trace_graph_node(
            corrupted,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


def test_plan_locator_cannot_reuse_another_run_revision() -> None:
    first_identity = RunIdentity(namespace="test", thread_id="thread", run_id="plan-a")
    second_identity = RunIdentity(namespace="test", thread_id="thread", run_id="plan-b")
    events = (
        _event(
            1,
            PlanRevisionFact(
                source_observation_id="plan-a",
                identity=first_identity,
                occurred_at=NOW,
                monotonic_ns=1,
                revision_id="revision-a",
                revision=1,
                status="active",
                plan=_captured({"title": "a"}),
            ),
        ),
        _event(
            2,
            PlanRevisionFact(
                source_observation_id="plan-b",
                identity=second_identity,
                occurred_at=NOW + timedelta(milliseconds=1),
                monotonic_ns=2,
                revision_id="revision-b",
                revision=1,
                status="active",
                plan=_captured({"title": "b"}),
            ),
        ),
    )
    records = reduce_trace_graph_records(
        events,
        run_ids=frozenset({"plan-a", "plan-b"}),
    )
    first = next(record for record in records if record.run_id == "plan-a")
    corrupted = replace(first, result_seq=2, result_event=events[1])

    with pytest.raises(TraceStoreProtocolError, match="another event"):
        project_trace_graph_node(
            corrupted,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"plan-a", "plan-b"}),
        )


def test_interaction_locator_cannot_reuse_another_interaction() -> None:
    events = tuple(
        _event(
            index,
            InteractionFact(
                **_common(index),
                phase="opened",
                interaction_id=f"interaction-{index}",
                source_interaction_id=f"native-{index}",
                interaction_kind="tool_review",
                status="pending",
            ),
        )
        for index in (1, 2)
    )
    records = reduce_trace_graph_records(events, run_ids=frozenset({"run"}))
    first = next(record for record in records if record.node_id == "interaction-1")
    corrupted = replace(first, result_seq=2, result_event=events[1])

    with pytest.raises(TraceStoreProtocolError, match="another event"):
        project_trace_graph_node(
            corrupted,
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


def test_link_issue_cannot_be_removed_from_standalone_assistant() -> None:
    event = _event(
        1,
        MessageFact(
            **_common(1),
            phase="reconciled",
            message_id="assistant-without-model",
            source_message_id="assistant-without-model",
            role="assistant",
            content=_captured("answer"),
        ),
    )
    record = reduce_trace_graph_records(
        (event,),
        run_ids=frozenset({"run"}),
    )[0]
    assert record.link_issue is not None

    with pytest.raises(TraceStoreProtocolError, match="link issue"):
        project_trace_graph_node(
            replace(record, link_issue=None),
            turn_id="turn",
            parent_subagent_id=None,
            relationship_missing=False,
            allowed_run_ids=frozenset({"run"}),
        )


@pytest.mark.parametrize(
    "phase, expected", [("awaiting_input", "succeeded"), ("awaiting_review", "waiting")]
)
def test_plan_waiting_for_a_new_message_is_not_a_pending_review(
    phase: str, expected: str
) -> None:
    event = _event(
        1,
        PlanRevisionFact(
            **_common(1),
            revision_id="plan-state",
            revision=0,
            status=phase,
            plan=_captured({"status": phase}),
        ),
    )
    records = reduce_trace_graph_records(
        (event,), run_ids=frozenset({event.fact.identity.run_id})
    )
    assert len(records) == 1
    assert records[0].status == expected
