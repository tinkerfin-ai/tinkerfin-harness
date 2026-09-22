"""Reduce committed facts into one flat, Subagent-scoped timeline index."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypeAlias, cast

from ._ids import scope_id
from .backend import TraceGraphNodeMutation
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
    TraceSemanticFact,
    TurnFact,
)
from .graph import TraceGraphLinkIssue, TraceGraphNodeKind, TraceGraphNodeStatus


@dataclass(slots=True)
class ReducedTraceGraphNode:
    """Hold one payload-free event revision produced by Graph mutations."""

    node_id: str
    parent_subagent_id: str | None
    model_call_id: str | None
    model_call_seq: int | None
    kind: TraceGraphNodeKind
    status: TraceGraphNodeStatus
    name: str
    run_id: str
    graph_namespace: tuple[str, ...]
    agent_name: str | None
    provider: str | None
    model: str | None
    started_at: datetime
    first_output_at: datetime | None
    completed_at: datetime | None
    started_seq: int
    updated_seq: int
    request_seq: int | None
    result_seq: int | None
    failure_seq: int | None
    link_issue: TraceGraphLinkIssue | None


@dataclass(frozen=True, slots=True)
class ReducedTraceGraphTombstone:
    """Hide an inherited logical event in one selected Run lineage."""

    node_id: str
    run_id: str
    updated_seq: int


ReducedTraceGraphRevision: TypeAlias = (
    ReducedTraceGraphNode | ReducedTraceGraphTombstone
)


def _tool_node(graph_namespace: tuple[str, ...], source_tool_call_id: str) -> str:
    return scope_id("tool", graph_namespace, source_tool_call_id)


def _subagent_owner(graph_namespace: tuple[str, ...]) -> str | None:
    if not graph_namespace:
        return None
    return scope_id("subagent", graph_namespace, graph_namespace[-1])


def _outer_subagent_owner(graph_namespace: tuple[str, ...]) -> str | None:
    return _subagent_owner(graph_namespace[:-1])


def _fact_subagent_owner(fact: TraceSemanticFact) -> str | None:
    return _subagent_owner(fact.graph_namespace) if fact.in_subagent_scope else None


def _subagent_input_node(fact: SubagentFact) -> str:
    return scope_id("subagent-input", fact.graph_namespace, fact.subagent_id)


def resolve_graph_link_issue(
    parent_subagent_id: str | None,
    model_call_id: str | None,
    link_issue: TraceGraphLinkIssue | None,
) -> TraceGraphLinkIssue | None:
    """Drop a missing-link marker once its exact relationship is available."""

    if (
        parent_subagent_id is not None
        and link_issue is TraceGraphLinkIssue.MISSING_SUBAGENT
    ):
        return None
    if (
        model_call_id is not None
        and link_issue is TraceGraphLinkIssue.MISSING_MODEL_CALL
    ):
        return None
    return link_issue


def resolve_graph_node_kind(
    current: TraceGraphNodeKind | None,
    incoming: TraceGraphNodeKind | None,
) -> TraceGraphNodeKind | None:
    """Keep one immutable semantic kind for every stable event identity."""

    if current is None:
        return incoming
    if incoming is None or incoming is current:
        return current
    raise TraceStoreProtocolError(
        f"Trace Graph node kind changed from {current.value} to {incoming.value}"
    )


def _subagent_status(value: str) -> TraceGraphNodeStatus:
    try:
        return {
            "succeeded": TraceGraphNodeStatus.SUCCEEDED,
            "failed": TraceGraphNodeStatus.FAILED,
            "cancelled": TraceGraphNodeStatus.CANCELLED,
            "abandoned": TraceGraphNodeStatus.ABANDONED,
            "waiting": TraceGraphNodeStatus.WAITING,
        }[value]
    except KeyError as error:  # pragma: no cover - SubagentFact validates this boundary
        raise TraceStoreProtocolError("Subagent status is not projectable") from error


def _phase_status(value: str) -> TraceGraphNodeStatus:
    return {
        "completed": TraceGraphNodeStatus.SUCCEEDED,
        "failed": TraceGraphNodeStatus.FAILED,
        "cancelled": TraceGraphNodeStatus.CANCELLED,
        "interrupted": TraceGraphNodeStatus.WAITING,
        "abandoned": TraceGraphNodeStatus.ABANDONED,
        "resolved": TraceGraphNodeStatus.SUCCEEDED,
    }.get(value, TraceGraphNodeStatus.UNKNOWN)


def _terminal_time(
    status: TraceGraphNodeStatus, occurred_at: datetime
) -> datetime | None:
    if status in {TraceGraphNodeStatus.RUNNING, TraceGraphNodeStatus.WAITING}:
        return None
    return occurred_at


def graph_node_mutations(
    events: tuple[TraceEvent, ...],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Reduce one ordered fact batch into payload-free timeline mutations."""

    mutations: list[TraceGraphNodeMutation] = []
    for event in events:
        fact = event.fact
        if isinstance(fact, TurnFact):
            # A maintenance Turn has no user message. Its execution boundary must
            # not create a synthetic HumanMessage in either live or rebuilt graphs.
            source_message_id = fact.user_message_id
            if source_message_id is None:
                continue
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=scope_id("message", (), source_message_id),
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.HUMAN_MESSAGE,
                    status=TraceGraphNodeStatus.SUCCEEDED,
                    name="HumanMessage",
                    run_id=fact.identity.run_id,
                    graph_namespace=(),
                    started_at=fact.occurred_at,
                    completed_at=fact.occurred_at,
                    started_seq=event.trace_seq,
                    link_issue=None,
                )
            )
            continue
        if isinstance(fact, MessageFact):
            if fact.phase == "removed":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.message_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        remove=True,
                    )
                )
                continue
            if fact.role == "tool" or fact.role == "other" or fact.role == "system":
                continue
            if fact.role == "user":
                if fact.graph_namespace or fact.phase == "content":
                    continue
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.message_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        result_seq=(
                            event.trace_seq if fact.content is not None else None
                        ),
                    )
                )
                continue
            if fact.phase == "content":
                continue
            status = (
                TraceGraphNodeStatus.RUNNING
                if fact.phase == "started"
                else _phase_status(fact.phase)
                if fact.phase in {"cancelled", "interrupted", "abandoned"}
                else TraceGraphNodeStatus.SUCCEEDED
            )
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=fact.message_id,
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
                    status=status,
                    name="AssistantMessage",
                    run_id=fact.identity.run_id,
                    parent_subagent_id=_fact_subagent_owner(fact),
                    graph_namespace=fact.graph_namespace,
                    started_at=fact.occurred_at,
                    completed_at=_terminal_time(status, fact.occurred_at),
                    started_seq=event.trace_seq,
                    result_seq=(event.trace_seq if fact.content is not None else None),
                    link_issue=TraceGraphLinkIssue.MISSING_MODEL_CALL,
                )
            )
            continue
        if isinstance(fact, ModelCallFact):
            if fact.phase == "started":
                if fact.context_started_at is None:  # pragma: no cover - Fact contract
                    raise TraceStoreProtocolError(
                        "Model call context start is unavailable"
                    )
                if fact.contribution_id is None:
                    mutations.append(
                        TraceGraphNodeMutation(
                            node_id=scope_id(
                                "context", fact.graph_namespace, fact.call_id
                            ),
                            updated_seq=event.trace_seq,
                            kind=TraceGraphNodeKind.CONTEXT,
                            status=TraceGraphNodeStatus.SUCCEEDED,
                            name="Context",
                            run_id=fact.identity.run_id,
                            parent_subagent_id=_fact_subagent_owner(fact),
                            graph_namespace=fact.graph_namespace,
                            agent_name=fact.agent_name,
                            provider=fact.provider,
                            model=fact.model,
                            started_at=fact.context_started_at,
                            completed_at=fact.occurred_at,
                            started_seq=event.trace_seq,
                            request_seq=event.trace_seq,
                        )
                    )
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.MODEL,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.model or "Model",
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        graph_namespace=fact.graph_namespace,
                        agent_name=fact.agent_name,
                        provider=fact.provider,
                        model=fact.model,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "first_output":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        first_output_at=fact.occurred_at,
                    )
                )
                for source_message_id in fact.output_message_ids:
                    mutations.append(
                        TraceGraphNodeMutation(
                            node_id=scope_id(
                                "message",
                                fact.graph_namespace,
                                source_message_id,
                            ),
                            updated_seq=event.trace_seq,
                            kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
                            status=TraceGraphNodeStatus.RUNNING,
                            name="AssistantMessage",
                            run_id=fact.identity.run_id,
                            parent_subagent_id=_fact_subagent_owner(fact),
                            model_call_id=fact.call_id,
                            model_call_seq=event.trace_seq,
                            graph_namespace=fact.graph_namespace,
                            agent_name=fact.agent_name,
                            started_at=fact.occurred_at,
                            started_seq=event.trace_seq,
                        )
                    )
            else:
                status = _phase_status(fact.phase)
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=status,
                        completed_at=_terminal_time(status, fact.occurred_at),
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
                if fact.phase == "completed":
                    for source_message_id in fact.output_message_ids:
                        mutations.append(
                            TraceGraphNodeMutation(
                                node_id=scope_id(
                                    "message",
                                    fact.graph_namespace,
                                    source_message_id,
                                ),
                                updated_seq=event.trace_seq,
                                kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
                                status=TraceGraphNodeStatus.SUCCEEDED,
                                name="AssistantMessage",
                                run_id=fact.identity.run_id,
                                parent_subagent_id=_fact_subagent_owner(fact),
                                model_call_id=fact.call_id,
                                model_call_seq=event.trace_seq,
                                graph_namespace=fact.graph_namespace,
                                agent_name=fact.agent_name,
                                started_at=fact.occurred_at,
                                completed_at=fact.occurred_at,
                                started_seq=event.trace_seq,
                                request_seq=event.trace_seq,
                            )
                        )
                    for source_tool_call_id in fact.tool_call_ids:
                        mutations.append(
                            TraceGraphNodeMutation(
                                node_id=_tool_node(
                                    fact.graph_namespace,
                                    source_tool_call_id,
                                ),
                                updated_seq=event.trace_seq,
                                run_id=fact.identity.run_id,
                                model_call_id=fact.call_id,
                                model_call_seq=event.trace_seq,
                            )
                        )
            continue
        if isinstance(fact, ToolFact):
            if fact.tool_name == "task":
                continue
            node_id = _tool_node(fact.graph_namespace, fact.source_tool_call_id)
            if fact.phase in {"started", "arguments"}:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=TraceGraphNodeStatus.WAITING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        model_call_id=fact.parent_call_id,
                        model_call_seq=(
                            event.trace_seq if fact.parent_call_id is not None else None
                        ),
                        graph_namespace=fact.graph_namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=(
                            event.trace_seq
                            if fact.phase == "arguments" and fact.content is not None
                            else None
                        ),
                        link_issue=(
                            None
                            if fact.parent_call_id is not None
                            else TraceGraphLinkIssue.MISSING_MODEL_CALL
                        ),
                    )
                )
            elif fact.phase == "result":
                status = (
                    TraceGraphNodeStatus.SUCCEEDED
                    if fact.result_status == "success"
                    else TraceGraphNodeStatus.FAILED
                )
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=status,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        model_call_id=fact.parent_call_id,
                        model_call_seq=(
                            event.trace_seq if fact.parent_call_id is not None else None
                        ),
                        graph_namespace=fact.graph_namespace,
                        started_at=fact.occurred_at,
                        completed_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.result_status == "error" and fact.failure_origin
                            else None
                        ),
                    )
                )
            elif fact.phase in {"cancelled", "abandoned"}:
                status = _phase_status(fact.phase)
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=status,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        model_call_id=fact.parent_call_id,
                        model_call_seq=(
                            event.trace_seq if fact.parent_call_id is not None else None
                        ),
                        graph_namespace=fact.graph_namespace,
                        started_at=fact.occurred_at,
                        completed_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            continue
        if isinstance(fact, ToolExecutionFact):
            if fact.tool_name == "task":
                continue
            node_id = (
                _tool_node(fact.graph_namespace, fact.source_tool_call_id)
                if fact.source_tool_call_id is not None
                else fact.execution_id
            )
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        model_call_id=fact.parent_call_id,
                        model_call_seq=(
                            event.trace_seq if fact.parent_call_id is not None else None
                        ),
                        graph_namespace=fact.graph_namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                        link_issue=(
                            TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
                            if fact.source_tool_call_id is None
                            else (
                                TraceGraphLinkIssue.MISSING_MODEL_CALL
                                if fact.parent_call_id is None
                                else None
                            )
                        ),
                    )
                )
            else:
                status = _phase_status(fact.phase)
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=status,
                        completed_at=_terminal_time(status, fact.occurred_at),
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, SubagentFact):
            parent_subagent_id = _outer_subagent_owner(fact.graph_namespace)
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.SUBAGENT,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.agent_name or "Subagent",
                        run_id=fact.identity.run_id,
                        parent_subagent_id=parent_subagent_id,
                        model_call_id=fact.model_call_id,
                        model_call_seq=(
                            event.trace_seq if fact.model_call_id is not None else None
                        ),
                        graph_namespace=fact.graph_namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                        link_issue=(
                            TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
                            if fact.parent_tool_call_id is None
                            else (
                                TraceGraphLinkIssue.MISSING_MODEL_CALL
                                if fact.model_call_id is None
                                else None
                            )
                        ),
                    )
                )
                if fact.input is not None:
                    mutations.append(
                        TraceGraphNodeMutation(
                            node_id=_subagent_input_node(fact),
                            updated_seq=event.trace_seq,
                            kind=TraceGraphNodeKind.HUMAN_MESSAGE,
                            status=TraceGraphNodeStatus.SUCCEEDED,
                            name="HumanMessage",
                            run_id=fact.identity.run_id,
                            parent_subagent_id=fact.subagent_id,
                            graph_namespace=fact.graph_namespace,
                            agent_name=fact.agent_name,
                            started_at=fact.occurred_at,
                            completed_at=fact.occurred_at,
                            started_seq=event.trace_seq,
                            result_seq=event.trace_seq,
                        )
                    )
            else:
                status = _subagent_status(fact.status)
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.SUBAGENT,
                        run_id=fact.identity.run_id,
                        status=status,
                        name=fact.agent_name or "Subagent",
                        parent_subagent_id=parent_subagent_id,
                        graph_namespace=fact.graph_namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        completed_at=_terminal_time(status, fact.occurred_at),
                        started_seq=event.trace_seq,
                        result_seq=event.trace_seq,
                    )
                )
            continue
        if isinstance(fact, ContextContributionFact):
            kind = {
                "memory": TraceGraphNodeKind.MEMORY,
                "guardrail": TraceGraphNodeKind.GUARDRAIL,
                "retrieval": TraceGraphNodeKind.RETRIEVAL,
                "custom": TraceGraphNodeKind.CUSTOM,
                "compaction": TraceGraphNodeKind.CUSTOM,
            }[fact.context_kind]
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        kind=kind,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.name,
                        run_id=fact.identity.run_id,
                        parent_subagent_id=_fact_subagent_owner(fact),
                        graph_namespace=fact.graph_namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                    )
                )
            else:
                status = (
                    TraceGraphNodeStatus.RUNNING
                    if fact.phase == "generated"
                    else _phase_status(fact.phase)
                )
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=status,
                        completed_at=_terminal_time(status, fact.occurred_at),
                        result_seq=event.trace_seq
                        if fact.phase in {"generated", "completed"}
                        else None,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            if fact.context_kind == "compaction":
                child = mutations[-1]
                mutations.append(
                    replace(
                        child,
                        node_id=scope_id(
                            "compaction-context",
                            fact.graph_namespace,
                            fact.contribution_id,
                        ),
                        kind=TraceGraphNodeKind.CONTEXT
                        if fact.phase == "started"
                        else child.kind,
                        name="Context" if fact.phase == "started" else child.name,
                    )
                )
            continue
        if isinstance(fact, PlanRevisionFact):
            waiting = bool(fact.status and fact.status in {"awaiting_review", "draft"})
            status = (
                TraceGraphNodeStatus.WAITING
                if waiting
                else TraceGraphNodeStatus.SUCCEEDED
            )
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=scope_id(
                        "plan", fact.graph_namespace, fact.identity.run_id
                    ),
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.PLAN,
                    status=status,
                    name="Plan",
                    run_id=fact.identity.run_id,
                    parent_subagent_id=_fact_subagent_owner(fact),
                    graph_namespace=fact.graph_namespace,
                    started_at=fact.occurred_at,
                    completed_at=_terminal_time(status, fact.occurred_at),
                    started_seq=event.trace_seq,
                    result_seq=event.trace_seq,
                )
            )
            continue
        if isinstance(fact, InteractionFact):
            status = (
                TraceGraphNodeStatus.WAITING
                if fact.status == "pending"
                else (
                    TraceGraphNodeStatus.CANCELLED
                    if fact.status == "cancelled"
                    else TraceGraphNodeStatus.SUCCEEDED
                )
            )
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=fact.interaction_id,
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.INTERACTION,
                    status=status,
                    name=fact.interaction_kind,
                    run_id=fact.identity.run_id,
                    parent_subagent_id=_fact_subagent_owner(fact),
                    graph_namespace=fact.graph_namespace,
                    started_at=fact.occurred_at,
                    completed_at=_terminal_time(status, fact.occurred_at),
                    started_seq=event.trace_seq,
                    result_seq=event.trace_seq,
                )
            )
    return _coalesce_graph_mutations(
        mutations, source_events={event.trace_seq: event for event in events}
    )


def _coalesce_graph_mutations(
    mutations: list[TraceGraphNodeMutation],
    *,
    source_events: Mapping[int, TraceEvent],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Collapse one commit batch to at most one write per event revision."""

    ordered_keys: list[tuple[str, str]] = []
    values: dict[tuple[str, str], TraceGraphNodeMutation] = {}
    for mutation in mutations:
        key = (mutation.node_id, mutation.run_id)
        current = values.get(key)
        if current is None:
            ordered_keys.append(key)
            values[key] = mutation
            continue
        if mutation.remove or current.remove:
            values[key] = mutation
            continue
        kind = resolve_graph_node_kind(current.kind, mutation.kind)
        replace_tool_start = graph_execution_started(
            mutation, source_events=source_events
        )
        status = mutation.status or current.status
        completed_at = (
            None
            if status
            in {
                TraceGraphNodeStatus.RUNNING,
                TraceGraphNodeStatus.WAITING,
            }
            else mutation.completed_at or current.completed_at
        )
        parent_subagent_id = mutation.parent_subagent_id or current.parent_subagent_id
        model_call_id = mutation.model_call_id or current.model_call_id
        # A Tool start can replace proposal timing and edited input together. Keep
        # the independent fact that proves which Model emitted that Tool call.
        values[key] = TraceGraphNodeMutation(
            node_id=mutation.node_id,
            updated_seq=mutation.updated_seq,
            run_id=mutation.run_id,
            kind=kind,
            status=status,
            name=current.name or mutation.name,
            parent_subagent_id=parent_subagent_id,
            model_call_id=model_call_id,
            model_call_seq=mutation.model_call_seq or current.model_call_seq,
            graph_namespace=(
                current.graph_namespace
                if current.graph_namespace is not None
                else mutation.graph_namespace
            ),
            agent_name=mutation.agent_name or current.agent_name,
            provider=mutation.provider or current.provider,
            model=mutation.model or current.model,
            started_at=(
                mutation.started_at
                if replace_tool_start
                else current.started_at or mutation.started_at
            ),
            first_output_at=mutation.first_output_at or current.first_output_at,
            completed_at=completed_at,
            started_seq=(
                mutation.started_seq
                if replace_tool_start
                else current.started_seq or mutation.started_seq
            ),
            request_seq=mutation.request_seq or current.request_seq,
            result_seq=(
                mutation.result_seq
                if replace_tool_start
                else mutation.result_seq or current.result_seq
            ),
            failure_seq=graph_failure_sequence(
                current.failure_seq,
                mutation,
                source_events=source_events,
            ),
            link_issue=resolve_graph_link_issue(
                parent_subagent_id,
                model_call_id,
                current.link_issue or mutation.link_issue,
            ),
        )
    return tuple(values[key] for key in ordered_keys)


def graph_execution_started(
    mutation: TraceGraphNodeMutation,
    *,
    source_events: Mapping[int, TraceEvent],
) -> bool:
    """Recognize an execution only from the exact retained start fact.

    Argument snapshots and execution starts can share request locators after batch
    coalescing. Status and locator equality cannot distinguish their provenance.
    """

    source = source_events.get(mutation.started_seq or 0)
    if source is None:
        return False
    fact = source.fact
    if (
        fact.identity.run_id != mutation.run_id
        or fact.graph_namespace != mutation.graph_namespace
    ):
        return False
    if isinstance(fact, ToolExecutionFact):
        expected_node = (
            _tool_node(fact.graph_namespace, fact.source_tool_call_id)
            if fact.source_tool_call_id is not None
            else fact.execution_id
        )
        return fact.phase == "started" and mutation.node_id == expected_node
    if isinstance(fact, SubagentFact):
        return fact.phase == "started" and mutation.node_id == fact.subagent_id
    return False


def graph_failure_sequence(
    current: int | None,
    mutation: TraceGraphNodeMutation,
    *,
    source_events: Mapping[int, TraceEvent],
) -> int | None:
    """Keep failure evidence only for the current failed execution.

    Relationship-only updates preserve evidence. A new execution clears the previous
    failure even when its start and terminal were committed together; its immutable
    Ledger facts and earlier Run revision remain available for historical queries.
    """

    if (
        mutation.status is not None
        and mutation.status is not TraceGraphNodeStatus.FAILED
    ):
        return None
    if graph_execution_started(mutation, source_events=source_events):
        return mutation.failure_seq
    return mutation.failure_seq or current


def apply_graph_node_mutation(
    revisions: MutableMapping[tuple[str, str], ReducedTraceGraphRevision],
    mutation: TraceGraphNodeMutation,
    *,
    source_events: Mapping[int, TraceEvent],
) -> None:
    """Apply one mutation while preserving immutable event identity fields."""

    storage_key = (mutation.node_id, mutation.run_id)
    if mutation.remove:
        revisions[storage_key] = ReducedTraceGraphTombstone(
            node_id=mutation.node_id,
            run_id=mutation.run_id,
            updated_seq=mutation.updated_seq,
        )
        return
    row = revisions.get(storage_key)
    removed_revision = isinstance(row, ReducedTraceGraphTombstone)
    if removed_revision:
        row = None
    if row is None:
        if (
            mutation.kind is None
            or mutation.status is None
            or mutation.name is None
            or mutation.graph_namespace is None
            or mutation.started_at is None
            or mutation.started_seq is None
        ):
            if removed_revision:
                raise TraceStoreProtocolError(
                    "Trace Graph removal revision cannot accept a partial update"
                )
            return
        revisions[storage_key] = ReducedTraceGraphNode(
            node_id=mutation.node_id,
            parent_subagent_id=mutation.parent_subagent_id,
            model_call_id=mutation.model_call_id,
            model_call_seq=mutation.model_call_seq,
            kind=mutation.kind,
            status=mutation.status,
            name=mutation.name,
            run_id=mutation.run_id,
            graph_namespace=mutation.graph_namespace,
            agent_name=mutation.agent_name,
            provider=mutation.provider,
            model=mutation.model,
            started_at=mutation.started_at,
            first_output_at=mutation.first_output_at,
            completed_at=mutation.completed_at,
            started_seq=mutation.started_seq,
            updated_seq=mutation.updated_seq,
            request_seq=mutation.request_seq,
            result_seq=mutation.result_seq,
            failure_seq=mutation.failure_seq,
            link_issue=resolve_graph_link_issue(
                mutation.parent_subagent_id,
                mutation.model_call_id,
                mutation.link_issue,
            ),
        )
        return
    if not isinstance(row, ReducedTraceGraphNode):
        raise TraceStoreProtocolError("Trace Graph revision type is invalid")
    if mutation.updated_seq < row.updated_seq:
        raise TraceStoreProtocolError("Trace Graph node sequence moved backwards")
    row.kind = cast(
        TraceGraphNodeKind,
        resolve_graph_node_kind(row.kind, mutation.kind),
    )
    if mutation.name is not None and mutation.name != row.name:
        raise TraceStoreProtocolError("Trace Graph node name changed")
    if (
        mutation.graph_namespace is not None
        and mutation.graph_namespace != row.graph_namespace
    ):
        raise TraceStoreProtocolError("Trace Graph node namespace changed")
    if mutation.parent_subagent_id is not None:
        if (
            row.parent_subagent_id is not None
            and mutation.parent_subagent_id != row.parent_subagent_id
        ):
            raise TraceStoreProtocolError("Trace Graph Subagent owner changed")
        row.parent_subagent_id = mutation.parent_subagent_id
    if mutation.model_call_id is not None:
        if (
            row.model_call_id is not None
            and mutation.model_call_id != row.model_call_id
        ):
            raise TraceStoreProtocolError("Trace Graph model call changed")
        row.model_call_id = mutation.model_call_id
        row.model_call_seq = mutation.model_call_seq
    replace_tool_start = graph_execution_started(mutation, source_events=source_events)
    if replace_tool_start:
        row.started_at = cast(datetime, mutation.started_at)
        row.started_seq = cast(int, mutation.started_seq)
        row.result_seq = None
    if mutation.status is not None:
        row.status = mutation.status
        if mutation.status in {
            TraceGraphNodeStatus.RUNNING,
            TraceGraphNodeStatus.WAITING,
        }:
            row.completed_at = None
    if mutation.agent_name is not None:
        row.agent_name = mutation.agent_name
    if mutation.provider is not None:
        row.provider = mutation.provider
    if mutation.model is not None:
        row.model = mutation.model
    if mutation.first_output_at is not None:
        row.first_output_at = mutation.first_output_at
    if mutation.completed_at is not None:
        row.completed_at = mutation.completed_at
    if mutation.request_seq is not None:
        row.request_seq = mutation.request_seq
    if mutation.result_seq is not None:
        row.result_seq = mutation.result_seq
    row.failure_seq = graph_failure_sequence(
        row.failure_seq, mutation, source_events=source_events
    )
    row.link_issue = resolve_graph_link_issue(
        row.parent_subagent_id,
        row.model_call_id,
        row.link_issue,
    )
    resolved_issue = resolve_graph_link_issue(
        row.parent_subagent_id,
        row.model_call_id,
        mutation.link_issue,
    )
    if resolved_issue is not None and row.link_issue is None:
        row.link_issue = resolved_issue
    row.updated_seq = mutation.updated_seq


def effective_graph_nodes(
    revisions: Iterable[ReducedTraceGraphRevision],
    *,
    run_ids: frozenset[str],
) -> tuple[ReducedTraceGraphNode, ...]:
    """Merge selected-lineage revisions into one current event per stable ID."""

    grouped: dict[str, list[ReducedTraceGraphRevision]] = {}
    for revision in revisions:
        if revision.run_id in run_ids:
            grouped.setdefault(revision.node_id, []).append(revision)
    effective: list[ReducedTraceGraphNode] = []
    for node_id, values in grouped.items():
        ordered = sorted(values, key=lambda item: item.updated_seq)
        latest = ordered[-1]
        if isinstance(latest, ReducedTraceGraphTombstone):
            continue
        nodes = [item for item in ordered if isinstance(item, ReducedTraceGraphNode)]
        origin = min(nodes, key=lambda item: item.started_seq)

        def latest_value(name: str) -> object | None:
            return next(
                (
                    value
                    for item in reversed(nodes)
                    if (value := getattr(item, name)) is not None
                ),
                None,
            )

        execution_starts = [
            item
            for item in nodes
            if item.request_seq is not None and item.request_seq == item.started_seq
        ]
        timing_origin = (
            max(execution_starts, key=lambda item: item.updated_seq)
            if execution_starts
            and latest.kind in {TraceGraphNodeKind.TOOL, TraceGraphNodeKind.SUBAGENT}
            else origin
        )
        # A logical Tool can execute again in another Run. Locators before its
        # latest execution belong to the earlier result, not the current delivery.
        execution_boundary = (
            timing_origin.started_seq
            if latest.kind in {TraceGraphNodeKind.TOOL, TraceGraphNodeKind.SUBAGENT}
            else 0
        )
        completed_at = (
            None
            if latest.status
            in {
                TraceGraphNodeStatus.RUNNING,
                TraceGraphNodeStatus.WAITING,
            }
            else cast(datetime | None, latest_value("completed_at"))
        )
        effective.append(
            ReducedTraceGraphNode(
                node_id=node_id,
                parent_subagent_id=cast(str | None, latest_value("parent_subagent_id")),
                model_call_id=cast(str | None, latest_value("model_call_id")),
                model_call_seq=cast(int | None, latest_value("model_call_seq")),
                kind=latest.kind,
                status=latest.status,
                name=latest.name,
                run_id=latest.run_id,
                graph_namespace=latest.graph_namespace,
                agent_name=cast(str | None, latest_value("agent_name")),
                provider=cast(str | None, latest_value("provider")),
                model=cast(str | None, latest_value("model")),
                started_at=timing_origin.started_at,
                first_output_at=min(
                    (
                        item.first_output_at
                        for item in nodes
                        if item.first_output_at is not None
                    ),
                    default=None,
                ),
                completed_at=completed_at,
                started_seq=timing_origin.started_seq,
                updated_seq=latest.updated_seq,
                request_seq=max(
                    (
                        item.request_seq
                        for item in nodes
                        if item.request_seq is not None
                    ),
                    default=None,
                ),
                result_seq=max(
                    (
                        item.result_seq
                        for item in nodes
                        if item.result_seq is not None
                        and item.result_seq >= execution_boundary
                    ),
                    default=None,
                ),
                failure_seq=max(
                    (
                        item.failure_seq
                        for item in nodes
                        if item.failure_seq is not None
                        and item.failure_seq >= execution_boundary
                        and latest.status is TraceGraphNodeStatus.FAILED
                    ),
                    default=None,
                ),
                link_issue=resolve_graph_link_issue(
                    cast(str | None, latest_value("parent_subagent_id")),
                    cast(str | None, latest_value("model_call_id")),
                    cast(TraceGraphLinkIssue | None, latest_value("link_issue")),
                ),
            )
        )
    return tuple(effective)


def assistant_run_terminal_status(fact: RunFact) -> TraceGraphNodeStatus | None:
    """Close unmatched Assistant delivery without inventing a model result.

    A Run terminal proves its remaining active deliveries have stopped. Only explicit
    cancellation cancels them; an interrupt waits, and any other missing result is
    abandoned. Completed messages and independent interactions are never changed.
    """

    if fact.phase != "terminal":
        return None
    if fact.outcome == "cancelled":
        return TraceGraphNodeStatus.CANCELLED
    if fact.outcome == "interrupted":
        return TraceGraphNodeStatus.WAITING
    return TraceGraphNodeStatus.ABANDONED


def assistant_run_terminal_mutations(
    event: TraceEvent,
    revisions: Iterable[ReducedTraceGraphRevision],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Derive only same-Run running Assistant closures from a committed terminal."""

    fact = event.fact
    if not isinstance(fact, RunFact):
        return ()
    status = assistant_run_terminal_status(fact)
    if status is None:
        return ()
    return tuple(
        TraceGraphNodeMutation(
            node_id=node.node_id,
            run_id=node.run_id,
            updated_seq=event.trace_seq,
            status=status,
            completed_at=_terminal_time(status, fact.occurred_at),
        )
        for node in revisions
        if isinstance(node, ReducedTraceGraphNode)
        and node.run_id == fact.identity.run_id
        and node.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
        and node.status is TraceGraphNodeStatus.RUNNING
        and node.updated_seq < event.trace_seq
    )


def apply_graph_events(
    revisions: MutableMapping[tuple[str, str], ReducedTraceGraphRevision],
    events: tuple[TraceEvent, ...],
) -> None:
    """Replay explicit facts and proven Run closure with identical prefix semantics."""

    source_events = {event.trace_seq: event for event in events}
    for event in events:
        for mutation in graph_node_mutations((event,)):
            apply_graph_node_mutation(revisions, mutation, source_events=source_events)
        for mutation in assistant_run_terminal_mutations(event, revisions.values()):
            apply_graph_node_mutation(revisions, mutation, source_events=source_events)


def reduce_graph_mutations(
    mutations: Iterable[TraceGraphNodeMutation],
    *,
    source_events: Mapping[int, TraceEvent],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Collapse sequential rebuild mutations to one complete revision per key."""

    revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
    for mutation in mutations:
        apply_graph_node_mutation(revisions, mutation, source_events=source_events)
    return graph_revision_mutations(revisions.values())


def graph_revision_mutations(
    revisions: Iterable[ReducedTraceGraphRevision],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Serialize already reduced nodes for one atomic disposable-index rebuild."""

    reduced: list[TraceGraphNodeMutation] = []
    for revision in revisions:
        if isinstance(revision, ReducedTraceGraphTombstone):
            reduced.append(
                TraceGraphNodeMutation(
                    node_id=revision.node_id,
                    updated_seq=revision.updated_seq,
                    run_id=revision.run_id,
                    remove=True,
                )
            )
            continue
        reduced.append(
            TraceGraphNodeMutation(
                node_id=revision.node_id,
                updated_seq=revision.updated_seq,
                run_id=revision.run_id,
                kind=revision.kind,
                status=revision.status,
                name=revision.name,
                parent_subagent_id=revision.parent_subagent_id,
                model_call_id=revision.model_call_id,
                model_call_seq=revision.model_call_seq,
                graph_namespace=revision.graph_namespace,
                agent_name=revision.agent_name,
                provider=revision.provider,
                model=revision.model,
                started_at=revision.started_at,
                first_output_at=revision.first_output_at,
                completed_at=revision.completed_at,
                started_seq=revision.started_seq,
                request_seq=revision.request_seq,
                result_seq=revision.result_seq,
                failure_seq=revision.failure_seq,
                link_issue=revision.link_issue,
            )
        )
    return tuple(reduced)


__all__ = [
    "ReducedTraceGraphNode",
    "ReducedTraceGraphRevision",
    "ReducedTraceGraphTombstone",
    "apply_graph_node_mutation",
    "effective_graph_nodes",
    "graph_node_mutations",
    "reduce_graph_mutations",
    "resolve_graph_link_issue",
    "resolve_graph_node_kind",
]
