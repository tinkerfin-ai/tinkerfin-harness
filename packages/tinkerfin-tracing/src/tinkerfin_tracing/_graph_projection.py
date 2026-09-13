"""Materialize public timeline events from index metadata and referenced facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from ._graph_reducer import (
    ReducedTraceGraphRevision,
    apply_graph_events,
    assistant_run_terminal_status,
    effective_graph_nodes,
    graph_node_mutations,
)
from ._ids import scope_id
from .backend import TraceGraphNodeMutation
from .capture import CapturedValue
from .errors import TraceStoreProtocolError
from .facts import (
    ContextContributionFact,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    PlanRevisionFact,
    RunFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TurnFact,
)
from .graph import (
    TraceGraphFailure,
    TraceGraphLinkIssue,
    TraceGraphNode,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphTurn,
    canonical_trace_graph_node_order,
)
from .store import TraceGraphNodeRecord


def reduce_trace_graph_records(
    events: tuple[TraceEvent, ...],
    *,
    run_ids: frozenset[str],
) -> tuple[TraceGraphNodeRecord, ...]:
    """Replay canonical mutations and bind their exact Ledger facts."""

    revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
    apply_graph_events(revisions, events)
    by_sequence = {event.trace_seq: event for event in events}

    def event_at(sequence: int | None) -> TraceEvent | None:
        if sequence is None:
            return None
        event = by_sequence.get(sequence)
        if event is None:
            raise TraceStoreProtocolError(
                "Trace Graph locator is outside the replayed Ledger prefix"
            )
        return event

    records: list[TraceGraphNodeRecord] = []
    for node in effective_graph_nodes(revisions.values(), run_ids=run_ids):
        started_event = event_at(node.started_seq)
        updated_event = event_at(node.updated_seq)
        if started_event is None or updated_event is None:
            raise TraceStoreProtocolError("Trace Graph event lacks lifecycle facts")
        records.append(
            TraceGraphNodeRecord(
                node_id=node.node_id,
                parent_subagent_id=node.parent_subagent_id,
                model_call_id=node.model_call_id,
                model_call_seq=node.model_call_seq,
                kind=node.kind,
                status=node.status,
                name=node.name,
                run_id=node.run_id,
                graph_namespace=node.graph_namespace,
                agent_name=node.agent_name,
                provider=node.provider,
                model=node.model,
                started_at=node.started_at,
                first_output_at=node.first_output_at,
                completed_at=node.completed_at,
                started_seq=node.started_seq,
                updated_seq=node.updated_seq,
                request_seq=node.request_seq,
                result_seq=node.result_seq,
                failure_seq=node.failure_seq,
                link_issue=node.link_issue,
                started_event=started_event,
                updated_event=updated_event,
                request_event=event_at(node.request_seq),
                result_event=event_at(node.result_seq),
                failure_event=event_at(node.failure_seq),
                model_call_event=event_at(node.model_call_seq),
            )
        )
    return tuple(records)


def _captured(value: CapturedValue | None) -> tuple[JsonValue | None, bool]:
    if value is None:
        return None, False
    return value.value, value.disposition == "omitted"


def _captured_tool(value: CapturedValue | None) -> tuple[JsonValue | None, bool]:
    captured, omitted = _captured(value)
    if isinstance(captured, dict) and set(captured) == {""}:
        return captured[""], omitted
    return captured, omitted


def _subagent_task_content(
    value: CapturedValue | None,
) -> tuple[JsonValue | None, bool]:
    """Project the delegated task text without duplicating its stored arguments."""

    request, omitted = _captured_tool(value)
    if omitted:
        return None, True
    if not isinstance(request, dict):
        raise TraceStoreProtocolError("Subagent input is not a task argument object")
    description = request.get("description")
    if description is None:
        return None, True
    if not isinstance(description, str):
        raise TraceStoreProtocolError("Subagent task description is not text")
    return description, False


def _assistant_tool_call_only(
    record: TraceGraphNodeRecord,
    *,
    content: JsonValue | None,
    content_omitted: bool,
) -> bool:
    """Identify an empty Assistant output backed by a completed Tool proposal."""

    if content_omitted:
        return False
    if isinstance(content, str):
        content_is_empty = not content.strip()
    else:
        content_is_empty = content is None or content == []
    if not content_is_empty:
        return False
    return any(
        isinstance(event.fact, ModelCallFact)
        and bool(event.fact.tool_call_ids)
        and any(
            scope_id("message", event.fact.graph_namespace, source_id) == record.node_id
            for source_id in event.fact.output_message_ids
        )
        for event in (
            record.started_event,
            record.request_event,
            record.result_event,
            record.failure_event,
        )
        if event is not None
    )


def _failure(
    error_type: str,
    *,
    message: CapturedValue | None = None,
) -> TraceGraphFailure:
    value, _omitted = _captured(message)
    return TraceGraphFailure(
        error_type=error_type,
        message=value if isinstance(value, str) else None,
    )


def _context_content(
    fact: ModelCallFact,
) -> tuple[JsonValue | None, bool]:
    request, omitted = _captured(fact.request)
    if omitted or not isinstance(request, dict):
        return None, omitted
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise TraceStoreProtocolError("Model request messages are unavailable")
    values: list[JsonValue] = []
    for position in fact.system_message_positions:
        if position >= len(messages) or not isinstance(messages[position], dict):
            raise TraceStoreProtocolError(
                "Context SystemMessage position is outside the request"
            )
        message = cast(dict[str, JsonValue], messages[position])
        if message.get("messageType") != "system":
            raise TraceStoreProtocolError(
                "Context SystemMessage position references another role"
            )
        values.append(message.get("content"))
    if not values:
        return None, False
    return (values[0] if len(values) == 1 else values), False


def _tool_node(graph_namespace: tuple[str, ...], source_id: str) -> str:
    return scope_id("tool", graph_namespace, source_id)


def _subagent_input_node(fact: SubagentFact) -> str:
    return scope_id("subagent-input", fact.graph_namespace, fact.subagent_id)


def _subagent_owner(graph_namespace: tuple[str, ...]) -> str | None:
    if not graph_namespace:
        return None
    return scope_id("subagent", graph_namespace, graph_namespace[-1])


def _outer_subagent_owner(graph_namespace: tuple[str, ...]) -> str | None:
    return _subagent_owner(graph_namespace[:-1])


def _message_fact_for(record: TraceGraphNodeRecord) -> MessageFact | None:
    facts = (
        None if record.result_event is None else record.result_event.fact,
        record.updated_event.fact,
        record.started_event.fact,
    )
    return next((fact for fact in facts if isinstance(fact, MessageFact)), None)


def _validate_locator_slots(record: TraceGraphNodeRecord) -> None:
    """Prove every stored locator was produced for its declared Graph slot."""

    if (record.model_call_id is None) != (record.model_call_seq is None):
        raise TraceStoreProtocolError(
            "Trace Graph Model relationship and evidence locator must be paired"
        )
    slots = (
        ("started", record.started_seq, record.started_event),
        ("updated", record.updated_seq, record.updated_event),
        ("request", record.request_seq, record.request_event),
        ("result", record.result_seq, record.result_event),
        ("failure", record.failure_seq, record.failure_event),
        ("model_call", record.model_call_seq, record.model_call_event),
    )
    mutations: dict[int, TraceGraphNodeMutation] = {}
    for name, sequence, event in slots:
        if sequence is None:
            if event is not None:
                raise TraceStoreProtocolError(
                    f"Trace Graph {name} locator has no sequence"
                )
            continue
        if event is None or event.trace_seq != sequence:
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator does not match its sequence"
            )
        mutation = next(
            (
                candidate
                for candidate in graph_node_mutations((event,))
                if candidate.node_id == record.node_id
                and getattr(candidate, f"{name}_seq") == sequence
            ),
            None,
        )
        if mutation is None and name == "updated":
            mutation = _assistant_run_terminal_locator(record, event)
        if mutation is None or mutation.remove:
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator does not produce that slot"
            )
        mutations[sequence] = mutation
        if mutation.kind is not None and mutation.kind is not record.kind:
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator produces another node kind"
            )
        if (
            mutation.graph_namespace is not None
            and mutation.graph_namespace != record.graph_namespace
        ):
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator produces another graph_namespace"
            )
        if (
            mutation.parent_subagent_id is not None
            and mutation.parent_subagent_id != record.parent_subagent_id
        ):
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator produces another Subagent owner"
            )
        if (
            mutation.model_call_id is not None
            and mutation.model_call_id != record.model_call_id
        ):
            raise TraceStoreProtocolError(
                f"Trace Graph {name} locator produces another model relationship"
            )
    if record.updated_event.fact.identity.run_id != record.run_id:
        raise TraceStoreProtocolError(
            "Trace Graph latest update belongs to another Run"
        )
    if any(sequence > record.updated_seq for sequence in mutations):
        raise TraceStoreProtocolError("Trace Graph locator follows its latest update")
    if mutations[record.started_seq].started_at != record.started_at:
        raise TraceStoreProtocolError("Trace Graph start time conflicts with its fact")
    latest_status = max(
        (mutation for mutation in mutations.values() if mutation.status is not None),
        key=lambda mutation: mutation.updated_seq,
        default=None,
    )
    if latest_status is None or latest_status.status is not record.status:
        raise TraceStoreProtocolError("Trace Graph status conflicts with its facts")
    expected_completion = (
        None
        if record.status in {TraceGraphNodeStatus.RUNNING, TraceGraphNodeStatus.WAITING}
        else latest_status.completed_at
    )
    if record.completed_at != expected_completion:
        raise TraceStoreProtocolError(
            "Trace Graph completion time conflicts with its facts"
        )


def _assistant_run_terminal_locator(
    record: TraceGraphNodeRecord,
    event: TraceEvent,
) -> TraceGraphNodeMutation | None:
    """Prove a stopped delivery from its own lifecycle and same-Run terminal.

    This compound locator cannot replace a completed message, an explicit terminal,
    another Run, or any interaction. Original message and model locators remain
    independently validated; the Run contributes no content or model relationship.
    """

    fact = event.fact
    if (
        record.kind is not TraceGraphNodeKind.ASSISTANT_MESSAGE
        or not isinstance(fact, RunFact)
        or fact.graph_namespace
        or fact.identity.run_id != record.run_id
    ):
        return None
    status = assistant_run_terminal_status(fact)
    if status is None:
        return None
    basis = tuple(
        source
        for source in (
            record.started_event,
            record.request_event,
            record.result_event,
            record.failure_event,
            record.model_call_event,
        )
        if source is not None
    )
    if any(source.trace_seq >= event.trace_seq for source in basis):
        return None
    prior = [
        mutation
        for source in basis
        for mutation in graph_node_mutations((source,))
        if mutation.node_id == record.node_id and mutation.status is not None
    ]
    latest = max(prior, key=lambda mutation: mutation.updated_seq, default=None)
    if latest is None or latest.status is not TraceGraphNodeStatus.RUNNING:
        return None
    return TraceGraphNodeMutation(
        node_id=record.node_id,
        run_id=record.run_id,
        updated_seq=event.trace_seq,
        status=status,
        completed_at=None
        if status is TraceGraphNodeStatus.WAITING
        else fact.occurred_at,
    )


def _validate_locator_ownership(
    record: TraceGraphNodeRecord,
    *,
    allowed_run_ids: frozenset[str],
) -> None:
    """Reject valid facts that belong to another semantic timeline event."""

    events = (
        record.started_event,
        record.updated_event,
        record.request_event,
        record.result_event,
        record.failure_event,
        record.model_call_event,
    )
    for event in events:
        if event is None:
            continue
        fact = event.fact
        if fact.identity.run_id not in allowed_run_ids:
            raise TraceStoreProtocolError(
                "Trace Graph locator belongs to another Run lineage"
            )
        run_terminal = (
            event is record.updated_event
            and _assistant_run_terminal_locator(record, event) is not None
        )
        if (
            fact.graph_namespace != record.graph_namespace
            and not isinstance(fact, TurnFact)
            and not run_terminal
        ):
            raise TraceStoreProtocolError(
                "Trace Graph locator belongs to another graph_namespace"
            )

    subagent_fact = next(
        (
            event.fact
            for event in events
            if event is not None and isinstance(event.fact, SubagentFact)
        ),
        None,
    )
    if record.kind is TraceGraphNodeKind.SUBAGENT:
        if not record.graph_namespace:
            raise TraceStoreProtocolError(
                "Subagent Graph event requires a graph_namespace"
            )
        expected_parent_subagent_id = _outer_subagent_owner(record.graph_namespace)
    elif record.kind is TraceGraphNodeKind.HUMAN_MESSAGE and isinstance(
        subagent_fact, SubagentFact
    ):
        expected_parent_subagent_id = subagent_fact.subagent_id
    else:
        scope_evidence = {
            event.fact.in_subagent_scope
            for event in events
            if event is not None and not isinstance(event.fact, (TurnFact, RunFact))
        }
        if len(scope_evidence) > 1:
            raise TraceStoreProtocolError(
                "Trace Graph facts disagree on Subagent scope ownership"
            )
        expected_parent_subagent_id = (
            _subagent_owner(record.graph_namespace)
            if scope_evidence == {True}
            else None
        )
    if record.parent_subagent_id != expected_parent_subagent_id:
        raise TraceStoreProtocolError(
            "Trace Graph Subagent owner conflicts with its graph_namespace"
        )

    model_call_ids: set[str] = set()
    for event in events:
        if event is None:
            continue
        fact = event.fact
        if record.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE and isinstance(
            fact, ModelCallFact
        ):
            if any(
                scope_id("message", fact.graph_namespace, source_id) == record.node_id
                for source_id in fact.output_message_ids
            ):
                model_call_ids.add(fact.call_id)
        elif record.kind is TraceGraphNodeKind.TOOL:
            if isinstance(fact, ModelCallFact) and any(
                _tool_node(fact.graph_namespace, source_id) == record.node_id
                for source_id in fact.tool_call_ids
            ):
                model_call_ids.add(fact.call_id)
            elif isinstance(fact, ToolFact) and fact.parent_call_id is not None:
                model_call_ids.add(fact.parent_call_id)
            elif (
                isinstance(fact, ToolExecutionFact) and fact.parent_call_id is not None
            ):
                model_call_ids.add(fact.parent_call_id)
        elif (
            record.kind is TraceGraphNodeKind.SUBAGENT
            and isinstance(fact, SubagentFact)
            and fact.model_call_id is not None
        ):
            model_call_ids.add(fact.model_call_id)
    if len(model_call_ids) > 1:
        raise TraceStoreProtocolError(
            "Trace Graph model relationship has conflicting evidence"
        )
    expected_model_call_id = next(iter(model_call_ids), None)
    if record.model_call_id != expected_model_call_id:
        raise TraceStoreProtocolError(
            "Trace Graph model relationship conflicts with its facts"
        )
    if record.kind is TraceGraphNodeKind.MODEL:
        if any(
            isinstance(event.fact, ModelCallFact)
            and event.fact.call_id != record.node_id
            for event in events
            if event is not None
        ):
            raise TraceStoreProtocolError("Model locator belongs to another call")
    elif record.kind is TraceGraphNodeKind.CONTEXT:
        if any(
            not isinstance(event.fact, ModelCallFact)
            or event.fact.phase != "started"
            or record.node_id
            != scope_id("context", event.fact.graph_namespace, event.fact.call_id)
            for event in events
            if event is not None
        ):
            raise TraceStoreProtocolError(
                "Context locator belongs to another model request"
            )
    elif record.kind is TraceGraphNodeKind.TOOL:
        for event in events:
            if event is None:
                continue
            fact = event.fact
            if (
                isinstance(fact, ToolFact)
                and _tool_node(fact.graph_namespace, fact.source_tool_call_id)
                != record.node_id
            ):
                raise TraceStoreProtocolError("Tool locator belongs to another call")
            if (
                isinstance(fact, ToolExecutionFact)
                and fact.source_tool_call_id is not None
                and _tool_node(fact.graph_namespace, fact.source_tool_call_id)
                != record.node_id
            ):
                raise TraceStoreProtocolError("Tool execution belongs to another call")
            if (
                isinstance(fact, ToolExecutionFact)
                and fact.source_tool_call_id is None
                and fact.execution_id != record.node_id
            ):
                raise TraceStoreProtocolError("Tool execution belongs to another call")
    elif record.kind is TraceGraphNodeKind.SUBAGENT:
        if any(
            isinstance(event.fact, SubagentFact)
            and event.fact.subagent_id != record.node_id
            for event in events
            if event is not None
        ):
            raise TraceStoreProtocolError("Subagent locator belongs to another scope")
    elif record.kind is TraceGraphNodeKind.HUMAN_MESSAGE:
        for event in events:
            if event is None:
                continue
            fact = event.fact
            if (
                isinstance(fact, SubagentFact)
                and _subagent_input_node(fact) != record.node_id
            ):
                raise TraceStoreProtocolError(
                    "Subagent input locator belongs to another event"
                )
            if isinstance(fact, MessageFact) and fact.message_id != record.node_id:
                raise TraceStoreProtocolError(
                    "Message locator belongs to another event"
                )
            if isinstance(fact, TurnFact):
                source_id = fact.user_message_id or fact.turn_id
                if scope_id("message", (), source_id) != record.node_id:
                    raise TraceStoreProtocolError(
                        "Turn locator belongs to another HumanMessage"
                    )
    elif record.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE:
        message = _message_fact_for(record)
        if message is not None and message.message_id != record.node_id:
            raise TraceStoreProtocolError(
                "Assistant locator belongs to another message"
            )
    elif record.kind in {
        TraceGraphNodeKind.MEMORY,
        TraceGraphNodeKind.GUARDRAIL,
        TraceGraphNodeKind.RETRIEVAL,
        TraceGraphNodeKind.CUSTOM,
    }:
        if any(
            not isinstance(event.fact, ContextContributionFact)
            or event.fact.contribution_id != record.node_id
            for event in events
            if event is not None
        ):
            raise TraceStoreProtocolError("Context locator belongs to another event")
    elif record.kind is TraceGraphNodeKind.PLAN:
        if any(
            not isinstance(event.fact, PlanRevisionFact)
            or scope_id(
                "plan",
                event.fact.graph_namespace,
                event.fact.identity.run_id,
            )
            != record.node_id
            for event in events
            if event is not None
        ):
            raise TraceStoreProtocolError("Plan locator belongs to another event")
    elif record.kind is TraceGraphNodeKind.INTERACTION and any(
        not isinstance(event.fact, InteractionFact)
        or event.fact.interaction_id != record.node_id
        for event in events
        if event is not None
    ):
        raise TraceStoreProtocolError("Interaction locator belongs to another event")

    _validate_locator_slots(record)

    expected_issue: TraceGraphLinkIssue | None = None
    if (
        record.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
        and record.model_call_id is None
    ):
        expected_issue = TraceGraphLinkIssue.MISSING_MODEL_CALL
    elif record.kind is TraceGraphNodeKind.TOOL:
        if any(
            isinstance(event.fact, ToolExecutionFact)
            and event.fact.source_tool_call_id is None
            for event in events
            if event is not None
        ):
            expected_issue = TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
        elif record.model_call_id is None and any(
            (
                isinstance(event.fact, ToolExecutionFact)
                and event.fact.source_tool_call_id is not None
            )
            or (
                isinstance(event.fact, ToolFact)
                and event.fact.phase in {"started", "arguments"}
            )
            for event in events
            if event is not None
        ):
            expected_issue = TraceGraphLinkIssue.MISSING_MODEL_CALL
    elif record.kind is TraceGraphNodeKind.SUBAGENT:
        if not isinstance(subagent_fact, SubagentFact):
            raise TraceStoreProtocolError("Subagent Graph event has no source fact")
        if subagent_fact.parent_tool_call_id is None:
            expected_issue = TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
        elif record.model_call_id is None:
            expected_issue = TraceGraphLinkIssue.MISSING_MODEL_CALL
    if record.link_issue is not expected_issue:
        raise TraceStoreProtocolError(
            "Trace Graph link issue conflicts with its relationship facts"
        )


def trace_graph_record_search_values(
    record: TraceGraphNodeRecord,
    *,
    allowed_run_ids: frozenset[str],
) -> tuple[JsonValue, ...]:
    """Return decoded detail values exposed by the corresponding public event."""

    node = project_trace_graph_node(
        record,
        turn_id="search",
        parent_subagent_id=record.parent_subagent_id,
        relationship_missing=False,
        allowed_run_ids=allowed_run_ids,
    )
    values: list[JsonValue] = []
    for value, omitted in (
        (node.content, node.content_omitted),
        (node.request, node.request_omitted),
        (node.result, node.result_omitted),
    ):
        if value is not None and not omitted:
            values.append(value)
    if node.failure is not None:
        values.append(node.failure.error_type)
        if node.failure.message is not None:
            values.append(node.failure.message)
    return tuple(values)


def project_trace_graph_node(
    record: TraceGraphNodeRecord,
    *,
    turn_id: str,
    parent_subagent_id: str | None,
    relationship_missing: bool,
    allowed_run_ids: frozenset[str],
) -> TraceGraphNode:
    """Build one public event while validating every Ledger locator owner."""

    _validate_locator_ownership(record, allowed_run_ids=allowed_run_ids)
    start = record.started_event.fact
    request_fact = None if record.request_event is None else record.request_event.fact
    result_fact = None if record.result_event is None else record.result_event.fact
    failure_fact = None if record.failure_event is None else record.failure_event.fact
    content: JsonValue | None = None
    content_omitted = False
    request: JsonValue | None = None
    request_omitted = False
    result: JsonValue | None = None
    result_omitted = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceGraphFailure | None = None
    source_id: str | None = None
    tool_call_only = False

    if record.kind is TraceGraphNodeKind.HUMAN_MESSAGE:
        message_fact = _message_fact_for(record)
        subagent_fact = next(
            (
                event.fact
                for event in (
                    record.result_event,
                    record.request_event,
                    record.started_event,
                )
                if event is not None and isinstance(event.fact, SubagentFact)
            ),
            None,
        )
        if message_fact is not None:
            source_id = message_fact.source_message_id
            content, content_omitted = _captured(message_fact.content)
        elif isinstance(subagent_fact, SubagentFact):
            source_id = subagent_fact.parent_tool_call_id
            content, content_omitted = _subagent_task_content(subagent_fact.input)
        elif isinstance(start, TurnFact):
            source_id = start.user_message_id
        else:
            raise TraceStoreProtocolError("HumanMessage Graph event is invalid")
    elif record.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE:
        message_fact = _message_fact_for(record)
        if message_fact is not None:
            source_id = message_fact.source_message_id
            content, content_omitted = _captured(message_fact.content)
        elif not isinstance(start, ModelCallFact):
            raise TraceStoreProtocolError("AssistantMessage Graph event is invalid")
        if source_id is None and record.model_call_event is not None:
            model_fact = record.model_call_event.fact
            if isinstance(model_fact, ModelCallFact):
                # The separately validated Model locator keeps the exact Native
                # identity when a Run terminal replaces the latest message locator.
                # Match the scoped output evidence; never decode a generated node ID.
                source_id = next(
                    (
                        candidate
                        for candidate in model_fact.output_message_ids
                        if scope_id("message", model_fact.graph_namespace, candidate)
                        == record.node_id
                    ),
                    None,
                )
        tool_call_only = _assistant_tool_call_only(
            record,
            content=content,
            content_omitted=content_omitted,
        )
    elif record.kind is TraceGraphNodeKind.CONTEXT:
        if (
            not isinstance(request_fact, ModelCallFact)
            or request_fact.phase != "started"
        ):
            raise TraceStoreProtocolError("Context Graph event is invalid")
        content, content_omitted = _context_content(request_fact)
        source_id = request_fact.call_id
    elif record.kind is TraceGraphNodeKind.MODEL:
        if (
            not isinstance(request_fact, ModelCallFact)
            or request_fact.phase != "started"
        ):
            raise TraceStoreProtocolError("Model Graph event is invalid")
        request, request_omitted = _captured(request_fact.request)
        source_id = request_fact.call_id
        if isinstance(result_fact, ModelCallFact):
            usage, _usage_omitted = _captured(result_fact.usage)
            response_metadata, _metadata_omitted = _captured(
                result_fact.response_metadata
            )
        if isinstance(failure_fact, ModelCallFact):
            failure = _failure(
                failure_fact.error_type or "model_error",
                message=failure_fact.error_message,
            )
    elif record.kind is TraceGraphNodeKind.TOOL:
        if isinstance(request_fact, ToolExecutionFact):
            request, request_omitted = _captured_tool(request_fact.input)
            source_id = request_fact.source_tool_call_id
        elif isinstance(request_fact, ToolFact):
            request, request_omitted = _captured_tool(request_fact.content)
            source_id = request_fact.source_tool_call_id
        if isinstance(result_fact, ToolExecutionFact):
            result, result_omitted = _captured_tool(result_fact.output)
        elif isinstance(result_fact, ToolFact):
            result, result_omitted = _captured_tool(result_fact.content)
            source_id = source_id or result_fact.source_tool_call_id
        if isinstance(failure_fact, ToolExecutionFact):
            failure = _failure(
                failure_fact.error_type or "tool_error",
                message=failure_fact.error_message,
            )
        elif isinstance(failure_fact, ToolFact):
            failure = TraceGraphFailure(error_type="tool_error")
    elif record.kind is TraceGraphNodeKind.SUBAGENT:
        subagent_request_fact = (
            request_fact if isinstance(request_fact, SubagentFact) else start
        )
        if not isinstance(subagent_request_fact, SubagentFact):
            raise TraceStoreProtocolError("Subagent Graph event is invalid")
        source_id = subagent_request_fact.parent_tool_call_id
        request, request_omitted = _captured_tool(subagent_request_fact.input)
    elif record.kind in {
        TraceGraphNodeKind.MEMORY,
        TraceGraphNodeKind.GUARDRAIL,
        TraceGraphNodeKind.RETRIEVAL,
        TraceGraphNodeKind.CUSTOM,
    }:
        if isinstance(request_fact, ContextContributionFact):
            request, request_omitted = _captured(request_fact.input)
        if isinstance(result_fact, ContextContributionFact):
            result, result_omitted = _captured(result_fact.output)
        if isinstance(failure_fact, ContextContributionFact):
            failure = TraceGraphFailure(
                error_type=failure_fact.error_type or "context_error"
            )
    elif record.kind is TraceGraphNodeKind.PLAN:
        if not isinstance(result_fact, PlanRevisionFact):
            raise TraceStoreProtocolError("Plan Graph event is invalid")
        result, result_omitted = _captured(result_fact.plan)
        source_id = result_fact.revision_id
    elif record.kind is TraceGraphNodeKind.INTERACTION:
        if not isinstance(result_fact, InteractionFact):
            raise TraceStoreProtocolError("Interaction Graph event is invalid")
        result, result_omitted = _captured(result_fact.payload)
        source_id = result_fact.source_interaction_id
    else:
        raise TraceStoreProtocolError("Trace Graph event kind has no projection")

    issues = () if record.link_issue is None else (record.link_issue,)
    if relationship_missing and TraceGraphLinkIssue.MISSING_SUBAGENT not in issues:
        issues = (*issues, TraceGraphLinkIssue.MISSING_SUBAGENT)
    return TraceGraphNode(
        id=record.node_id,
        turn_id=turn_id,
        parent_subagent_id=parent_subagent_id,
        model_call_id=record.model_call_id,
        kind=record.kind,
        status=record.status,
        name=record.name,
        run_id=record.run_id,
        graph_namespace=record.graph_namespace,
        agent_name=record.agent_name,
        provider=record.provider,
        model=record.model,
        source_id=source_id,
        started_at=record.started_at,
        first_output_at=record.first_output_at,
        completed_at=record.completed_at,
        started_seq=record.started_seq,
        updated_seq=record.updated_seq,
        content=content,
        content_omitted=content_omitted,
        tool_call_only=tool_call_only,
        request=request,
        request_omitted=request_omitted,
        result=result,
        result_omitted=result_omitted,
        usage=usage,
        response_metadata=response_metadata,
        failure=failure,
        link_issues=issues,
    )


def project_trace_graph_records(
    records: tuple[TraceGraphNodeRecord, ...],
    *,
    turns: tuple[TraceGraphTurn, ...],
    run_turns: Mapping[str, str],
    selected_run_ids: frozenset[str],
) -> tuple[tuple[TraceGraphNode, ...], tuple[str, ...]]:
    """Project flat scopes and deterministic display order from Graph records."""

    turn_by_id = {turn.id: turn for turn in turns}
    record_ids = {record.node_id for record in records}
    projected: dict[str, TraceGraphNode] = {}
    for record in records:
        # A later input can settle an earlier pending call without starting it
        # again. Its latest observation belongs to the new Run, while the call
        # and its child scope remain in the Turn that owns the recorded start.
        turn_id = run_turns.get(record.started_event.fact.identity.run_id)
        turn = None if turn_id is None else turn_by_id.get(turn_id)
        if turn is None:
            raise TraceStoreProtocolError(
                "Trace Graph event has no selected Turn ownership"
            )
        parent_id = record.parent_subagent_id
        missing = parent_id is not None and parent_id not in record_ids
        projected[record.node_id] = project_trace_graph_node(
            record,
            turn_id=turn.id,
            parent_subagent_id=None if missing else parent_id,
            relationship_missing=missing,
            allowed_run_ids=selected_run_ids,
        )
    try:
        ordered_ids = canonical_trace_graph_node_order(turns, projected)
    except ValueError as error:
        raise TraceStoreProtocolError(str(error), cause=error) from error
    return tuple(projected[node_id] for node_id in ordered_ids), ordered_ids


__all__ = [
    "project_trace_graph_node",
    "project_trace_graph_records",
    "reduce_trace_graph_records",
    "trace_graph_record_search_values",
]
