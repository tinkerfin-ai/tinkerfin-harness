"""Immutable public Trace views and live semantic update values."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, JsonValue, computed_field, field_validator

from ._models import TraceModel
from .facts import TraceEvent, TraceSemanticFact
from .graph import TraceGraphDelta


class TraceMessage(TraceModel, frozen=True):
    """One projected message with the content actually retained from delivery.

    ``completed`` means delivery ended or paused, not that the provider succeeded.
    The corresponding Graph node distinguishes success, cancellation, waiting, and
    abandonment. A later resumed delivery can return the message to ``streaming``.
    """

    id: str
    trace_seq: int = Field(ge=1)
    source_id: str | None = None
    graph_namespace: tuple[str, ...] = ()
    run_id: str
    role: Literal["user", "assistant", "tool", "system", "other"]
    content: JsonValue | None = None
    content_omitted: bool = False
    name: str | None = None
    tool_call_id: str | None = None
    status: Literal["streaming", "completed"]
    created_at: datetime
    completed_at: datetime | None = None


class TraceReasoning(TraceModel, frozen=True):
    """One explicitly enabled provider reasoning stream for a scoped message."""

    id: str
    trace_seq: int = Field(ge=1)
    message_id: str
    graph_namespace: tuple[str, ...] = ()
    run_id: str
    extractor: str
    content: JsonValue | None = None
    content_omitted: bool = False
    status: Literal["streaming", "completed"]
    created_at: datetime
    completed_at: datetime | None = None


class TraceInteraction(TraceModel, frozen=True):
    """One approval, clarification, or review interaction.

    ``source_id`` preserves the canonical native interrupt identity. Protocol adapters
    may derive their documented public action IDs from it without forcing the Ledger
    to retain an AG-UI event copy.
    """

    id: str
    trace_seq: int = Field(ge=1)
    source_id: str
    graph_namespace: tuple[str, ...] = ()
    run_id: str
    kind: str
    tool_call_ids: tuple[str, ...] = ()
    status: Literal["pending", "resolved", "cancelled"]
    payload: JsonValue | None = None
    payload_omitted: bool = False
    opened_at: datetime
    resolved_at: datetime | None = None


class TraceState(TraceModel, frozen=True):
    """Latest complete root state plus isolated subgraph state snapshots."""

    root: dict[str, JsonValue] = Field(default_factory=dict)
    subgraphs: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)


class TraceStatus(TraceModel, frozen=True):
    """Current Agent execution status for the selected head lineage."""

    execution: Literal[
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
        "unknown",
    ]
    head_run_id: str


class TraceCompleteness(TraceModel, frozen=True):
    """Describe structural or capture gaps without overloading execution status."""

    missing_prefix: bool = False
    missing_tail: bool = False
    payload_omitted: bool = False


class TraceSummary(TraceModel, frozen=True):
    """Return complete cumulative status for one selected fixed-as-of lineage.

    Counts, pending interactions, and the latest source time are independent of the
    visible history window. Sibling branches never contribute to this value.
    """

    status: TraceStatus
    completeness: TraceCompleteness
    message_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    pending_interactions: tuple[TraceInteraction, ...] = ()
    last_occurred_at: datetime

    @field_validator("last_occurred_at")
    @classmethod
    def last_occurred_at_is_utc(cls, value: datetime) -> datetime:
        """Require one comparable source timestamp without rewriting evidence."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("last_occurred_at must be an aware UTC timestamp")
        return value


class TraceEventPage(TraceModel, frozen=True):
    """One fixed-as-of ascending page of safe Ledger events."""

    items: tuple[TraceEvent, ...]
    next_cursor: str | None = None
    page_as_of_seq: int


EntityT = TypeVar("EntityT", bound=BaseModel, covariant=True)


class TraceEntityDelta(TraceModel, Generic[EntityT], frozen=True):
    """Upsert and remove immutable entities by their stable IDs."""

    upserts: tuple[EntityT, ...] = ()
    removes: tuple[str, ...] = ()


class TraceUpdate(TraceModel, frozen=True):
    """A committed event update or execution-status change at the same sequence.

    Writer close or lease expiry can change status and completeness with empty
    ``events`` and ``facts``. Consumers must apply those updates even when
    ``as_of_seq`` has not advanced; no Agent terminal is inferred from ownership.
    """

    generation: str = Field(min_length=1)
    observed_at: datetime = Field(
        description="Storage UTC time of this event and ownership observation"
    )
    as_of_seq: int
    events: tuple[TraceEvent, ...]
    facts: tuple[TraceSemanticFact, ...]
    messages: TraceEntityDelta[TraceMessage]
    reasoning: TraceEntityDelta[TraceReasoning]
    graph: TraceGraphDelta
    interactions: TraceEntityDelta[TraceInteraction]
    state: TraceState
    summary: TraceSummary
    projections: dict[str, JsonValue] = Field(default_factory=dict)

    @computed_field
    @property
    def status(self) -> TraceStatus:
        """Return the cumulative status retained in :attr:`summary`."""

        return self.summary.status

    @computed_field
    @property
    def completeness(self) -> TraceCompleteness:
        """Return cumulative completeness retained in :attr:`summary`."""

        return self.summary.completeness

    @computed_field
    @property
    def message_count(self) -> int:
        """Return the cumulative selected-lineage message count."""

        return self.summary.message_count

    @computed_field
    @property
    def tool_call_count(self) -> int:
        """Return the cumulative selected-lineage Tool call count."""

        return self.summary.tool_call_count


__all__ = [
    "TraceCompleteness",
    "TraceEntityDelta",
    "TraceEventPage",
    "TraceInteraction",
    "TraceMessage",
    "TraceReasoning",
    "TraceState",
    "TraceStatus",
    "TraceSummary",
    "TraceUpdate",
]
