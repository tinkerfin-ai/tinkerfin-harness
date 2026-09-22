"""Storage-neutral contracts for durable Trace Ledger backends."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from tinkerfin_contracts import RunIdentity

from .facts import TraceEvent, TraceSemanticFact
from .graph import (
    MAX_TRACE_GRAPH_LINEAGE_RUNS,
    TraceGraphFilter,
    TraceGraphLinkIssue,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from .limits import TraceLimits
from .store import StoreWriterSnapshot, TraceProjectionCheckpoint, TraceThreadKey

TraceLedgerChangeKind = Literal[
    "open_writer",
    "append_events",
    "renew_writer",
    "close_writer",
    "save_projection_checkpoint",
    "delete_generation",
]
TraceEventPageDirection = Literal["forward", "reverse"]


@dataclass(frozen=True, slots=True)
class TraceStoreOptions:
    """Configure shared-writer leases, following, and bounded commit retries.

    Attributes:
        writer_lease_seconds: Storage-clock ownership duration after a successful
            writer open, heartbeat, or append.
        writer_heartbeat_interval_seconds: Delay between owned writer renewals.
        follow_poll_seconds: Maximum delay between cross-instance follow reads.
        commit_retry_attempts: Maximum explicit retryable commit attempts, including
            the first attempt.
        commit_retry_delay_seconds: Base delay for linear commit retry backoff.
    """

    writer_lease_seconds: float = 30.0
    writer_heartbeat_interval_seconds: float = 10.0
    follow_poll_seconds: float = 0.5
    commit_retry_attempts: int = 5
    commit_retry_delay_seconds: float = 0.02

    def __post_init__(self) -> None:
        """Validate finite timing values and lease/retry ordering."""

        for name in (
            "writer_lease_seconds",
            "writer_heartbeat_interval_seconds",
            "follow_poll_seconds",
            "commit_retry_delay_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.writer_heartbeat_interval_seconds >= self.writer_lease_seconds:
            raise ValueError("writer heartbeat interval must be shorter than lease")
        if isinstance(self.commit_retry_attempts, bool) or not isinstance(
            self.commit_retry_attempts, int
        ):
            raise TypeError("commit_retry_attempts must be an integer")
        if self.commit_retry_attempts < 1:
            raise ValueError("commit_retry_attempts must be a positive integer")


@dataclass(frozen=True, slots=True)
class TraceStoredFact:
    """Carry one validated fact and its canonical pre-storage evidence."""

    event_id: str
    fact: TraceSemanticFact
    canonical_payload: bytes
    payload_digest: str
    exact_byte_count: int


@dataclass(frozen=True, slots=True)
class StoredTraceEvent:
    """Represent one backend event record without exposing a storage schema."""

    event_id: str
    trace_seq: int
    run_id: str
    fact_kind: str
    occurred_at: datetime
    canonical_payload: bytes
    payload_digest: str
    persisted_bytes: int


@dataclass(frozen=True, slots=True)
class TraceGraphNodeMutation:
    """Apply one deterministic partial update to the disposable Graph index.

    Attributes:
        model_call_seq: Ledger sequence proving ``model_call_id``, retained across
            independent lifecycle, input, and output updates.
    """

    node_id: str
    updated_seq: int
    run_id: str
    remove: bool = False
    kind: TraceGraphNodeKind | None = None
    status: TraceGraphNodeStatus | None = None
    name: str | None = None
    parent_subagent_id: str | None = None
    model_call_id: str | None = None
    model_call_seq: int | None = None
    graph_namespace: tuple[str, ...] | None = None
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    first_output_at: datetime | None = None
    completed_at: datetime | None = None
    started_seq: int | None = None
    request_seq: int | None = None
    result_seq: int | None = None
    failure_seq: int | None = None
    link_issue: TraceGraphLinkIssue | None = None

    def __post_init__(self) -> None:
        """Require a retained Ledger proof whenever a Model relationship is supplied."""

        if (self.model_call_id is None) != (self.model_call_seq is None):
            raise ValueError(
                "model_call_id and model_call_seq must be supplied together"
            )
        if self.model_call_seq is not None and (
            isinstance(self.model_call_seq, bool)
            or not isinstance(self.model_call_seq, int)
            or not 1 <= self.model_call_seq <= self.updated_seq
        ):
            raise ValueError(
                "model_call_seq must identify a fact at or before updated_seq"
            )


@dataclass(frozen=True, slots=True)
class StoredTraceGraphNode:
    """Return one indexed Graph row with referenced Ledger evidence.

    Attributes:
        model_call_seq: Ledger sequence proving the emitting Model relationship.
        model_call_event: The corresponding Model, Tool, or Subagent fact, independent
            of the current lifecycle and payload locators.
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
    started_event: StoredTraceEvent
    updated_event: StoredTraceEvent
    request_event: StoredTraceEvent | None
    result_event: StoredTraceEvent | None
    failure_event: StoredTraceEvent | None
    model_call_event: StoredTraceEvent | None


@dataclass(frozen=True, slots=True)
class TraceGraphQueryRequest:
    """Select one stable page from the backend-maintained Graph index.

    ``run_ids`` retains the complete selected lineage for resolving node facts.
    ``started_run_ids`` optionally restricts direct matches to nodes whose real
    start belongs to those Runs; it is a subset of ``run_ids``. Apply it after
    merging logical nodes and before paging. Parent scopes still use the complete
    lineage. None leaves starts unrestricted; an empty tuple selects no matches.
    ``node_ids`` restricts indexed matches when resolving explicit context relationships.
    """

    key: TraceThreadKey
    run_ids: tuple[str, ...]
    where: TraceGraphFilter
    limit: int
    total_limit: int = 4000
    before_started_at: datetime | None = None
    before_node_id: str | None = None
    node_ids: tuple[str, ...] | None = None
    started_run_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Reject an incomplete or timezone-dependent backend cursor."""

        if (
            not self.run_ids
            or len(self.run_ids) > MAX_TRACE_GRAPH_LINEAGE_RUNS
            or len(set(self.run_ids)) != len(self.run_ids)
            or any(
                not run_id or len(run_id) > 1024 or run_id != run_id.strip()
                for run_id in self.run_ids
            )
        ):
            raise ValueError("Graph run lineage is invalid or exceeds 10000 Runs")
        if self.started_run_ids is not None and (
            not isinstance(self.started_run_ids, tuple)
            or len(set(self.started_run_ids)) != len(self.started_run_ids)
            or not set(self.started_run_ids) <= set(self.run_ids)
        ):
            raise ValueError("Graph start Runs must be a unique subset of its lineage")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("Graph page limit must be an integer")
        if isinstance(self.total_limit, bool) or not isinstance(self.total_limit, int):
            raise TypeError("Graph total limit must be an integer")
        if self.limit < 1:
            raise ValueError("Graph page limit must be positive")
        if self.total_limit < self.limit:
            raise ValueError("Graph total limit must include every direct match")
        if self.where.search is not None:
            raise ValueError("Graph content search must be resolved by the Trace Store")
        if (self.before_started_at is None) != (self.before_node_id is None):
            raise ValueError("Graph cursor time and node ID must be supplied together")
        if self.before_started_at is not None and (
            self.before_started_at.tzinfo is None
            or self.before_started_at.utcoffset()
            != UTC.utcoffset(self.before_started_at)
        ):
            raise ValueError("Graph cursor time must be aware UTC")


@dataclass(frozen=True, slots=True)
class TraceGraphRebuildRequest:
    """Replace one generation's disposable Graph index at an exact Ledger tail."""

    key: TraceThreadKey
    as_of_seq: int
    mutations: tuple[TraceGraphNodeMutation, ...]


@dataclass(frozen=True, slots=True)
class StoredTraceGraphPage:
    """Return indexed Graph rows, direct matches, and page cursor evidence."""

    key: TraceThreadKey
    as_of_seq: int
    nodes: tuple[StoredTraceGraphNode, ...]
    matched_node_ids: tuple[str, ...]
    has_more: bool
    next_started_at: datetime | None
    next_node_id: str | None
    relationship_evidence_missing: bool


@dataclass(frozen=True, slots=True)
class StoredTraceCheckpoint:
    """Represent one canonical Projection checkpoint storage record."""

    key: TraceThreadKey
    projection_name: str
    run_id: str | None
    as_of_seq: int
    canonical_state: bytes
    state_digest: str


@dataclass(frozen=True, slots=True)
class TraceLedgerWriterState:
    """Describe durable ownership and terminal capacity for one Run writer."""

    run_id: str
    owner_token: str
    fence: int
    lease_expires_at: datetime
    active: bool
    terminal_committed: bool
    closed_committed: bool
    committed_events: int
    remaining_event_reserve: int
    remaining_byte_reserve: int


@dataclass(frozen=True, slots=True)
class TraceLedgerThreadState:
    """Describe one exact generation head without loading its event prefix."""

    key: TraceThreadKey
    next_seq: int
    persisted_bytes: int


@dataclass(frozen=True, slots=True)
class TraceLedgerState:
    """Return the bounded state required for one Store read or atomic change.

    The namespace aggregates include every current thread. Reserve aggregates include
    active writer rows even when their lease has expired, because incomplete writers
    retain terminal capacity until takeover, close, or generation deletion.
    """

    observed_at: datetime
    namespace_thread_count: int
    namespace_persisted_bytes: int
    namespace_reserved_bytes: int
    thread: TraceLedgerThreadState | None
    target_writer: TraceLedgerWriterState | None
    writer_count: int
    thread_reserved_events: int
    thread_reserved_bytes: int
    active_writers: tuple[StoreWriterSnapshot, ...]
    current_checkpoint: StoredTraceCheckpoint | None = None


@dataclass(frozen=True, slots=True)
class TraceLedgerStateRequest:
    """Select current or exact-generation metadata for a bounded Store operation."""

    namespace: str
    thread_id: str
    generation: str | None = None
    run_id: str | None = None
    include_active_writers: bool = False


@dataclass(frozen=True, slots=True)
class TraceEventPageRequest:
    """Select one bounded event page from an exact immutable prefix."""

    key: TraceThreadKey
    direction: TraceEventPageDirection
    limit: int
    after_seq: int | None = None
    as_of_seq: int | None = None
    before_seq: int | None = None


@dataclass(frozen=True, slots=True)
class StoredTraceEventPage:
    """Return raw records, generation tail, and storage-clock active Run identities.

    Ownership is observed with the tail, independently of event content. Expired
    leases are excluded even when no writer has committed another event.
    """

    key: TraceThreadKey
    tail_seq: int
    events: tuple[StoredTraceEvent, ...]
    active_run_ids: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TraceCheckpointRequest:
    """Select the newest Projection checkpoint inside one fixed prefix."""

    key: TraceThreadKey
    projection_name: str
    run_id: str | None
    as_of_seq: int


@dataclass(frozen=True, slots=True)
class TraceLedgerChange:
    """Request one framework-owned atomic Trace Ledger state transition."""

    kind: TraceLedgerChangeKind
    namespace: str
    limits: TraceLimits
    options: TraceStoreOptions
    identity: RunIdentity | None = None
    key: TraceThreadKey | None = None
    run_id: str | None = None
    owner_token: str | None = None
    fence: int | None = None
    facts: tuple[TraceStoredFact, ...] = ()
    mandatory: bool = False
    checkpoint: TraceProjectionCheckpoint | None = None
    canonical_checkpoint_state: bytes | None = None
    checkpoint_state_digest: str | None = None
    expected_checkpoint_as_of_seq: int | None = None
    proven_events: tuple[TraceEvent, ...] = ()
    proven_renewal: bool = False
    enforce_writer_lease: bool = True
    persist_canonical_event_records: bool = True


@dataclass(frozen=True, slots=True)
class TraceLedgerCommitResult:
    """Return the observable value produced by one committed Ledger change."""

    kind: TraceLedgerChangeKind
    key: TraceThreadKey | None = None
    run_id: str | None = None
    owner_token: str | None = None
    fence: int | None = None
    events: tuple[TraceEvent, ...] = ()
    checkpoint: TraceProjectionCheckpoint | None = None


@dataclass(frozen=True, slots=True)
class TraceLedgerStorageEffect:
    """Describe complete physical effects after framework semantic validation."""

    result: TraceLedgerCommitResult
    thread: TraceLedgerThreadState | None = None
    remove_thread: bool = False
    writer: TraceLedgerWriterState | None = None
    remove_writer_run_id: str | None = None
    events: tuple[StoredTraceEvent, ...] = ()
    validated_events: tuple[TraceEvent, ...] = ()
    graph_node_mutations: tuple[TraceGraphNodeMutation, ...] = ()
    checkpoint: StoredTraceCheckpoint | None = None
    delete_generation: bool = False
    namespace_thread_delta: int = 0
    namespace_persisted_bytes_delta: int = 0
    namespace_reserved_bytes_delta: int = 0


@runtime_checkable
class TraceLedgerBackend(Protocol):
    """Persist one shared Trace Ledger through five storage-oriented operations.

    Implementations borrow their database client from the host. They must preserve
    cancellation and atomically apply every resolved storage effect. Success
    requires proof of commit; an uncertain outcome that cannot be confirmed raises
    a stable Trace Store error without replaying unproven writes.
    """

    async def prepare_storage(self) -> None:
        """Idempotently create and validate backend-owned storage structures.

        The operation may run concurrently through independent Backend instances. It
        must not close, resize, or otherwise take ownership of the host client.

        Raises:
            TraceStoreError: Storage cannot be prepared or proven ready.
            TraceStoreProtocolError: Existing owned structures are not the current
                required shape.
        """

        ...

    async def commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        """Resolve and atomically commit one framework-owned Ledger change.

        The Backend obtains current state and storage time inside its transaction or
        conditional-write loop, calls ``resolve_ledger_change()``, and applies the
        returned effect as one unit. Optimistic conflicts may repeat resolution with
        fresh state. An uncertain commit must be confirmed before returning success;
        otherwise the Backend raises a stable Trace Store error.

        Args:
            change: Complete framework-created semantic change request.

        Returns:
            Exact committed result, including writer, event, or checkpoint evidence.

        Raises:
            TraceStoreError: The change cannot be committed or proven committed.
            TraceStoreProtocolError: Durable evidence conflicts with the change.
        """

        ...

    async def load_ledger_state(
        self,
        request: TraceLedgerStateRequest,
    ) -> TraceLedgerState:
        """Load one consistent namespace, thread, and optional writer state.

        Args:
            request: Namespace, thread, generation, and writer selection.

        Returns:
            Storage-clock state with bounded aggregate and ownership metadata.

        Raises:
            TraceStoreError: Consistent state cannot be read.
            TraceStoreProtocolError: Stored identity evidence conflicts.
        """

        ...

    async def read_event_page(
        self,
        request: TraceEventPageRequest,
    ) -> StoredTraceEventPage:
        """Read one bounded raw event page and its exact observed tail.

        Args:
            request: Exact generation, direction, cursor, prefix, and page limit.

        Returns:
            Raw canonical event records and the tail observed in the same read boundary.

        Raises:
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The bounded consistent read fails.
        """

        ...

    async def load_projection_checkpoint(
        self,
        request: TraceCheckpointRequest,
    ) -> StoredTraceCheckpoint | None:
        """Load the newest raw Projection checkpoint within a fixed prefix.

        Args:
            request: Exact generation, Projection scope, and inclusive prefix.

        Returns:
            Newest canonical checkpoint record at or below the prefix, or ``None``.

        Raises:
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The bounded consistent read fails.
        """

        ...


@runtime_checkable
class TraceGraphQueryBackend(Protocol):
    """Query the disposable Graph index without scanning Ledger payloads."""

    async def query_trace_graph(
        self,
        request: TraceGraphQueryRequest,
    ) -> StoredTraceGraphPage:
        """Return one backend-filtered Graph page and referenced facts."""

        ...


@runtime_checkable
class TraceGraphRebuildBackend(Protocol):
    """Atomically rebuild disposable Graph nodes from Ledger-derived mutations."""

    async def rebuild_trace_graph(self, request: TraceGraphRebuildRequest) -> int:
        """Replace Graph nodes when the Ledger still has the requested tail."""

        ...


def resolve_ledger_change(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    """Return the framework-owned atomic storage effect for current state.

    Backend implementations call this pure function inside a transaction or optimistic
    conditional-write loop. It performs no I/O and may be called again with fresh state
    after a conflict.

    Args:
        change: Framework-created semantic change request.
        state: Consistent current storage state and storage-clock observation.

    Returns:
        Complete physical effect and public commit result.

    Raises:
        TraceStoreError: Current state rejects the requested transition.
    """

    from ._ledger import resolve_ledger_change as _resolve_ledger_change

    return _resolve_ledger_change(change, state)


__all__ = [
    "StoredTraceCheckpoint",
    "StoredTraceEvent",
    "StoredTraceEventPage",
    "StoredTraceGraphNode",
    "StoredTraceGraphPage",
    "TraceCheckpointRequest",
    "TraceEventPageDirection",
    "TraceEventPageRequest",
    "TraceGraphNodeMutation",
    "TraceGraphQueryBackend",
    "TraceGraphQueryRequest",
    "TraceGraphRebuildBackend",
    "TraceGraphRebuildRequest",
    "TraceLedgerBackend",
    "TraceLedgerChange",
    "TraceLedgerChangeKind",
    "TraceLedgerCommitResult",
    "TraceLedgerState",
    "TraceLedgerStateRequest",
    "TraceLedgerStorageEffect",
    "TraceLedgerThreadState",
    "TraceLedgerWriterState",
    "TraceStoreOptions",
    "TraceStoredFact",
    "resolve_ledger_change",
]
