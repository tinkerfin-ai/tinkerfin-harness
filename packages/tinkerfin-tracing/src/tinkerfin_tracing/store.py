"""Replaceable high-level Trace Store and writer contracts."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import Field, JsonValue, field_validator, model_validator

from tinkerfin_contracts import RunIdentity, ThreadIdentity

from ._models import TraceModel
from .errors import TraceStoreProtocolError
from .facts import TraceEvent, TraceSemanticFact
from .graph import (
    TraceGraphFilter,
    TraceGraphLinkIssue,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from .limits import TraceLimits


class TraceThreadKey(ThreadIdentity):
    """Bind a Trace handle to one namespace, thread, and non-reusable generation."""

    generation: str = Field(min_length=1, max_length=2048)

    @field_validator("generation")
    @classmethod
    def components_are_canonical(cls, value: str) -> str:
        """Reject whitespace aliases before a Store operation is selected."""

        if value != value.strip():
            raise ValueError("Trace thread key components must be canonical")
        value.encode("utf-8")
        return value

    @property
    def thread(self) -> ThreadIdentity:
        """Return the complete logical thread without selecting a generation."""

        return ThreadIdentity(namespace=self.namespace, thread_id=self.thread_id)


class StoreWriterSnapshot(TraceModel, frozen=True):
    """Describe one active Run writer at a fixed Store observation point."""

    run_id: str = Field(min_length=1, max_length=1024)
    committed_events: int = Field(ge=0)

    @field_validator("run_id")
    @classmethod
    def run_id_is_canonical(cls, value: str) -> str:
        """Reject aliases before exposing active writer ownership."""

        if value != value.strip():
            raise ValueError("active writer Run ID must be canonical")
        return value


class StoreThreadSnapshot(TraceModel, frozen=True):
    """Return immutable generation, prefix, capacity, and writer metadata."""

    key: TraceThreadKey
    as_of_seq: int = Field(ge=0)
    persisted_bytes: int = Field(ge=0)
    active_writers: tuple[StoreWriterSnapshot, ...]
    observed_at: datetime = Field(
        description="Storage UTC time of the consistent event and ownership read"
    )

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        """Keep ownership observation time separate from the last event time."""
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("observed_at must be aware UTC")
        return value

    @field_validator("active_writers")
    @classmethod
    def active_writers_are_unique(
        cls,
        values: tuple[StoreWriterSnapshot, ...],
    ) -> tuple[StoreWriterSnapshot, ...]:
        """Require a canonical unique active writer set."""

        run_ids = tuple(value.run_id for value in values)
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("active writer Run IDs must be unique")
        return values

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        """Return active Run IDs in the snapshot's canonical writer order."""

        return tuple(writer.run_id for writer in self.active_writers)


@dataclass(frozen=True, slots=True)
class TraceStoreUpdate:
    """Deliver committed events and the currently active Run identities.

    The first update also supplies current ownership when no events follow the
    requested cursor. Later updates with no events report ownership changes, including
    writer close and lease expiry. They preserve ``as_of_seq`` and do not invent an
    Agent result. Event pages remain bounded independently of append transactions.
    """

    as_of_seq: int
    events: tuple[TraceEvent, ...]
    active_run_ids: tuple[str, ...]
    observed_at: datetime


class TraceProjectionCheckpoint(TraceModel, frozen=True):
    """Store one disposable serializable Projection state at an exact prefix."""

    key: TraceThreadKey
    projection_name: str = Field(min_length=1, max_length=2048)
    run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    as_of_seq: int = Field(ge=0)
    state: JsonValue

    @field_validator("projection_name", "run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject whitespace aliases in Projection cache ownership."""

        if value is not None and value != value.strip():
            raise ValueError("Projection checkpoint identifiers must be canonical")
        return value

    @model_validator(mode="after")
    def scoped_checkpoints_require_events(self) -> TraceProjectionCheckpoint:
        """Reserve sequence zero for the unscoped empty core state."""

        if self.run_id is not None and self.as_of_seq == 0:
            raise ValueError("Run-scoped Projection checkpoints require an event")
        return self


@dataclass(frozen=True, slots=True)
class TraceGraphNodeRecord:
    """Carry one indexed Graph node and its decoded authoritative facts.

    Attributes:
        model_call_seq: Ledger sequence proving the emitting Model relationship.
        model_call_event: The corresponding fact, retained even when later lifecycle
            or payload facts omit that relationship.
    """

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
    started_event: TraceEvent
    updated_event: TraceEvent
    request_event: TraceEvent | None
    result_event: TraceEvent | None
    failure_event: TraceEvent | None
    model_call_event: TraceEvent | None


@dataclass(frozen=True, slots=True)
class TraceGraphNodeRecordPage:
    """Return decoded Graph rows and cursor evidence from one current read."""

    key: TraceThreadKey
    as_of_seq: int
    nodes: tuple[TraceGraphNodeRecord, ...]
    matched_node_ids: tuple[str, ...]
    has_more: bool
    next_started_at: datetime | None
    next_node_id: str | None
    relationship_evidence_missing: bool


@runtime_checkable
class TraceWriter(Protocol):
    """Append facts for one Run under one exact thread generation."""

    @property
    def key(self) -> TraceThreadKey:
        """Return the immutable generation bound to this writer."""

        ...

    @property
    def run_id(self) -> str:
        """Return the semantic Run identity owned by this writer."""

        ...

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Atomically append one ordered fact batch.

        ``mandatory=False`` accepts only pre-terminal facts. ``mandatory=True`` spends
        this Run's reserved capacity and accepts the exactly-once terminal followed by
        the exactly-once closed fact, either together or in two ordered transactions.

        Args:
            facts: Non-empty ordered semantic facts for this writer's exact Run.
            mandatory: Whether the transaction may spend terminal reserve.

        Returns:
            Committed immutable events with contiguous global Trace sequences.

        Raises:
            TraceStoreProtocolError: Identity, ordering, ownership, terminal, or
                idempotency invariants fail.
            TraceQuotaExceeded: Ordinary or reserved capacity is exhausted.
            TraceStoreError: The transaction cannot be committed or proven committed.
        """

        ...

    async def aclose(self) -> None:
        """Release writer ownership idempotently.

        A distributed implementation treats ownership already transferred after lease
        expiry as successful local cleanup and must not disturb the replacement fence.

        Raises:
            TraceStoreError: Local heartbeat or owned-resource settlement fails.
        """

        ...


@runtime_checkable
class TraceStore(Protocol):
    """Persist, read, follow, and delete semantic Trace Ledger generations."""

    @property
    def limits(self) -> TraceLimits:
        """Return the immutable limits enforced by this Store."""

        ...

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Open one exclusive writer for a previously unseen Run ID.

        Args:
            identity: Canonical thread and Run identity.

        Returns:
            Writer owning one Run in the current exact generation.

        Raises:
            TraceRunConflict: The Run exists or another live writer conflicts.
            TraceQuotaExceeded: Store thread or byte capacity is exhausted.
            TraceStoreError: Durable writer admission fails.
        """

        ...

    async def snapshot(self, identity: ThreadIdentity) -> StoreThreadSnapshot:
        """Return one consistent event prefix for the current generation.

        Args:
            identity: Complete logical namespace and thread identity.

        Returns:
            Metadata-only generation snapshot with active writer evidence.

        Raises:
            TraceThreadNotFound: The thread has no current generation.
            TraceStoreError: The consistent read fails.
        """

        ...

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return one consistent event prefix for an exact generation.

        Args:
            key: Exact namespace, thread, and generation handle.

        Returns:
            Metadata-only prefix and active writer evidence.

        Raises:
            TraceThreadNotFound: The generation was deleted or replaced.
            TraceStoreError: The consistent read fails.
        """

        ...

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read a bounded ascending page inside one fixed as-of prefix.

        Args:
            key: Exact generation handle.
            after_seq: Exclusive lower global sequence.
            as_of_seq: Inclusive immutable upper global sequence.
            limit: Positive maximum event count.

        Returns:
            Defensive immutable events in ascending sequence order.

        Raises:
            ValueError: Cursor values or limit are invalid.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreProtocolError: Stored event content is corrupt.
            TraceStoreError: The bounded read fails.
        """

        ...

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read a bounded newest-first page below one exclusive sequence.

        Args:
            key: Exact generation handle.
            before_seq: Exclusive upper global sequence.
            limit: Positive maximum event count.

        Returns:
            Defensive immutable events in descending sequence order.

        Raises:
            ValueError: Cursor values or limit are invalid.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The bounded read fails.
        """

        ...

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Return the newest matching checkpoint at or below one fixed prefix.

        Args:
            key: Exact generation handle.
            projection_name: Registered Projection identity.
            run_id: Optional Run-scoped cache identity.
            as_of_seq: Inclusive fixed lookup prefix.

        Returns:
            Defensive checkpoint copy, or ``None`` when no cache is available.

        Raises:
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: Checkpoint lookup fails.
        """

        ...

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        """Compare-and-swap one disposable Projection checkpoint.

        Repeating the exact current checkpoint succeeds even with its prior expected
        prefix so a caller can resolve an unknown commit. The same identity and prefix
        with different state is a Store protocol violation.

        Args:
            checkpoint: Complete disposable cache state to persist.
            expected_as_of_seq: Previous prefix required for compare-and-swap, or
                ``None`` when no prior checkpoint is expected.

        Returns:
            Defensive copy of the committed checkpoint.

        Raises:
            TraceProjectionCheckpointConflict: A newer checkpoint already exists.
            TraceStoreProtocolError: Equal identity/prefix carries different state.
            TraceStoreError: The compare-and-swap cannot be completed or proven.
        """

        ...

    def follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        """Follow bounded committed event pages and active Run ownership.

        Args:
            key: Exact generation to follow.
            after_seq: Last sequence already consumed by the caller.

        Returns:
            Iterator preserving sequence order and backpressure. The first update
            supplies current ownership. A page may split one append or combine
            multiple committed appends; an empty page reports only ownership.

        Raises:
            TraceThreadNotFound: The generation is deleted or replaced.
            TraceStoreError: The follower cannot continue.
        """

        ...

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete an inactive exact generation and invalidate old handles.

        Args:
            key: Exact generation selected for deletion.

        Raises:
            TraceRunConflict: One or more active writers still own it.
            TraceThreadNotFound: The generation is unavailable.
            TraceStoreError: Durable deletion fails.
        """

        ...


@runtime_checkable
class TraceGraphStore(Protocol):
    """Optional Store capability for direct indexed Graph queries."""

    @property
    def supports_graph_queries(self) -> bool:
        """Whether this Store provides indexed Graph queries for its actual backend."""

        ...

    async def query_trace_graph(
        self,
        key: TraceThreadKey,
        *,
        run_ids: tuple[str, ...],
        where: TraceGraphFilter,
        limit: int,
        max_nodes: int = 4000,
        before_started_at: datetime | None = None,
        before_node_id: str | None = None,
        started_run_ids: tuple[str, ...] | None = None,
    ) -> TraceGraphNodeRecordPage:
        """Return direct matches plus their bounded parent Subagent scopes.

        ``run_ids`` supplies the complete selected lineage. ``started_run_ids``
        optionally limits direct matches by their authoritative start's Run,
        after lineage facts are merged and before applying the page limit. Its
        values must be a unique subset of ``run_ids``; None adds no filter and
        an empty tuple selects no matches. Parent scopes retain full evidence.
        """

        ...


@runtime_checkable
class TraceGraphRebuildStore(Protocol):
    """Reconstruct the disposable Graph index from authoritative Ledger facts."""

    @property
    def supports_graph_rebuild(self) -> bool:
        """Whether this Store can rebuild its backend's derived Graph index."""

        ...

    async def rebuild_trace_graph(self, key: TraceThreadKey) -> int:
        """Replace derived Graph nodes for one exact generation."""

        ...


def _event(
    *,
    event_id: str,
    trace_seq: int,
    generation: str,
    fact: TraceSemanticFact,
    copy_fact: bool = True,
) -> TraceEvent:
    persisted_bytes = 1
    stored_fact = fact.model_copy(deep=True) if copy_fact else fact
    for _attempt in range(4):
        event = TraceEvent(
            event_id=event_id,
            trace_seq=trace_seq,
            generation=generation,
            fact=stored_fact,
            persisted_bytes=persisted_bytes,
        )
        encoded = event.model_dump_json(by_alias=True).encode()
        if len(encoded) == persisted_bytes:
            return event
        persisted_bytes = len(encoded)
    raise TraceStoreProtocolError("Trace event size did not converge")


def _checkpoint_lookup(
    key: TraceThreadKey,
    *,
    projection_name: str,
    run_id: str | None,
    as_of_seq: int,
) -> None:
    """Validate a Projection checkpoint lookup before Store state is touched."""

    if not isinstance(key, TraceThreadKey):
        raise TypeError("key must be a TraceThreadKey")
    for name, value in (("projection_name", projection_name), ("run_id", run_id)):
        if value is not None and (
            not isinstance(value, str) or not value or value != value.strip()
        ):
            raise ValueError(f"{name} must be canonical text")
    if as_of_seq < 0:
        raise ValueError("as_of_seq must be non-negative")


__all__ = [
    "StoreThreadSnapshot",
    "StoreWriterSnapshot",
    "TraceGraphNodeRecord",
    "TraceGraphNodeRecordPage",
    "TraceGraphRebuildStore",
    "TraceGraphStore",
    "TraceProjectionCheckpoint",
    "TraceStore",
    "TraceStoreUpdate",
    "TraceThreadKey",
    "TraceWriter",
    "_checkpoint_lookup",
    "_event",
]
