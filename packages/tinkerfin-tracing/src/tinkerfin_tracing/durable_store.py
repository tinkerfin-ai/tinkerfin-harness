"""Framework-owned Trace Store lifecycle over a five-operation Ledger backend."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity, ThreadIdentity

from ._graph_projection import trace_graph_record_search_values
from ._graph_reducer import (
    ReducedTraceGraphNode,
    ReducedTraceGraphRevision,
    apply_graph_events,
    apply_graph_node_mutation,
    effective_graph_nodes,
    graph_revision_mutations,
)
from ._prepared import prepare_trace_facts
from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .backend import (
    StoredTraceCheckpoint,
    StoredTraceEvent,
    StoredTraceEventPage,
    StoredTraceGraphNode,
    StoredTraceGraphPage,
    TraceCheckpointRequest,
    TraceEventPageRequest,
    TraceGraphQueryBackend,
    TraceGraphQueryRequest,
    TraceGraphRebuildBackend,
    TraceGraphRebuildRequest,
    TraceLedgerBackend,
    TraceLedgerChange,
    TraceLedgerCommitResult,
    TraceLedgerState,
    TraceLedgerStateRequest,
    TraceLedgerStorageEffect,
    TraceLedgerThreadState,
    TraceLedgerWriterState,
    TraceStoreOptions,
    resolve_ledger_change,
)
from .codec import CanonicalTracePayloadCodec
from .errors import (
    TraceQuotaExceeded,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from .facts import TraceEvent, TraceSemanticFact
from .graph import (
    MAX_SUBAGENT_SCOPE_DEPTH,
    MAX_TRACE_GRAPH_LINEAGE_RUNS,
    TraceGraphFilter,
    _validate_trace_graph_subagent_scopes,
)
from .limits import TraceLimits
from .store import (
    StoreThreadSnapshot,
    StoreWriterSnapshot,
    TraceGraphNodeRecord,
    TraceGraphNodeRecordPage,
    TraceProjectionCheckpoint,
    TraceStoreUpdate,
    TraceThreadKey,
    TraceWriter,
    _checkpoint_lookup,
    _event,
)

_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


class _DurableTraceWriter:
    """Keep a writer leased until close settles its heartbeat and persistence.

    A failed heartbeat prevents further appends. Background process control is
    retained for the next append or close caller, never raised from an unobserved
    Task. Close still settles the Ledger before delivering that control failure.
    If close also fails, its exception chain retains every prior heartbeat failure.

    Args:
        store: Store whose backend holds the writer lease.
        key: Exact thread generation owned by this writer.
        run_id: Run allowed to append facts with this lease.
        owner_token: Opaque token that distinguishes independent writer instances.
        fence: Monotonic ownership number checked on every mutation.
    """

    def __init__(
        self,
        store: DurableTraceStore,
        *,
        key: TraceThreadKey,
        run_id: str,
        owner_token: str,
        fence: int,
    ) -> None:
        self._store = store
        self._key = key
        self._run_id = run_id
        self._owner_token = owner_token
        self._fence = fence
        self._closed = False
        self._failure: BaseException | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._mutation_lock = asyncio.Lock()
        self._heartbeat: asyncio.Task[None] | None = None
        if self._store._enforce_writer_leases:
            self._heartbeat = asyncio.create_task(
                self._heartbeat_forever(),
                name=f"tinkerfin-trace-ledger-heartbeat:{run_id}",
            )
            self._heartbeat.add_done_callback(_consume_task_exception)

    @property
    def key(self) -> TraceThreadKey:
        """Return the exact generation protected by this writer."""

        return self._key

    @property
    def run_id(self) -> str:
        """Return the semantic Run protected by this writer."""

        return self._run_id

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Canonically prepare and atomically append one ordered fact batch."""

        if not facts:
            raise ValueError("facts must not be empty")
        if self._closed:
            raise TraceStoreProtocolError("Trace writer is closed")
        if self._failure is not None:
            if not isinstance(self._failure, Exception):
                raise self._failure
            raise TraceStoreProtocolError(
                "Trace writer heartbeat failed",
                cause=self._failure,
            )
        prepared = prepare_trace_facts(facts, codec=self._store._codec)
        async with self._mutation_lock:
            result = await self._store._commit_ledger_change(
                TraceLedgerChange(
                    kind="append_events",
                    namespace=self._key.namespace,
                    limits=self._store.limits,
                    options=self._store.options,
                    key=self._key,
                    run_id=self._run_id,
                    owner_token=self._owner_token,
                    fence=self._fence,
                    facts=prepared,
                    mandatory=mandatory,
                    enforce_writer_lease=self._store._enforce_writer_leases,
                    persist_canonical_event_records=(
                        self._store._enforce_writer_leases
                    ),
                )
            )
        return result.events

    async def aclose(self) -> None:
        """Settle heartbeat and owned persistence exactly once under cancellation."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                capture(self._close_once()),
                name=f"tinkerfin-trace-ledger-close:{self._run_id}",
            )
            self._close_task = task
        await join_owned_task(task, cancel_operation=False)

    async def _heartbeat_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(
                    self._store.options.writer_heartbeat_interval_seconds
                )
                async with self._mutation_lock:
                    await self._store._commit_ledger_change(
                        TraceLedgerChange(
                            kind="renew_writer",
                            namespace=self._key.namespace,
                            limits=self._store.limits,
                            options=self._store.options,
                            key=self._key,
                            run_id=self._run_id,
                            owner_token=self._owner_token,
                            fence=self._fence,
                        )
                    )
        except asyncio.CancelledError as error:
            if not self._closed:
                self._failure = error
            raise
        except BaseException as error:  # noqa: BLE001 - the writer's next caller owns background process control
            self._failure = error

    async def _close_once(self) -> None:
        self._closed = True
        heartbeat = self._heartbeat
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            async with self._mutation_lock:
                await self._store._commit_ledger_change(
                    TraceLedgerChange(
                        kind="close_writer",
                        namespace=self._key.namespace,
                        limits=self._store.limits,
                        options=self._store.options,
                        key=self._key,
                        run_id=self._run_id,
                        owner_token=self._owner_token,
                        fence=self._fence,
                        enforce_writer_lease=self._store._enforce_writer_leases,
                    )
                )
        except BaseException as close_error:
            if self._failure is not None:
                raise select_failure(self._failure, close_error)
            raise
        if self._failure is not None and not isinstance(self._failure, Exception):
            raise self._failure


class _FollowChange:
    """Keep notifications only while an exact generation has local followers."""

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.revision = 0
        self.followers = 0


class DurableTraceStore:
    """Provide the complete Trace Store contract over borrowed durable storage.

    The framework owns writer objects, heartbeats, canonical payload validation,
    following, and every semantic Ledger transition. The supplied backend owns only
    storage preparation, atomic effects, and consistent raw reads. The Store never
    closes the backend or its database client.

    Args:
        backend: Borrowed five-operation shared Ledger backend.
        limits: Ledger, event, thread, reserve, and follow capacity limits.
        options: Writer lease, heartbeat, polling, and commit retry settings.
        codec: Canonical fact and Projection-state codec.

    Raises:
        TypeError: An argument has the wrong public type.
        ValueError: The supplied options are invalid.
    """

    def __init__(
        self,
        backend: TraceLedgerBackend,
        *,
        limits: TraceLimits | None = None,
        options: TraceStoreOptions | None = None,
        codec: CanonicalTracePayloadCodec | None = None,
    ) -> None:
        """Initialize a Store over a borrowed durable Backend.

        Args:
            backend: Borrowed shared Ledger Backend.
            limits: Optional capacity limits.
            options: Optional writer and retry settings.
            codec: Optional canonical payload codec.
        """

        if not isinstance(backend, TraceLedgerBackend):
            raise TypeError("backend must implement TraceLedgerBackend")
        if limits is not None and not isinstance(limits, TraceLimits):
            raise TypeError("limits must be a TraceLimits or None")
        if options is not None and not isinstance(options, TraceStoreOptions):
            raise TypeError("options must be a TraceStoreOptions or None")
        if codec is not None and not isinstance(codec, CanonicalTracePayloadCodec):
            raise TypeError("codec must be a CanonicalTracePayloadCodec or None")
        self._backend = backend
        self._limits = limits or TraceLimits()
        self._options = options or TraceStoreOptions()
        self._codec = codec or CanonicalTracePayloadCodec()
        self._setup_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._follow_changes: dict[TraceThreadKey, _FollowChange] = {}
        self._enforce_writer_leases = True

    @property
    def limits(self) -> TraceLimits:
        """Return the immutable capacity limits enforced by the Store."""

        return self._limits

    @property
    def options(self) -> TraceStoreOptions:
        """Return immutable writer, follow, and retry settings."""

        return self._options

    @property
    def backend(self) -> TraceLedgerBackend:
        """Return the borrowed Ledger backend without transferring ownership."""

        return self._backend

    @property
    def supports_graph_queries(self) -> bool:
        """Return whether the borrowed backend provides indexed Graph queries."""

        return isinstance(self._backend, TraceGraphQueryBackend)

    @property
    def supports_graph_rebuild(self) -> bool:
        """Return whether the borrowed backend can replace its derived Graph index."""

        return isinstance(self._backend, TraceGraphRebuildBackend)

    async def setup(self) -> None:
        """Prepare storage once and wait for accepted work before cancellation returns.

        Concurrent callers share one setup attempt. Cancelling a waiter does not
        cancel storage initialization; a later explicit call can retry a failed
        attempt. Backend failure and its original causes remain available even
        when the caller was cancelled. Process control takes precedence.

        Raises:
            BaseException: Storage preparation fails, or the caller is cancelled
                after accepted work has settled.
        """

        # No await occurs while selecting the shared task. Existing waiters keep
        # its original outcome; a later explicit call may retry a failed setup.
        task = self._setup_task
        if task is None or (task.done() and isinstance(task.result(), BaseException)):
            task = asyncio.create_task(
                capture(self._backend.prepare_storage()),
                name="tinkerfin-trace-prepare-storage",
            )
            self._setup_task = task
        await join_owned_task(task, cancel_operation=False)

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Open one exclusive backend-fenced writer for a semantic Run."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        owner_token = uuid4().hex
        result = await self._commit_ledger_change(
            TraceLedgerChange(
                kind="open_writer",
                namespace=identity.namespace,
                limits=self._limits,
                options=self._options,
                identity=identity,
                owner_token=owner_token,
                enforce_writer_lease=self._enforce_writer_leases,
            )
        )
        if result.key is None or result.fence is None:
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid writer result"
            )
        return _DurableTraceWriter(
            self,
            key=result.key,
            run_id=identity.run_id,
            owner_token=owner_token,
            fence=result.fence,
        )

    async def snapshot(self, identity: ThreadIdentity) -> StoreThreadSnapshot:
        """Return one consistent prefix for the current generation."""

        if not isinstance(identity, ThreadIdentity):
            raise TypeError("identity must be a ThreadIdentity")
        await self.setup()
        state = await self._backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                include_active_writers=True,
            )
        )
        return _snapshot_from_state(
            state,
            expected_namespace=identity.namespace,
            expected_thread_id=identity.thread_id,
            expected_key=None,
        )

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return one consistent prefix for an exact generation."""

        self._validate_key(key)
        await self.setup()
        state = await self._backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=key.namespace,
                thread_id=key.thread_id,
                generation=key.generation,
                include_active_writers=True,
            )
        )
        return _snapshot_from_state(
            state,
            expected_namespace=key.namespace,
            expected_thread_id=key.thread_id,
            expected_key=key,
        )

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read and verify one bounded ascending fixed-prefix event page."""

        self._validate_key(key)
        if after_seq < 0 or as_of_seq < 0 or limit < 1:
            raise ValueError("event cursor values must be non-negative and bounded")
        await self.setup()
        request = TraceEventPageRequest(
            key=key,
            direction="forward",
            after_seq=after_seq,
            as_of_seq=as_of_seq,
            limit=limit,
        )
        page = await self._backend.read_event_page(request)
        return self._decode_event_page(page, expected_request=request)

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read and verify one bounded newest-first event page."""

        self._validate_key(key)
        if before_seq < 1 or limit < 1:
            raise ValueError("reverse event cursor values must be positive")
        await self.setup()
        request = TraceEventPageRequest(
            key=key,
            direction="reverse",
            before_seq=before_seq,
            limit=limit,
        )
        page = await self._backend.read_event_page(request)
        events = self._decode_event_page(page, expected_request=request)
        if before_seq > page.tail_seq + 1:
            raise ValueError("before_seq exceeds the current Trace tail")
        return events

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
        """Apply metadata and content predicates before returning one Graph page.

        Keep ``run_ids`` as the complete selected lineage. ``started_run_ids``
        optionally selects starts from a subset of those Runs, after resolving
        node history and before paging; None adds no filter. Parent scopes may
        come from other selected Runs and retain their original relationships.
        """

        self._validate_key(key)
        if not isinstance(where, TraceGraphFilter):
            raise TypeError("where must be a TraceGraphFilter")
        backend = self._backend
        if not isinstance(backend, TraceGraphQueryBackend):
            raise TraceStoreProtocolError(
                "Trace Store backend does not provide indexed Graph queries"
            )
        await self.setup()
        if where.search is not None:
            return await self._query_searched_trace_graph(
                backend,
                key,
                run_ids=run_ids,
                started_run_ids=started_run_ids,
                where=where,
                limit=limit,
                max_nodes=max_nodes,
                before_started_at=before_started_at,
                before_node_id=before_node_id,
            )
        return await self._query_trace_graph_records(
            backend,
            key,
            run_ids=run_ids,
            started_run_ids=started_run_ids,
            where=where,
            limit=limit,
            max_nodes=max_nodes,
            before_started_at=before_started_at,
            before_node_id=before_node_id,
        )

    async def _query_trace_graph_records(
        self,
        backend: TraceGraphQueryBackend,
        key: TraceThreadKey,
        *,
        run_ids: tuple[str, ...],
        where: TraceGraphFilter,
        limit: int,
        max_nodes: int,
        before_started_at: datetime | None = None,
        before_node_id: str | None = None,
        started_run_ids: tuple[str, ...] | None = None,
    ) -> TraceGraphNodeRecordPage:
        if len(run_ids) > MAX_TRACE_GRAPH_LINEAGE_RUNS:
            raise TraceQuotaExceeded(
                "Trace Graph lineage exceeds the supported Run count",
                context={"resource": "graph_lineage_runs"},
            )
        stored = await backend.query_trace_graph(
            TraceGraphQueryRequest(
                key=key,
                run_ids=run_ids,
                started_run_ids=started_run_ids,
                where=where,
                limit=limit,
                total_limit=max_nodes,
                before_started_at=before_started_at,
                before_node_id=before_node_id,
            )
        )
        if stored.key != key:
            raise TraceStoreProtocolError(
                "Trace Graph page belongs to another generation"
            )
        stored_node_ids = tuple(item.node_id for item in stored.nodes)
        matched_ids = set(stored.matched_node_ids)
        if (
            len(set(stored_node_ids)) != len(stored_node_ids)
            or len(set(stored.matched_node_ids)) != len(stored.matched_node_ids)
            or not matched_ids <= set(stored_node_ids)
            or tuple(node_id for node_id in stored_node_ids if node_id in matched_ids)
            != stored.matched_node_ids
        ):
            raise TraceStoreProtocolError(
                "Trace Graph direct matches conflict with returned rows"
            )

        stored_events: dict[int, StoredTraceEvent] = {}
        decoded_events: dict[int, TraceEvent] = {}
        allowed_run_ids = frozenset(run_ids)

        def decode_required(event: StoredTraceEvent, sequence: int) -> TraceEvent:
            existing = stored_events.get(sequence)
            if existing is not None and existing != event:
                raise TraceStoreProtocolError(
                    "Trace Graph locators disagree on one Ledger event"
                )
            stored_events[sequence] = event
            decoded = decoded_events.get(sequence)
            if decoded is None:
                decoded = self._decode_graph_event(
                    event, key=key, expected_seq=sequence
                )
                decoded_events[sequence] = decoded
            if decoded.fact.identity.run_id not in allowed_run_ids:
                raise TraceStoreProtocolError(
                    "Trace Graph locator belongs to another Run lineage"
                )
            return decoded

        def decode_optional(
            event: StoredTraceEvent | None,
            sequence: int | None,
        ) -> TraceEvent | None:
            if event is None or sequence is None:
                if event is not None or sequence is not None:
                    raise TraceStoreProtocolError(
                        "Trace Graph optional fact locator is incomplete"
                    )
                return None
            return decode_required(event, sequence)

        if any(item.run_id not in allowed_run_ids for item in stored.nodes):
            raise TraceStoreProtocolError(
                "Trace Graph node belongs to another Run lineage"
            )
        records = tuple(
            TraceGraphNodeRecord(
                node_id=item.node_id,
                parent_subagent_id=item.parent_subagent_id,
                model_call_id=item.model_call_id,
                model_call_seq=item.model_call_seq,
                kind=item.kind,
                status=item.status,
                name=item.name,
                run_id=item.run_id,
                graph_namespace=item.graph_namespace,
                agent_name=item.agent_name,
                provider=item.provider,
                model=item.model,
                started_at=item.started_at,
                first_output_at=item.first_output_at,
                completed_at=item.completed_at,
                started_seq=item.started_seq,
                updated_seq=item.updated_seq,
                request_seq=item.request_seq,
                result_seq=item.result_seq,
                failure_seq=item.failure_seq,
                link_issue=item.link_issue,
                started_event=decode_required(item.started_event, item.started_seq),
                updated_event=decode_required(item.updated_event, item.updated_seq),
                request_event=decode_optional(
                    item.request_event,
                    item.request_seq,
                ),
                result_event=decode_optional(
                    item.result_event,
                    item.result_seq,
                ),
                failure_event=decode_optional(
                    item.failure_event,
                    item.failure_seq,
                ),
                model_call_event=decode_optional(
                    item.model_call_event,
                    item.model_call_seq,
                ),
            )
            for item in stored.nodes
        )
        if started_run_ids is not None and any(
            record.node_id in matched_ids
            and record.started_event.fact.identity.run_id not in started_run_ids
            for record in records
        ):
            raise TraceStoreProtocolError(
                "Trace Graph direct match started outside the requested Runs"
            )
        try:
            _validate_trace_graph_subagent_scopes(
                {
                    record.node_id: (record.parent_subagent_id, record.kind)
                    for record in records
                }
            )
        except ValueError as error:
            raise TraceStoreProtocolError(str(error)) from error
        return TraceGraphNodeRecordPage(
            key=stored.key,
            as_of_seq=stored.as_of_seq,
            nodes=records,
            matched_node_ids=stored.matched_node_ids,
            has_more=stored.has_more,
            next_started_at=stored.next_started_at,
            next_node_id=stored.next_node_id,
            relationship_evidence_missing=stored.relationship_evidence_missing,
        )

    async def _query_searched_trace_graph(
        self,
        backend: TraceGraphQueryBackend,
        key: TraceThreadKey,
        *,
        run_ids: tuple[str, ...],
        where: TraceGraphFilter,
        limit: int,
        max_nodes: int,
        before_started_at: datetime | None,
        before_node_id: str | None,
        started_run_ids: tuple[str, ...] | None = None,
    ) -> TraceGraphNodeRecordPage:
        search = where.search
        if search is None:  # pragma: no cover - private caller contract
            raise ValueError("searched Graph query requires search text")
        candidate_where = where.model_copy(update={"search": None}, deep=True)
        candidates = await self._query_trace_graph_records(
            backend,
            key,
            run_ids=run_ids,
            started_run_ids=started_run_ids,
            where=candidate_where,
            limit=max_nodes + 1,
            max_nodes=max_nodes + 1,
        )
        if candidates.has_more or len(candidates.matched_node_ids) > max_nodes:
            raise TraceQuotaExceeded(
                "Trace Graph content search exceeds max_total_nodes",
                context={"resource": "graph_search_nodes"},
            )
        records_by_id = {record.node_id: record for record in candidates.nodes}
        ordered_candidates = tuple(
            records_by_id[node_id] for node_id in candidates.matched_node_ids
        )
        direct = [
            record
            for record in ordered_candidates
            if _graph_record_contains(
                record,
                search,
                allowed_run_ids=frozenset(run_ids),
            )
        ]
        if before_started_at is not None:
            if before_node_id is None:  # pragma: no cover - public cursor validation
                raise ValueError("Graph cursor requires a node ID")
            cursor = (before_started_at, _node_order_key(before_node_id))
            direct = [
                record
                for record in direct
                if (record.started_at, _node_order_key(record.node_id)) < cursor
            ]
        has_more = len(direct) > limit
        selected = direct[:limit]
        selected_ids = tuple(record.node_id for record in selected)
        cursor_record = selected[-1] if has_more and selected else None
        if not selected_ids:
            return TraceGraphNodeRecordPage(
                key=key,
                as_of_seq=candidates.as_of_seq,
                nodes=(),
                matched_node_ids=(),
                has_more=False,
                next_started_at=None,
                next_node_id=None,
                relationship_evidence_missing=(
                    candidates.relationship_evidence_missing
                ),
            )
        selected_records = {record.node_id: record for record in selected}
        pending = [record.parent_subagent_id for record in selected]
        while pending:
            parent_id = pending.pop()
            if parent_id is None or parent_id in selected_records:
                continue
            parent = records_by_id.get(parent_id)
            if parent is None:
                continue
            if len(selected_records) >= max_nodes:
                raise TraceQuotaExceeded(
                    "Trace Graph Subagent path exceeds max_total_nodes",
                    context={"resource": "graph_total_nodes"},
                )
            selected_records[parent_id] = parent
            pending.append(parent.parent_subagent_id)
        candidate_order = {
            record.node_id: index for index, record in enumerate(candidates.nodes)
        }
        return TraceGraphNodeRecordPage(
            key=key,
            as_of_seq=candidates.as_of_seq,
            nodes=tuple(
                sorted(
                    selected_records.values(),
                    key=lambda record: candidate_order[record.node_id],
                )
            ),
            matched_node_ids=selected_ids,
            has_more=has_more,
            next_started_at=(
                None if cursor_record is None else cursor_record.started_at
            ),
            next_node_id=None if cursor_record is None else cursor_record.node_id,
            relationship_evidence_missing=(candidates.relationship_evidence_missing),
        )

    async def rebuild_trace_graph(self, key: TraceThreadKey) -> int:
        """Rebuild the disposable Graph index from one fixed Ledger prefix."""

        self._validate_key(key)
        backend = self._backend
        if not isinstance(backend, TraceGraphRebuildBackend):
            raise TraceStoreProtocolError(
                "Trace Store backend does not rebuild indexed Graph nodes"
            )
        snapshot = await self.snapshot_key(key)
        revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
        after_seq = 0
        while after_seq < snapshot.as_of_seq:
            page = await self.read_events(
                key,
                after_seq=after_seq,
                as_of_seq=snapshot.as_of_seq,
                limit=self._limits.follow_batch_size,
            )
            if not page:
                raise TraceStoreProtocolError(
                    "Trace Ledger ended before the Graph rebuild prefix"
                )
            apply_graph_events(revisions, page)
            after_seq = page[-1].trace_seq
        return await backend.rebuild_trace_graph(
            TraceGraphRebuildRequest(
                key=key,
                as_of_seq=snapshot.as_of_seq,
                mutations=graph_revision_mutations(revisions.values()),
            )
        )

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Load and verify the newest checkpoint inside a fixed prefix."""

        self._validate_key(key)
        _checkpoint_lookup(
            key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        await self.setup()
        request = TraceCheckpointRequest(
            key=key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        stored = await self._backend.load_projection_checkpoint(request)
        if stored is None:
            return None
        return self._decode_checkpoint(stored, expected_request=request)

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        """Atomically advance one canonical Projection checkpoint."""

        if not isinstance(checkpoint, TraceProjectionCheckpoint):
            raise TypeError("checkpoint must be a TraceProjectionCheckpoint")
        self._validate_key(checkpoint.key)
        if expected_as_of_seq is not None and expected_as_of_seq < 0:
            raise ValueError("expected_as_of_seq must be non-negative or None")
        stored_checkpoint = checkpoint.model_copy(deep=True)
        encoded = self._codec.encode_json(stored_checkpoint.state)
        result = await self._commit_ledger_change(
            TraceLedgerChange(
                kind="save_projection_checkpoint",
                namespace=stored_checkpoint.key.namespace,
                limits=self._limits,
                options=self._options,
                key=stored_checkpoint.key,
                checkpoint=stored_checkpoint,
                canonical_checkpoint_state=encoded.data,
                checkpoint_state_digest=encoded.digest,
                expected_checkpoint_as_of_seq=expected_as_of_seq,
            )
        )
        if result.checkpoint is None:
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid checkpoint result"
            )
        return result.checkpoint.model_copy(deep=True)

    def follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[TraceStoreUpdate, None]:
        """Follow bounded pages with exact-generation wakeups and cross-instance reads.

        An empty read checks the tail and active Runs without loading quota aggregates.
        Local revisions close the read-to-wait gap; notification state lives only
        as long as a generation has followers. Database I/O runs outside the condition.
        """

        self._validate_key(key)
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")

        async def iterate() -> AsyncGenerator[TraceStoreUpdate, None]:
            await self.setup()
            signal = self._follow_changes.get(key)
            if signal is None:
                signal = _FollowChange()
                self._follow_changes[key] = signal
            signal.followers += 1
            cursor = after_seq
            active_run_ids: tuple[str, ...] | None = None
            try:
                while True:
                    revision = signal.revision
                    request = TraceEventPageRequest(
                        key=key,
                        direction="forward",
                        after_seq=cursor,
                        limit=self._limits.follow_batch_size,
                    )
                    page = await self._backend.read_event_page(request)
                    batch = self._decode_event_page(page, expected_request=request)
                    if batch:
                        cursor = batch[-1].trace_seq
                    if batch or active_run_ids != page.active_run_ids:
                        active_run_ids = page.active_run_ids
                        yield TraceStoreUpdate(
                            as_of_seq=cursor,
                            events=batch,
                            active_run_ids=active_run_ids,
                            observed_at=page.observed_at,
                        )
                    if batch:
                        continue
                    async with signal.condition:
                        if signal.revision != revision:
                            continue
                        try:
                            await asyncio.wait_for(
                                signal.condition.wait_for(
                                    lambda: signal.revision != revision
                                ),
                                timeout=self._options.follow_poll_seconds,
                            )
                        except TimeoutError:
                            pass
            finally:
                signal.followers -= 1
                if not signal.followers:
                    self._follow_changes.pop(key, None)

        return iterate()

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete one exact inactive generation through the atomic change boundary."""

        self._validate_key(key)
        await self._commit_ledger_change(
            TraceLedgerChange(
                kind="delete_generation",
                namespace=key.namespace,
                limits=self._limits,
                options=self._options,
                key=key,
            )
        )

    async def _commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        await self.setup()
        result = await self._backend.commit_ledger_change(change)
        if (
            not isinstance(result, TraceLedgerCommitResult)
            or result.kind != change.kind
        ):
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid commit result"
            )
        _validate_commit_result(change, result)
        signal = (
            None
            if result.key is None
            or change.kind in {"renew_writer", "save_projection_checkpoint"}
            else self._follow_changes.get(result.key)
        )
        if signal is not None:
            async with signal.condition:
                signal.revision += 1
                signal.condition.notify_all()
        return result

    def _decode_event_page(
        self,
        page: StoredTraceEventPage,
        *,
        expected_request: TraceEventPageRequest,
    ) -> tuple[TraceEvent, ...]:
        if (
            not isinstance(page.observed_at, datetime)
            or page.observed_at.tzinfo is None
            or page.observed_at.utcoffset() != UTC.utcoffset(page.observed_at)
        ):
            raise TraceStoreProtocolError(
                "Trace event page has invalid observation time"
            )
        if page.key != expected_request.key:
            raise TraceStoreProtocolError(
                "Trace event page does not match the requested generation"
            )
        if (
            not isinstance(page.active_run_ids, tuple)
            or any(
                not isinstance(run_id, str) or not run_id or run_id != run_id.strip()
                for run_id in page.active_run_ids
            )
            or len(set(page.active_run_ids)) != len(page.active_run_ids)
        ):
            raise TraceStoreProtocolError("Trace event page has invalid active Runs")
        records = page.events
        if len(records) > expected_request.limit:
            raise TraceStoreProtocolError(
                "Trace event page exceeds its requested limit"
            )
        sequences = tuple(record.trace_seq for record in records)
        if expected_request.direction == "forward":
            after = (
                0 if expected_request.after_seq is None else expected_request.after_seq
            )
            as_of = (
                page.tail_seq
                if expected_request.as_of_seq is None
                else expected_request.as_of_seq
            )
            invalid = any(
                sequence <= after or sequence > as_of or sequence > page.tail_seq
                for sequence in sequences
            ) or any(right != left + 1 for left, right in zip(sequences, sequences[1:]))
            expected_last = min(as_of, page.tail_seq)
            if after < expected_last and (not sequences or sequences[0] != after + 1):
                invalid = True
        else:
            before = (
                page.tail_seq + 1
                if expected_request.before_seq is None
                else expected_request.before_seq
            )
            invalid = any(
                sequence < 1 or sequence >= before or sequence > page.tail_seq
                for sequence in sequences
            ) or any(right != left - 1 for left, right in zip(sequences, sequences[1:]))
            expected_first = min(before - 1, page.tail_seq)
            if (
                before <= page.tail_seq + 1
                and expected_first >= 1
                and (not sequences or sequences[0] != expected_first)
            ):
                invalid = True
        if invalid:
            raise TraceStoreProtocolError(
                "Trace event page violates requested ordering"
            )
        return tuple(
            self._decode_event(expected_request.key, record) for record in records
        )

    def _decode_event(
        self,
        key: TraceThreadKey,
        record: StoredTraceEvent,
    ) -> TraceEvent:
        if isinstance(self._backend, _InMemoryTraceLedgerBackend) and isinstance(
            record, _MemoryStoredTraceEvent
        ):
            if self._codec.digest(record.canonical_payload) != record.payload_digest:
                raise TraceStoreProtocolError("Trace event payload digest mismatch")
            cached = record.validated_event
            if (
                cached.event_id != record.event_id
                or cached.trace_seq != record.trace_seq
                or cached.generation != key.generation
                or cached.persisted_bytes != record.persisted_bytes
                or cached.fact.identity.thread_id != key.thread_id
                or cached.fact.identity.namespace != key.namespace
                or cached.fact.identity.run_id != record.run_id
                or cached.fact.kind != record.fact_kind
                or cached.fact.occurred_at != record.occurred_at
            ):
                raise TraceStoreProtocolError(
                    "In-memory Trace event evidence conflicts"
                )
            return cached.model_copy(deep=True)
        if self._codec.digest(record.canonical_payload) != record.payload_digest:
            raise TraceStoreProtocolError("Trace event payload digest mismatch")
        fact = self._codec.decode_fact(record.canonical_payload)
        if (
            fact.identity.thread_id != key.thread_id
            or fact.identity.namespace != key.namespace
            or fact.identity.run_id != record.run_id
            or fact.kind != record.fact_kind
            or fact.occurred_at != record.occurred_at
        ):
            raise TraceStoreProtocolError(
                "Trace event searchable metadata conflicts with its payload"
            )
        event = _event(
            event_id=record.event_id,
            trace_seq=record.trace_seq,
            generation=key.generation,
            fact=fact,
        )
        if event.persisted_bytes != record.persisted_bytes:
            raise TraceStoreProtocolError("Trace event size metadata conflicts")
        return event

    def _decode_graph_event(
        self,
        record: StoredTraceEvent,
        *,
        key: TraceThreadKey,
        expected_seq: int,
    ) -> TraceEvent:
        if record.trace_seq != expected_seq:
            raise TraceStoreProtocolError(
                "Trace Graph node references a different Ledger sequence"
            )
        return self._decode_event(key, record)

    def _decode_checkpoint(
        self,
        stored: StoredTraceCheckpoint,
        *,
        expected_request: TraceCheckpointRequest,
    ) -> TraceProjectionCheckpoint:
        if (
            stored.key != expected_request.key
            or stored.projection_name != expected_request.projection_name
            or stored.run_id != expected_request.run_id
            or stored.as_of_seq > expected_request.as_of_seq
        ):
            raise TraceStoreProtocolError(
                "Projection checkpoint does not match the requested scope"
            )
        if self._codec.digest(stored.canonical_state) != stored.state_digest:
            raise TraceStoreProtocolError("Projection checkpoint digest mismatch")
        return TraceProjectionCheckpoint(
            key=stored.key,
            projection_name=stored.projection_name,
            run_id=stored.run_id,
            as_of_seq=stored.as_of_seq,
            state=self._codec.decode_json(stored.canonical_state),
        )

    def _validate_key(self, key: TraceThreadKey) -> None:
        if not isinstance(key, TraceThreadKey):
            raise TypeError("key must be a TraceThreadKey")


@dataclass(slots=True)
class _MemoryThread:
    state: TraceLedgerThreadState
    writers: dict[str, TraceLedgerWriterState] = field(
        default_factory=lambda: dict[str, TraceLedgerWriterState]()
    )
    events: list[_MemoryStoredTraceEvent] = field(
        default_factory=lambda: list[_MemoryStoredTraceEvent]()
    )
    graph_nodes: dict[tuple[str, str], ReducedTraceGraphRevision] = field(
        default_factory=lambda: dict[tuple[str, str], ReducedTraceGraphRevision]()
    )
    checkpoints: dict[
        tuple[str, str | None],
        list[StoredTraceCheckpoint],
    ] = field(
        default_factory=lambda: dict[
            tuple[str, str | None],
            list[StoredTraceCheckpoint],
        ]()
    )


@dataclass(frozen=True, slots=True)
class _MemoryStoredTraceEvent(StoredTraceEvent):
    """Retain framework-validated event evidence for process-local reads."""

    validated_event: TraceEvent


class _InMemoryTraceLedgerBackend:
    """Apply the public backend contract to bounded process-local raw records."""

    def __init__(self) -> None:
        self._threads: dict[tuple[str, str], _MemoryThread] = {}
        self._condition = asyncio.Condition()

    def _shared_peer(self) -> _InMemoryTraceLedgerBackend:
        """Create an independent Backend value over the same process-local storage."""

        peer = _InMemoryTraceLedgerBackend()
        peer._threads = self._threads
        peer._condition = self._condition
        return peer

    async def prepare_storage(self) -> None:
        """Require no preparation for process-local memory."""

    async def commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        """Resolve and apply one complete effect under the backend condition."""

        async with self._condition:
            state = self._state_for_change(change)
            effect = resolve_ledger_change(change, state)
            self._apply_effect(change, effect)
            self._condition.notify_all()
            return effect.result

    async def load_ledger_state(
        self,
        request: TraceLedgerStateRequest,
    ) -> TraceLedgerState:
        """Return a defensive consistent process-local state snapshot."""

        async with self._condition:
            return self._state(
                namespace=request.namespace,
                thread_id=request.thread_id,
                run_id=request.run_id,
                checkpoint_request=None,
                include_active_writers=request.include_active_writers,
            )

    async def read_event_page(
        self,
        request: TraceEventPageRequest,
    ) -> StoredTraceEventPage:
        """Return one bounded raw page from an exact generation."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = thread.state.next_seq - 1
            now = datetime.now(UTC)
            if request.direction == "forward":
                after = 0 if request.after_seq is None else request.after_seq
                as_of = tail if request.as_of_seq is None else request.as_of_seq
                start = min(after, tail)
                stop = min(as_of, tail, start + request.limit)
                records = tuple(thread.events[start:stop])
            else:
                before = tail + 1 if request.before_seq is None else request.before_seq
                if before > tail + 1:
                    records = ()
                else:
                    stop = before - 1
                    start = max(0, stop - request.limit)
                    records = tuple(reversed(thread.events[start:stop]))
            return StoredTraceEventPage(
                key=request.key,
                tail_seq=tail,
                events=records,
                active_run_ids=tuple(
                    sorted(
                        writer.run_id
                        for writer in thread.writers.values()
                        if writer.active and writer.lease_expires_at > now
                    )
                ),
                observed_at=now,
            )

    async def query_trace_graph(
        self,
        request: TraceGraphQueryRequest,
    ) -> StoredTraceGraphPage:
        """Filter process-local Graph metadata without scanning fact payloads."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = thread.state.next_seq - 1
            run_ids = frozenset(request.run_ids)
            revisions = [
                row for row in thread.graph_nodes.values() if row.run_id in run_ids
            ]
            candidates = effective_graph_nodes(revisions, run_ids=run_ids)
            matching = [
                row for row in candidates if _graph_node_matches(row, request.where)
            ]
            if request.started_run_ids is not None:
                started_runs = frozenset(request.started_run_ids)
                try:
                    matching = [
                        row
                        for row in matching
                        if thread.events[row.started_seq - 1].run_id in started_runs
                    ]
                except IndexError as error:
                    raise TraceStoreProtocolError(
                        "Trace Graph start locator is outside the Ledger"
                    ) from error
            matching.sort(
                key=lambda row: (row.started_at, _node_order_key(row.node_id)),
                reverse=True,
            )
            if request.before_started_at is not None:
                if request.before_node_id is None:
                    raise ValueError("Graph cursor requires a node ID")
                cursor = (
                    request.before_started_at,
                    _node_order_key(request.before_node_id),
                )
                matching = [
                    row
                    for row in matching
                    if (row.started_at, _node_order_key(row.node_id)) < cursor
                ]
            has_more = len(matching) > request.limit
            selected = matching[: request.limit]
            matched_node_ids = tuple(row.node_id for row in selected)
            cursor_row = selected[-1] if has_more and selected else None
            result_rows: dict[str, ReducedTraceGraphNode] = {
                row.node_id: row for row in selected
            }
            candidates_by_id = {row.node_id: row for row in candidates}
            pending = {
                row.parent_subagent_id
                for row in selected
                if row.parent_subagent_id is not None
                and row.parent_subagent_id not in result_rows
            }
            for depth in range(1, MAX_SUBAGENT_SCOPE_DEPTH + 2):
                if not pending:
                    break
                parents = tuple(
                    candidates_by_id[node_id]
                    for node_id in sorted(pending)
                    if node_id in candidates_by_id
                    and candidates_by_id[node_id].run_id in run_ids
                )
                if depth > MAX_SUBAGENT_SCOPE_DEPTH and parents:
                    raise TraceStoreProtocolError(
                        "Trace Graph Subagent scope exceeds 64 levels"
                    )
                next_parent_ids: set[str] = set()
                for parent in parents:
                    if parent.node_id in result_rows:
                        continue
                    if len(result_rows) >= request.total_limit:
                        raise TraceQuotaExceeded(
                            "Trace Graph Subagent path exceeds max_total_nodes",
                            context={"resource": "graph_total_nodes"},
                        )
                    result_rows[parent.node_id] = parent
                    if (
                        parent.parent_subagent_id is not None
                        and parent.parent_subagent_id not in result_rows
                    ):
                        next_parent_ids.add(parent.parent_subagent_id)
                pending = next_parent_ids
            return StoredTraceGraphPage(
                key=request.key,
                as_of_seq=tail,
                nodes=tuple(
                    _stored_memory_graph_node(thread, row)
                    for row in sorted(
                        result_rows.values(),
                        key=lambda item: (
                            item.started_at,
                            _node_order_key(item.node_id),
                        ),
                        reverse=True,
                    )
                ),
                matched_node_ids=matched_node_ids,
                has_more=has_more,
                next_started_at=(None if cursor_row is None else cursor_row.started_at),
                next_node_id=None if cursor_row is None else cursor_row.node_id,
                relationship_evidence_missing=any(
                    row.link_issue is not None for row in candidates
                ),
            )

    async def rebuild_trace_graph(self, request: TraceGraphRebuildRequest) -> int:
        """Atomically replace process-local Graph nodes at one exact tail."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            if thread.state.next_seq - 1 != request.as_of_seq:
                raise TraceStoreProtocolError(
                    "Trace Ledger changed while rebuilding the Graph"
                )
            rebuilt: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
            current = thread.graph_nodes
            thread.graph_nodes = rebuilt
            try:
                source_events = {
                    event.trace_seq: event.validated_event for event in thread.events
                }
                for mutation in request.mutations:
                    apply_graph_node_mutation(
                        thread.graph_nodes, mutation, source_events=source_events
                    )
            except BaseException:
                thread.graph_nodes = current
                raise
            self._condition.notify_all()
            return len(
                {
                    revision.node_id
                    for revision in rebuilt.values()
                    if isinstance(revision, ReducedTraceGraphNode)
                }
            )

    async def load_projection_checkpoint(
        self,
        request: TraceCheckpointRequest,
    ) -> StoredTraceCheckpoint | None:
        """Return the newest raw checkpoint inside one fixed prefix."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            return next(
                (
                    item
                    for item in reversed(
                        thread.checkpoints.get(
                            (request.projection_name, request.run_id),
                            (),
                        )
                    )
                    if item.as_of_seq <= request.as_of_seq
                ),
                None,
            )

    def _state_for_change(self, change: TraceLedgerChange) -> TraceLedgerState:
        thread_id: str
        run_id = change.run_id
        if change.identity is not None:
            thread_id = change.identity.thread_id
            run_id = change.identity.run_id
        elif change.key is not None:
            thread_id = change.key.thread_id
        else:
            raise TraceStoreProtocolError("Trace Ledger change has no thread identity")
        checkpoint_request = None
        if change.checkpoint is not None:
            checkpoint_request = TraceCheckpointRequest(
                key=change.checkpoint.key,
                projection_name=change.checkpoint.projection_name,
                run_id=change.checkpoint.run_id,
                as_of_seq=change.checkpoint.as_of_seq,
            )
        return self._state(
            namespace=change.namespace,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_request=checkpoint_request,
            include_active_writers=change.kind == "delete_generation",
        )

    def _state(
        self,
        *,
        namespace: str,
        thread_id: str,
        run_id: str | None,
        checkpoint_request: TraceCheckpointRequest | None,
        include_active_writers: bool,
    ) -> TraceLedgerState:
        now = datetime.now(UTC)
        namespace_threads = tuple(
            thread
            for (stored_namespace, _thread_id), thread in self._threads.items()
            if stored_namespace == namespace
        )
        namespace_reserved = sum(
            writer.remaining_byte_reserve
            for thread in namespace_threads
            for writer in thread.writers.values()
            if writer.active
        )
        thread = self._threads.get((namespace, thread_id))
        writers = () if thread is None else tuple(thread.writers.values())
        target = (
            None if run_id is None or thread is None else thread.writers.get(run_id)
        )
        active = (
            tuple(
                StoreWriterSnapshot(
                    run_id=writer.run_id,
                    committed_events=writer.committed_events,
                )
                for writer in sorted(writers, key=lambda item: item.run_id)
                if writer.active and writer.lease_expires_at > now
            )
            if include_active_writers
            else ()
        )
        checkpoint = None
        if thread is not None and checkpoint_request is not None:
            checkpoint = next(
                reversed(
                    thread.checkpoints.get(
                        (
                            checkpoint_request.projection_name,
                            checkpoint_request.run_id,
                        ),
                        (),
                    )
                ),
                None,
            )
        return TraceLedgerState(
            observed_at=now,
            namespace_thread_count=len(namespace_threads),
            namespace_persisted_bytes=sum(
                item.state.persisted_bytes for item in namespace_threads
            ),
            namespace_reserved_bytes=namespace_reserved,
            thread=None if thread is None else thread.state,
            target_writer=target,
            writer_count=len(writers),
            thread_reserved_events=sum(
                writer.remaining_event_reserve for writer in writers if writer.active
            ),
            thread_reserved_bytes=sum(
                writer.remaining_byte_reserve for writer in writers if writer.active
            ),
            active_writers=active,
            current_checkpoint=checkpoint,
        )

    def _apply_effect(
        self,
        change: TraceLedgerChange,
        effect: TraceLedgerStorageEffect,
    ) -> None:
        key = effect.result.key or change.key
        if effect.delete_generation:
            if key is not None:
                self._threads.pop((key.namespace, key.thread_id), None)
            return
        if effect.remove_thread:
            if key is not None:
                self._threads.pop((key.namespace, key.thread_id), None)
            return
        if effect.thread is not None:
            storage_key = (
                effect.thread.key.namespace,
                effect.thread.key.thread_id,
            )
            thread = self._threads.get(storage_key)
            if thread is None:
                thread = _MemoryThread(state=effect.thread)
                self._threads[storage_key] = thread
            else:
                thread.state = effect.thread
        if key is None:
            return
        thread = self._threads.get((key.namespace, key.thread_id))
        if thread is None:
            return
        if effect.remove_writer_run_id is not None:
            thread.writers.pop(effect.remove_writer_run_id, None)
        if effect.writer is not None:
            thread.writers[effect.writer.run_id] = effect.writer
        if effect.validated_events:
            thread.events.extend(
                _memory_stored_event(
                    event,
                    prepared.canonical_payload,
                    prepared.payload_digest,
                )
                for event, prepared in zip(
                    effect.validated_events,
                    change.facts,
                    strict=True,
                )
            )
        apply_graph_events(thread.graph_nodes, effect.validated_events)
        if effect.checkpoint is not None:
            thread.checkpoints.setdefault(
                (effect.checkpoint.projection_name, effect.checkpoint.run_id),
                [],
            ).append(effect.checkpoint)


class InMemoryTraceStore(DurableTraceStore):
    """Bounded process-local Trace Store using the shared Ledger reducer."""

    def __init__(
        self,
        *,
        limits: TraceLimits | None = None,
    ) -> None:
        """Initialize a bounded process-local Store.

        Args:
            limits: Optional capacity limits.
        """

        super().__init__(
            _InMemoryTraceLedgerBackend(),
            limits=limits,
        )
        self._enforce_writer_leases = False


def _memory_stored_event(
    event: TraceEvent,
    canonical_payload: bytes,
    payload_digest: str,
) -> _MemoryStoredTraceEvent:
    return _MemoryStoredTraceEvent(
        event_id=event.event_id,
        trace_seq=event.trace_seq,
        run_id=event.fact.identity.run_id,
        fact_kind=event.fact.kind,
        occurred_at=event.fact.occurred_at,
        canonical_payload=canonical_payload,
        payload_digest=payload_digest,
        persisted_bytes=event.persisted_bytes,
        validated_event=event,
    )


def _graph_node_matches(
    row: ReducedTraceGraphNode | TraceGraphNodeRecord,
    where: TraceGraphFilter,
) -> bool:
    if where.search is not None:
        raise ValueError(
            "Graph content search must be resolved before metadata filters"
        )
    return not (
        (where.kinds and row.kind not in where.kinds)
        or (where.statuses and row.status not in where.statuses)
        or (
            where.model_call_id is not None and row.model_call_id != where.model_call_id
        )
        or (where.agent_names and row.agent_name not in where.agent_names)
        or (where.providers and row.provider not in where.providers)
        or (where.models and row.model not in where.models)
        or (
            where.graph_namespaces and row.graph_namespace not in where.graph_namespaces
        )
        or (where.started_after is not None and row.started_at <= where.started_after)
        or (where.started_before is not None and row.started_at >= where.started_before)
    )


def _graph_record_contains(
    record: TraceGraphNodeRecord,
    search: str,
    *,
    allowed_run_ids: frozenset[str],
) -> bool:
    """Match visible metadata and decoded public details with one literal rule."""

    case_insensitive = search.isascii()
    needle = search.translate(_ASCII_LOWER_TRANSLATION) if case_insensitive else search
    values = (
        record.name,
        record.agent_name,
        record.provider,
        record.model,
        *(
            text
            for value in trace_graph_record_search_values(
                record,
                allowed_run_ids=allowed_run_ids,
            )
            for text in _json_search_values(value)
        ),
    )
    return any(
        needle
        in (value.translate(_ASCII_LOWER_TRANSLATION) if case_insensitive else value)
        for value in values
        if value is not None
    )


def _json_search_values(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(
            text
            for key, child in value.items()
            for text in (key, *_json_search_values(child))
        )
    if isinstance(value, list):
        return tuple(text for child in value for text in _json_search_values(child))
    if isinstance(value, str):
        return (value,)
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("true" if value else "false",)
    if isinstance(value, (int, float)):
        return (str(value),)
    raise TraceStoreProtocolError("Trace Graph detail contains a non-JSON value")


def _stored_memory_graph_node(
    thread: _MemoryThread,
    row: ReducedTraceGraphNode,
) -> StoredTraceGraphNode:
    def event(sequence: int | None) -> _MemoryStoredTraceEvent | None:
        if sequence is None:
            return None
        try:
            return thread.events[sequence - 1]
        except IndexError as error:
            raise TraceStoreProtocolError(
                "Trace Graph locator is outside the Ledger",
            ) from error

    started = event(row.started_seq)
    updated = event(row.updated_seq)
    if started is None or updated is None:  # pragma: no cover - required integer fields
        raise TraceStoreProtocolError("Trace Graph node lacks lifecycle facts")
    return StoredTraceGraphNode(
        node_id=row.node_id,
        parent_subagent_id=row.parent_subagent_id,
        model_call_id=row.model_call_id,
        model_call_seq=row.model_call_seq,
        kind=row.kind,
        status=row.status,
        name=row.name,
        run_id=row.run_id,
        graph_namespace=row.graph_namespace,
        agent_name=row.agent_name,
        provider=row.provider,
        model=row.model,
        started_at=row.started_at,
        first_output_at=row.first_output_at,
        completed_at=row.completed_at,
        started_seq=row.started_seq,
        updated_seq=row.updated_seq,
        request_seq=row.request_seq,
        result_seq=row.result_seq,
        failure_seq=row.failure_seq,
        link_issue=row.link_issue,
        started_event=started,
        updated_event=updated,
        request_event=event(row.request_seq),
        result_event=event(row.result_seq),
        failure_event=event(row.failure_seq),
        model_call_event=event(row.model_call_seq),
    )


def _node_order_key(node_id: str) -> str:
    """Match the collision-checked SQL ordering for equal node timestamps."""

    return hashlib.sha256(node_id.encode("utf-8")).hexdigest()


def _snapshot_from_state(
    state: TraceLedgerState,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_key: TraceThreadKey | None,
) -> StoreThreadSnapshot:
    thread = state.thread
    if (
        thread is None
        or thread.key.namespace != expected_namespace
        or thread.key.thread_id != expected_thread_id
        or (expected_key is not None and thread.key != expected_key)
    ):
        raise TraceThreadNotFound("Trace generation does not exist")
    return StoreThreadSnapshot(
        key=thread.key,
        as_of_seq=thread.next_seq - 1,
        persisted_bytes=thread.persisted_bytes,
        active_writers=state.active_writers,
        observed_at=state.observed_at,
    )


def _validate_commit_result(
    change: TraceLedgerChange,
    result: TraceLedgerCommitResult,
) -> None:
    if change.kind == "open_writer":
        identity = change.identity
        key = result.key
        if (
            identity is None
            or key is None
            or key.namespace != change.namespace
            or key.thread_id != identity.thread_id
            or result.run_id != identity.run_id
            or result.owner_token != change.owner_token
            or result.fence is None
            or result.fence < 1
        ):
            raise TraceStoreProtocolError(
                "Trace Ledger writer result conflicts with its request"
            )
        return
    if result.key != change.key:
        raise TraceStoreProtocolError(
            "Trace Ledger commit result conflicts with its generation"
        )
    if (
        change.kind in {"append_events", "renew_writer", "close_writer"}
        and result.run_id != change.run_id
    ):
        raise TraceStoreProtocolError(
            "Trace Ledger commit result conflicts with its Run"
        )
    if change.kind == "append_events":
        expected_ids = tuple(item.event_id for item in change.facts)
        if change.proven_events:
            expected_ids = tuple(event.event_id for event in change.proven_events)
        if tuple(event.event_id for event in result.events) != expected_ids:
            raise TraceStoreProtocolError(
                "Trace Ledger append result conflicts with its event evidence"
            )
        if change.key is None or len(result.events) != len(change.facts):
            raise TraceStoreProtocolError(
                "Trace Ledger append result has incomplete event evidence"
            )
        for event, draft in zip(result.events, change.facts, strict=True):
            expected = _event(
                event_id=draft.event_id,
                trace_seq=event.trace_seq,
                generation=change.key.generation,
                fact=draft.fact,
                copy_fact=False,
            )
            if event != expected:
                raise TraceStoreProtocolError(
                    "Trace Ledger append result conflicts with its committed event"
                )
        sequences = tuple(event.trace_seq for event in result.events)
        if any(right != left + 1 for left, right in zip(sequences, sequences[1:])):
            raise TraceStoreProtocolError(
                "Trace Ledger append result sequence is discontinuous"
            )
    if (
        change.kind == "save_projection_checkpoint"
        and result.checkpoint != change.checkpoint
    ):
        raise TraceStoreProtocolError(
            "Trace Ledger checkpoint result conflicts with its request"
        )


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


__all__ = ["DurableTraceStore", "InMemoryTraceStore"]
