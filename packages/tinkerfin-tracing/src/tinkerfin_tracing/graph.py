"""Canonical flat execution-timeline values exposed by TinkerFin Tracing."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ._models import TraceModel

MAX_SUBAGENT_SCOPE_DEPTH = 64
MAX_TRACE_GRAPH_LINEAGE_RUNS = 10_000


class TraceGraphNodeKind(StrEnum):
    """Semantic event kinds available to graph consumers and filters."""

    HUMAN_MESSAGE = "human_message"
    ASSISTANT_MESSAGE = "assistant_message"
    CONTEXT = "context"
    MODEL = "model"
    TOOL = "tool"
    SUBAGENT = "subagent"
    MEMORY = "memory"
    GUARDRAIL = "guardrail"
    RETRIEVAL = "retrieval"
    CUSTOM = "custom"
    PLAN = "plan"
    INTERACTION = "interaction"


class TraceGraphNodeStatus(StrEnum):
    """Current lifecycle status of one canonical timeline event."""

    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class TraceGraphLinkIssue(StrEnum):
    """Explicit missing evidence that prevented one exact relationship."""

    MISSING_SUBAGENT = "missing_subagent"
    MISSING_MODEL_CALL = "missing_model_call"
    MISSING_TOOL_PROPOSAL = "missing_tool_proposal"


class TraceGraphFilter(TraceModel, frozen=True):
    """Select timeline events through bounded Store-side predicates.

    Parent Subagent containers are added automatically after direct selection so a
    filtered result retains its execution scope. They never become direct matches.
    """

    kinds: AbstractSet[TraceGraphNodeKind] = Field(default=frozenset(), strict=False)
    statuses: AbstractSet[TraceGraphNodeStatus] = Field(
        default=frozenset(), strict=False
    )
    model_call_id: str | None = Field(default=None, min_length=1, max_length=2048)
    agent_names: AbstractSet[str] = Field(default=frozenset(), strict=False)
    providers: AbstractSet[str] = Field(default=frozenset(), strict=False)
    models: AbstractSet[str] = Field(default=frozenset(), strict=False)
    graph_namespaces: AbstractSet[tuple[str, ...]] = Field(
        default=frozenset(),
        strict=False,
        description="Exact graph scopes; the empty tuple selects the root graph",
    )
    search: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Literal substring across visible metadata and retained public details; "
            "ASCII queries fold only ASCII A-Z while non-ASCII queries are case-sensitive"
        ),
    )
    started_after: datetime | None = Field(
        default=None,
        description="Exclusive UTC lower bound for event start time",
    )
    started_before: datetime | None = Field(
        default=None,
        description="Exclusive UTC upper bound for event start time",
    )

    @field_validator("agent_names", "providers", "models")
    @classmethod
    def names_are_canonical(cls, values: AbstractSet[str]) -> AbstractSet[str]:
        """Reject oversized, blank, or whitespace-aliased filter values."""

        if len(values) > 64:
            raise ValueError("Trace Graph filters accept at most 64 values per field")
        if any(len(value) > 1024 for value in values):
            raise ValueError("Trace Graph filter names accept at most 1024 characters")
        if any(not value or value != value.strip() for value in values):
            raise ValueError("Trace Graph filter names must be canonical text")
        return values

    @field_validator("graph_namespaces")
    @classmethod
    def namespaces_are_bounded(
        cls,
        values: AbstractSet[tuple[str, ...]],
    ) -> AbstractSet[tuple[str, ...]]:
        """Bound graph scopes and reject ambiguous graph namespace segments."""

        if len(values) > 64:
            raise ValueError("Trace Graph filters accept at most 64 graph namespaces")
        if any(len(graph_namespace) > 64 for graph_namespace in values):
            raise ValueError("Trace Graph namespaces accept at most 64 segments")
        if any(len(segment) > 1024 for value in values for segment in value):
            raise ValueError("Trace Graph namespace segments are too long")
        if any(
            not segment or segment != segment.strip()
            for value in values
            for segment in value
        ):
            raise ValueError("Trace Graph namespace segments must be canonical text")
        return values

    @field_validator("started_after", "started_before")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require comparable UTC filter boundaries."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace Graph filter times must be aware UTC values")
        return value

    @model_validator(mode="after")
    def values_are_consistent(self) -> TraceGraphFilter:
        """Reject ambiguous text and empty or reversed time windows."""

        if self.search is not None and self.search != self.search.strip():
            raise ValueError("search must be canonical text")
        if (
            self.model_call_id is not None
            and self.model_call_id != self.model_call_id.strip()
        ):
            raise ValueError("model_call_id must be canonical text")
        if (
            self.started_after is not None
            and self.started_before is not None
            and self.started_after >= self.started_before
        ):
            raise ValueError("started_after must precede started_before")
        return self


class TraceGraphQueryLimits(TraceModel, frozen=True):
    """Bound direct matches, Subagent expansion, and serialized Graph pages."""

    max_direct_nodes: int = Field(default=1000, ge=1, le=10_000)
    max_total_nodes: int = Field(default=4000, ge=1, le=20_000)
    max_page_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def direct_nodes_fit_total_nodes(self) -> TraceGraphQueryLimits:
        """Keep direct selection inside the complete page node budget."""

        if self.max_direct_nodes > self.max_total_nodes:
            raise ValueError("max_direct_nodes must not exceed max_total_nodes")
        return self


class TraceGraphFailure(TraceModel, frozen=True):
    """Failure retained only by the timeline event that owns it."""

    error_type: str = Field(min_length=1, max_length=1024)
    message: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("error_type")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject whitespace aliases in machine-readable failure identifiers."""

        if value is not None and value != value.strip():
            raise ValueError("Trace Graph failure identifiers must be canonical")
        return value


class TraceGraphTurn(TraceModel, frozen=True):
    """One selected-lineage user turn that owns a flat root execution scope."""

    id: str = Field(min_length=1, max_length=2048)
    ordinal: int = Field(ge=1)
    started_at: datetime

    @field_validator("id")
    @classmethod
    def id_is_canonical(cls, value: str) -> str:
        """Reject whitespace aliases in Turn identities."""

        if value != value.strip():
            raise ValueError("Trace Graph Turn IDs must be canonical")
        return value

    @field_validator("started_at")
    @classmethod
    def started_at_is_utc(cls, value: datetime) -> datetime:
        """Require a comparable source timestamp for Turn ordering."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Trace Graph Turn time must be aware UTC")
        return value


class TraceGraphNode(TraceModel, frozen=True):
    """One canonical event in a Turn or nested Subagent execution scope."""

    id: str = Field(min_length=1, max_length=2048)
    turn_id: str = Field(min_length=1, max_length=2048)
    parent_subagent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Nearest owning Subagent; null selects the Turn root scope",
    )
    context_kind: (
        Literal["memory", "guardrail", "retrieval", "custom", "compaction"] | None
    ) = None
    compaction_origin: Literal["manual", "automatic", "tool"] | None = None
    parent_node_id: str | None = Field(
        default=None, description="Explicit containing context action or tool"
    )
    model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Provider call that emitted this Assistant, Tool, or Subagent",
    )
    kind: TraceGraphNodeKind
    status: TraceGraphNodeStatus
    name: str = Field(min_length=1, max_length=1024)
    run_id: str = Field(min_length=1, max_length=1024)
    graph_namespace: tuple[str, ...] = ()
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    provider: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=1024)
    source_id: str | None = Field(default=None, min_length=1, max_length=2048)
    started_at: datetime
    first_output_at: datetime | None = None
    completed_at: datetime | None = None
    started_seq: int = Field(ge=1)
    updated_seq: int = Field(ge=1)
    content: JsonValue | None = None
    content_omitted: bool = False
    tool_call_only: bool = Field(
        default=False,
        description=(
            "Whether this Assistant emitted Tool calls without user-visible content"
        ),
    )
    request: JsonValue | None = None
    request_omitted: bool = False
    result: JsonValue | None = None
    result_omitted: bool = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceGraphFailure | None = None
    link_issues: tuple[TraceGraphLinkIssue, ...] = ()

    @field_validator(
        "id",
        "turn_id",
        "parent_subagent_id",
        "model_call_id",
        "name",
        "run_id",
        "agent_name",
        "provider",
        "model",
        "source_id",
    )
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject whitespace aliases in event identity and searchable metadata."""

        if value is not None and value != value.strip():
            raise ValueError("Trace Graph event identifiers must be canonical")
        return value

    @field_validator("graph_namespace")
    @classmethod
    def namespace_is_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Bound Subagent scope paths and reject ambiguous segments."""

        if len(value) > MAX_SUBAGENT_SCOPE_DEPTH:
            raise ValueError("Trace Graph namespaces accept at most 64 segments")
        if any(
            not segment or len(segment) > 1024 or segment != segment.strip()
            for segment in value
        ):
            raise ValueError("Trace Graph namespace segments must be canonical")
        return value

    @field_validator("started_at", "first_output_at", "completed_at")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require source timestamps that can be compared across event kinds."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace Graph event times must be aware UTC")
        return value

    @model_validator(mode="after")
    def lifecycle_is_ordered(self) -> TraceGraphNode:
        """Require exact scope, sequence, status, and timestamp relationships."""

        if self.updated_seq < self.started_seq:
            raise ValueError("updated_seq cannot precede started_seq")
        if self.first_output_at is not None and self.first_output_at < self.started_at:
            raise ValueError("first output cannot precede event start")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completion cannot precede event start")
        if (
            self.first_output_at is not None
            and self.completed_at is not None
            and self.first_output_at > self.completed_at
        ):
            raise ValueError("first output cannot follow event completion")
        if (
            self.status
            in {
                TraceGraphNodeStatus.RUNNING,
                TraceGraphNodeStatus.WAITING,
            }
            and self.completed_at is not None
        ):
            raise ValueError("non-terminal events cannot carry completion time")
        if (
            self.status
            in {
                TraceGraphNodeStatus.SUCCEEDED,
                TraceGraphNodeStatus.FAILED,
                TraceGraphNodeStatus.CANCELLED,
                TraceGraphNodeStatus.ABANDONED,
            }
            and self.completed_at is None
        ):
            raise ValueError("terminal events require completion time")
        if self.failure is not None and self.status is not TraceGraphNodeStatus.FAILED:
            raise ValueError("failure details require failed event status")
        if self.model_call_id is not None and self.kind not in {
            TraceGraphNodeKind.ASSISTANT_MESSAGE,
            TraceGraphNodeKind.TOOL,
            TraceGraphNodeKind.SUBAGENT,
        }:
            raise ValueError("model_call_id belongs only to emitted execution events")
        if (
            self.tool_call_only
            and self.kind is not TraceGraphNodeKind.ASSISTANT_MESSAGE
        ):
            raise ValueError("tool_call_only belongs only to AssistantMessage events")
        if self.tool_call_only and self.content_omitted:
            raise ValueError(
                "tool_call_only requires observed AssistantMessage content"
            )
        if len(set(self.link_issues)) != len(self.link_issues):
            raise ValueError("Trace Graph link issues must be unique")
        return self


_GRAPH_KIND_ORDER = {
    TraceGraphNodeKind.HUMAN_MESSAGE: 0,
    TraceGraphNodeKind.CONTEXT: 1,
    TraceGraphNodeKind.MODEL: 2,
    TraceGraphNodeKind.TOOL: 3,
    TraceGraphNodeKind.SUBAGENT: 4,
    TraceGraphNodeKind.ASSISTANT_MESSAGE: 5,
}


def _validate_trace_graph_subagent_scopes(
    nodes: Mapping[
        str,
        tuple[str | None, TraceGraphNodeKind],
    ],
) -> None:
    """Validate every Subagent parent chain without using language recursion."""

    resolved_depths: dict[str, int] = {}
    for origin_id in nodes:
        if origin_id in resolved_depths:
            continue
        path: list[str] = []
        path_ids: set[str] = set()
        current_id = origin_id
        while current_id not in resolved_depths:
            if current_id in path_ids:
                raise ValueError("Trace Graph Subagent scopes cannot form a cycle")
            path.append(current_id)
            path_ids.add(current_id)
            parent_id, _kind = nodes[current_id]
            if parent_id is None:
                break
            parent = nodes.get(parent_id)
            if parent is None or parent[1] is not TraceGraphNodeKind.SUBAGENT:
                raise ValueError("Trace Graph scope parents must be returned Subagents")
            current_id = parent_id

        for node_id in reversed(path):
            parent_id, kind = nodes[node_id]
            parent_depth = 0 if parent_id is None else resolved_depths[parent_id]
            depth = parent_depth + (1 if kind is TraceGraphNodeKind.SUBAGENT else 0)
            if depth > MAX_SUBAGENT_SCOPE_DEPTH:
                raise ValueError("Trace Graph Subagent scopes accept at most 64 levels")
            resolved_depths[node_id] = depth


def canonical_trace_graph_node_order(
    turns: tuple[TraceGraphTurn, ...],
    nodes: Mapping[str, TraceGraphNode],
) -> tuple[str, ...]:
    """Validate Subagent scopes and return their canonical iterative DFS order."""

    _validate_trace_graph_subagent_scopes(
        {
            node_id: (node.parent_subagent_id, node.kind)
            for node_id, node in nodes.items()
        }
    )
    by_turn: dict[str, list[TraceGraphNode]] = {}
    for node in nodes.values():
        by_turn.setdefault(node.turn_id, []).append(node)
    ordered: list[str] = []
    visited: set[str] = set()
    for turn in sorted(turns, key=lambda item: (item.ordinal, item.id)):
        children: dict[str | None, list[TraceGraphNode]] = {}
        for node in by_turn.get(turn.id, ()):
            parent_id = node.parent_subagent_id
            if parent_id is not None:
                parent = nodes.get(parent_id)
                if parent is None or parent.kind is not TraceGraphNodeKind.SUBAGENT:
                    raise ValueError(
                        "Trace Graph scope parents must be returned Subagents"
                    )
                if parent.turn_id != node.turn_id:
                    raise ValueError(
                        "Trace Graph scope parents must share the child Turn"
                    )
            children.setdefault(parent_id, []).append(node)
        for values in children.values():
            values.sort(
                key=lambda node: (
                    node.started_seq,
                    _GRAPH_KIND_ORDER.get(node.kind, 1),
                    node.id,
                )
            )
        stack = list(reversed(children.get(None, ())))
        while stack:
            node = stack.pop()
            if node.id in visited:
                raise ValueError("Trace Graph Subagent scopes cannot form a cycle")
            visited.add(node.id)
            ordered.append(node.id)
            if node.kind is TraceGraphNodeKind.SUBAGENT:
                stack.extend(reversed(children.get(node.id, ())))
    if len(visited) != len(nodes):
        raise ValueError("Trace Graph Subagent scopes cannot form a cycle")
    return tuple(ordered)


class TraceGraphCompleteness(TraceModel, frozen=True):
    """Expose missing call or relationship evidence without inventing events."""

    call_tracking_missing: bool = Field(
        default=False,
        description=(
            "Some selected Runs lack call tracking and have no proven failure "
            "before Agent execution"
        ),
    )
    relationship_evidence_missing: bool = False
    details_omitted: bool = False


class TraceGraph(TraceModel, frozen=True):
    """One authoritative ordered timeline projection at an exact Ledger tail."""

    turns: tuple[TraceGraphTurn, ...] = ()
    nodes: tuple[TraceGraphNode, ...] = ()
    ordered_node_ids: tuple[str, ...] = ()
    matched_node_ids: tuple[str, ...] = Field(
        description="Direct filter matches in authoritative display order"
    )
    as_of_seq: int = Field(ge=1)
    completeness: TraceGraphCompleteness = Field(default_factory=TraceGraphCompleteness)

    @model_validator(mode="after")
    def references_are_exact(self) -> TraceGraph:
        """Reject duplicate, missing, cyclic, or out-of-order references."""

        nodes_by_id = {node.id: node for node in self.nodes}
        node_ids = tuple(nodes_by_id)
        if len(nodes_by_id) != len(self.nodes):
            raise ValueError("Trace Graph node IDs must be unique")
        ordered_set = set(self.ordered_node_ids)
        if len(ordered_set) != len(self.ordered_node_ids):
            raise ValueError("Trace Graph ordered node IDs must be unique")
        if ordered_set != set(node_ids):
            raise ValueError(
                "Trace Graph ordering must contain every node exactly once"
            )
        if self.ordered_node_ids != node_ids:
            raise ValueError("Trace Graph nodes must follow authoritative order")
        if any(node.updated_seq > self.as_of_seq for node in self.nodes):
            raise ValueError(
                "Trace Graph nodes cannot reference a future Ledger sequence"
            )
        matched_set = set(self.matched_node_ids)
        if len(matched_set) != len(self.matched_node_ids):
            raise ValueError("Trace Graph matched node IDs must be unique")
        if not matched_set <= ordered_set:
            raise ValueError("Trace Graph matches must reference returned nodes")
        expected_matches = tuple(
            node_id for node_id in self.ordered_node_ids if node_id in matched_set
        )
        if self.matched_node_ids != expected_matches:
            raise ValueError("Trace Graph matches must follow authoritative order")
        turn_ids_in_order = tuple(turn.id for turn in self.turns)
        turn_ordinals = tuple(turn.ordinal for turn in self.turns)
        if len(set(turn_ids_in_order)) != len(turn_ids_in_order):
            raise ValueError("Trace Graph Turn IDs must be unique")
        if len(set(turn_ordinals)) != len(turn_ordinals):
            raise ValueError("Trace Graph Turn ordinals must be unique")
        if self.turns != tuple(
            sorted(self.turns, key=lambda turn: (turn.ordinal, turn.id))
        ):
            raise ValueError("Trace Graph Turns must follow ordinal order")
        turn_ids = {turn.id for turn in self.turns}
        if any(node.turn_id not in turn_ids for node in self.nodes):
            raise ValueError("Trace Graph nodes must reference returned Turns")
        expected_order = canonical_trace_graph_node_order(self.turns, nodes_by_id)
        if self.ordered_node_ids != expected_order:
            raise ValueError("Trace Graph node order is not canonical")
        return self


class TraceGraphPage(TraceGraph, frozen=True):
    """One directly filtered timeline page with an optional older-page cursor."""

    next_cursor: str | None = None


class TraceGraphDelta(TraceModel, frozen=True):
    """Replace changed timeline values while retaining authoritative display order."""

    as_of_seq: int = Field(ge=1)
    next_cursor: str | None
    turn_upserts: tuple[TraceGraphTurn, ...] = ()
    turn_removes: tuple[str, ...] = ()
    node_upserts: tuple[TraceGraphNode, ...] = ()
    node_removes: tuple[str, ...] = ()
    ordered_node_ids: tuple[str, ...] = ()
    matched_node_ids: tuple[str, ...] = Field(
        description="Complete current direct-match set in authoritative order"
    )
    completeness: TraceGraphCompleteness

    @model_validator(mode="after")
    def references_follow_authoritative_order(self) -> TraceGraphDelta:
        """Reject duplicate or out-of-order match references."""

        for values, maximum, label in (
            (self.ordered_node_ids, 20_000, "ordered node"),
            (self.matched_node_ids, 10_000, "matched node"),
            (self.node_removes, 20_000, "removed node"),
            (self.turn_removes, 20_000, "removed Turn"),
        ):
            if len(values) > maximum or any(
                not value or len(value) > 2048 or value != value.strip()
                for value in values
            ):
                raise ValueError(f"Trace Graph Delta {label} IDs are invalid")
        ordered_set = set(self.ordered_node_ids)
        if len(ordered_set) != len(self.ordered_node_ids):
            raise ValueError("Trace Graph Delta ordered node IDs must be unique")
        matched_set = set(self.matched_node_ids)
        if len(matched_set) != len(self.matched_node_ids):
            raise ValueError("Trace Graph Delta matched node IDs must be unique")
        if not matched_set <= ordered_set:
            raise ValueError("Trace Graph Delta matches must reference ordered nodes")
        expected_matches = tuple(
            node_id for node_id in self.ordered_node_ids if node_id in matched_set
        )
        if self.matched_node_ids != expected_matches:
            raise ValueError(
                "Trace Graph Delta matches must follow authoritative order"
            )
        turn_upsert_ids = tuple(turn.id for turn in self.turn_upserts)
        node_upsert_ids = tuple(node.id for node in self.node_upserts)
        if len(set(turn_upsert_ids)) != len(turn_upsert_ids):
            raise ValueError("Trace Graph Delta Turn upserts must be unique")
        if len(set(self.turn_removes)) != len(self.turn_removes):
            raise ValueError("Trace Graph Delta Turn removals must be unique")
        if set(turn_upsert_ids) & set(self.turn_removes):
            raise ValueError("Trace Graph Delta cannot upsert and remove one Turn")
        if len(set(node_upsert_ids)) != len(node_upsert_ids):
            raise ValueError("Trace Graph Delta node upserts must be unique")
        if len(set(self.node_removes)) != len(self.node_removes):
            raise ValueError("Trace Graph Delta node removals must be unique")
        if set(node_upsert_ids) & set(self.node_removes):
            raise ValueError("Trace Graph Delta cannot upsert and remove one node")
        if not set(node_upsert_ids) <= ordered_set:
            raise ValueError("Trace Graph Delta upserts must remain in node order")
        if any(node.updated_seq > self.as_of_seq for node in self.node_upserts):
            raise ValueError(
                "Trace Graph Delta nodes cannot reference a future Ledger sequence"
            )
        return self


def _omit_graph_node_details(node: TraceGraphNode) -> TraceGraphNode:
    return node.model_copy(
        update={
            "content": None,
            "content_omitted": node.content_omitted or node.content is not None,
            "request": None,
            "request_omitted": node.request_omitted or node.request is not None,
            "result": None,
            "result_omitted": node.result_omitted or node.result is not None,
            "usage": None,
            "response_metadata": None,
        },
        deep=True,
    )


def _graph_json_bytes(value: TraceModel) -> int:
    return len(value.model_dump_json(by_alias=True, exclude_none=False).encode())


def bound_graph_page(page: TraceGraphPage, *, max_bytes: int) -> TraceGraphPage:
    """Prefer complete structure and explicitly omit details before rejecting a page."""

    if _graph_json_bytes(page) <= max_bytes:
        return page
    bounded = page.model_copy(
        update={
            "nodes": tuple(_omit_graph_node_details(node) for node in page.nodes),
            "completeness": page.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph page exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def bound_graph(graph: TraceGraph, *, max_bytes: int) -> TraceGraph:
    """Prefer complete structure and omit details before rejecting a Graph."""

    if _graph_json_bytes(graph) <= max_bytes:
        return graph
    bounded = graph.model_copy(
        update={
            "nodes": tuple(_omit_graph_node_details(node) for node in graph.nodes),
            "completeness": graph.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def _bound_graph_delta(delta: TraceGraphDelta, *, max_bytes: int) -> TraceGraphDelta:
    """Apply the same deterministic detail omission to live Graph updates."""

    if _graph_json_bytes(delta) <= max_bytes:
        return delta
    bounded = delta.model_copy(
        update={
            "node_upserts": tuple(
                _omit_graph_node_details(node) for node in delta.node_upserts
            ),
            "completeness": delta.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph update exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def graph_delta(
    previous: TraceGraph,
    current: TraceGraph,
    *,
    max_bytes: int,
) -> TraceGraphDelta:
    """Return the bounded canonical replacement delta between two Graphs."""

    previous_nodes = {node.id: node for node in previous.nodes}
    current_nodes = {node.id: node for node in current.nodes}
    previous_turns = {turn.id: turn for turn in previous.turns}
    current_turns = {turn.id: turn for turn in current.turns}
    return _bound_graph_delta(
        TraceGraphDelta(
            as_of_seq=current.as_of_seq,
            next_cursor=(
                current.next_cursor if isinstance(current, TraceGraphPage) else None
            ),
            turn_upserts=tuple(
                turn
                for turn_id, turn in current_turns.items()
                if previous_turns.get(turn_id) != turn
            ),
            turn_removes=tuple(
                turn_id for turn_id in previous_turns if turn_id not in current_turns
            ),
            node_upserts=tuple(
                node
                for node_id, node in current_nodes.items()
                if previous_nodes.get(node_id) != node
            ),
            node_removes=tuple(
                node_id for node_id in previous_nodes if node_id not in current_nodes
            ),
            ordered_node_ids=current.ordered_node_ids,
            matched_node_ids=current.matched_node_ids,
            completeness=current.completeness,
        ),
        max_bytes=max_bytes,
    )


__all__ = [
    "TraceGraph",
    "TraceGraphCompleteness",
    "TraceGraphDelta",
    "TraceGraphFailure",
    "TraceGraphFilter",
    "TraceGraphLinkIssue",
    "TraceGraphNode",
    "TraceGraphNodeKind",
    "TraceGraphNodeStatus",
    "TraceGraphPage",
    "TraceGraphQueryLimits",
    "TraceGraphTurn",
]
