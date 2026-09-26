"""Runtime Observer that records normalized semantic facts into a Trace Store."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from tinkerfin_contracts import (
    RunObservationSession,
    RunSourceContext,
    ThreadIdentity,
)

from ._graph_projection import project_trace_graph_records
from ._projection_cache import ProjectionRegistry
from ._tasks import capture, join_owned_task, select_failure
from ._tracing_session import _TracingSession
from .capture import (
    CapturePolicy,
    ReasoningCapturePolicy,
)
from .durable_store import InMemoryTraceStore
from .errors import TraceStoreProtocolError
from .graph import (
    TraceGraphCompleteness,
    TraceGraphFilter,
    TraceGraphPage,
    TraceGraphQueryLimits,
    bound_graph_page,
)
from .graph_query import TraceGraphQuery, decode_graph_cursor, encode_graph_cursor
from .limits import TraceLimits
from .projection import (
    RegisteredTraceProjection,
    TraceProjection,
    select_core_projection_window,
    select_prior_run_ids,
    trace_graph_turns,
)
from .query import (
    TraceThread,
    build_trace_thread,
    load_core_projection_state,
    read_lineage_events,
    resolve_history_request,
)
from .redaction import (
    TraceRedactor,
    _validate_redactor,
)
from .store import (
    StoreThreadSnapshot,
    TraceGraphRebuildStore,
    TraceGraphStore,
    TraceStore,
    TraceThreadKey,
    TraceWriter,
)
from .writing import TraceWritePolicy

_GRAPH_QUERY_STABILITY_ATTEMPTS = 4


class Tracer:
    """Record Runtime observations and expose deterministic Trace queries.

    Provider reasoning requires an independently configured Runtime extractor. This
    observer omits extracted content unless ``reasoning_capture_policy`` explicitly
    authorizes retention.
    """

    def __init__(
        self,
        *,
        store: TraceStore | None = None,
        capture_policy: CapturePolicy | None = None,
        reasoning_capture_policy: ReasoningCapturePolicy | None = None,
        redactor: TraceRedactor | None = None,
        graph_query_limits: TraceGraphQueryLimits | None = None,
        limits: TraceLimits | None = None,
        write_policy: TraceWritePolicy | None = None,
        projections: tuple[TraceProjection[Any, Any], ...] = (),
    ) -> None:
        """Configure borrowed persistence, capture, batching, and query Projections.

        Args:
            store: Borrowed Store. Omitting it creates a bounded in-process Store owned
                only by this Tracer value.
            capture_policy: Public semantic retention policy. Omitting it retains
                complete sanitized Tool content through ``public_history``.
            reasoning_capture_policy: Independent authorization for extracted provider
                reasoning content.
            redactor: Optional synchronous business-value Redactor. Framework credential
                and private-reasoning safety always runs before and after this extension.
            graph_query_limits: Independent bounds for direct matches, returned parent
                Subagent scopes, and serialized Graph pages and updates.
            limits: Capacity contract; when supplied it must equal ``store.limits``.
            write_policy: Bounded asynchronous batching and backpressure policy.
            projections: Pure business Projections evaluated and cached only on query.

        Raises:
            TypeError: A supplied integration has the wrong public type.
            ValueError: Store limits or Projection registrations conflict.
        """

        if store is not None and not isinstance(store, TraceStore):
            raise TypeError("store must implement TraceStore")
        if limits is not None and not isinstance(limits, TraceLimits):
            raise TypeError("limits must be a TraceLimits or None")
        if capture_policy is not None and not isinstance(
            capture_policy,
            CapturePolicy,
        ):
            raise TypeError("capture_policy must be a CapturePolicy or None")
        if reasoning_capture_policy is not None and not isinstance(
            reasoning_capture_policy,
            ReasoningCapturePolicy,
        ):
            raise TypeError(
                "reasoning_capture_policy must be a ReasoningCapturePolicy or None"
            )
        if redactor is not None:
            _validate_redactor(redactor)
        if graph_query_limits is not None and not isinstance(
            graph_query_limits,
            TraceGraphQueryLimits,
        ):
            raise TypeError(
                "graph_query_limits must be a TraceGraphQueryLimits or None"
            )
        if write_policy is not None and not isinstance(write_policy, TraceWritePolicy):
            raise TypeError("write_policy must be a TraceWritePolicy or None")
        resolved_limits = (
            limits
            if limits is not None
            else (store.limits if store is not None else TraceLimits())
        )
        resolved_store = (
            InMemoryTraceStore(limits=resolved_limits) if store is None else store
        )
        if limits is not None and resolved_store.limits != limits:
            raise ValueError("Tracer limits must match Store limits")
        self._store = resolved_store
        self._capture_policy = (
            CapturePolicy.public_history() if capture_policy is None else capture_policy
        )
        self._reasoning_capture_policy = (
            ReasoningCapturePolicy.omitted()
            if reasoning_capture_policy is None
            else reasoning_capture_policy
        )
        self._redactor = redactor
        self._graph_query_limits = graph_query_limits or TraceGraphQueryLimits()
        self._limits = resolved_limits
        self._write_policy = write_policy or TraceWritePolicy()
        self._projections = _projection_registry(projections)
        self._sessions: dict[tuple[str, str], set[_TracingSession]] = {}

    @property
    def store(self) -> TraceStore:
        """Return the borrowed Store used for writes and queries."""

        return self._store

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        """Open one fail-closed request-scoped semantic observation session.

        The Store remains borrowed by the Tracer. The returned session owns the exact
        Run writer, restores only the selected parent lineage for dedupe, and closes the
        writer on every partial-start failure before propagating the primary error.

        Args:
            context: Canonical Runtime input, Profile, lineage, mode, and privacy facts.

        Returns:
            A request-scoped Observer session owned by the Runtime until close.

        Raises:
            TypeError: ``context`` has the wrong public type.
            TraceStoreError: Writer creation or bounded lineage reads fail.
            TraceStoreProtocolError: The Store returns an invalid writer or snapshot.
            TraceCorruption: Existing lineage facts violate semantic invariants.
        """

        if not isinstance(context, RunSourceContext):
            raise TypeError("context must be a RunSourceContext")
        writer = await self._store.open_writer(context.identity)
        if not isinstance(writer, TraceWriter):
            raise TraceStoreProtocolError("Trace Store returned an invalid writer")
        try:
            if (
                not isinstance(writer.key, TraceThreadKey)
                or _thread_scope(writer.key) != _thread_scope(context.identity)
                or writer.run_id != context.identity.run_id
            ):
                raise TraceStoreProtocolError("Trace writer belongs to another Run")
            snapshot = await self._store.snapshot_key(writer.key)
            if (
                not isinstance(snapshot, StoreThreadSnapshot)
                or snapshot.key != writer.key
            ):
                raise TraceStoreProtocolError(
                    "Trace Store returned an invalid thread snapshot"
                )
            core_state = await load_core_projection_state(
                self._store,
                writer.key,
                as_of_seq=snapshot.as_of_seq,
            )
            prior_run_ids = select_prior_run_ids(
                core_state,
                parent_run_id=context.parent_run_id,
            )
            prior_events = await read_lineage_events(
                self._store,
                writer.key,
                run_ids=prior_run_ids,
                as_of_seq=snapshot.as_of_seq,
            )
            session = _TracingSession(
                writer=writer,
                store=self._store,
                context=context,
                capture_policy=self._capture_policy,
                reasoning_capture_policy=self._reasoning_capture_policy,
                redactor=self._redactor,
                limits=self._limits,
                write_policy=self._write_policy,
                on_closed=self._session_closed,
                prior_events=prior_events,
            )
            self._sessions.setdefault(_thread_scope(context.identity), set()).add(
                session
            )
            return session
        except BaseException as error:
            try:
                # The Tracer owns rejected startup even when a custom writer does
                # not protect its cleanup. Repeated cancellation waits for this
                # close; capture keeps process control in the calling owner.
                cleanup = asyncio.create_task(
                    capture(writer.aclose()), name="tinkerfin-trace-open-cleanup"
                )
                await join_owned_task(cleanup, cancel_operation=False)
            except BaseException as close_error:  # noqa: BLE001 - preserve primary error
                # Rejected startup must retain cleanup evidence as well as the
                # invalid scope. Control and cancellation keep their precedence.
                raise select_failure(error, close_error)
            raise

    async def get(
        self,
        identity: ThreadIdentity,
        *,
        head_run_id: str | None = None,
        limit: int = 100,
        history_cursor: str | None = None,
        projections: tuple[str, ...] = (),
        at_run_start: bool = False,
    ) -> TraceThread:
        """Return one fixed-as-of semantic handle for the current generation.

        Active sessions owned by this Tracer are forced before the Store snapshot. The
        opaque history cursor can only expand the same generation, head, and as-of
        prefix; it never admits facts committed after that boundary.

        Args:
            identity: Namespace and thread to read, usually from ``runtime.thread_identity``.
            head_run_id: Optional explicit branch head.
            limit: Positive number of latest Turns in the initial window.
            history_cursor: Optional opaque cursor from the same fixed prefix.
            projections: Unique registered Projection names requested for this query.
            at_run_start: Read the admitted request before any output of the selected
                run. Cannot be combined with a history cursor.

        Returns:
            Immutable Trace view with bounded history and a closeable follow iterator.

        Raises:
            TypeError: ``identity`` is not a ThreadIdentity.
            ValueError: Limit, cursor, or Projection names are invalid.
            TraceThreadNotFound: The thread or cursor generation is unavailable.
            TraceRunNotFound: The explicitly selected Run has not entered the generation.
            AmbiguousTraceHead: Multiple heads exist without an explicit selection.
            TraceStoreError: Snapshot, event, or Projection checkpoint access fails.
            TraceProjectionFailed: A requested business Projection cannot be evaluated.
        """

        if not isinstance(identity, ThreadIdentity):
            raise TypeError("identity must be a ThreadIdentity")
        if head_run_id is not None and (
            not isinstance(head_run_id, str)
            or not head_run_id
            or head_run_id != head_run_id.strip()
        ):
            raise ValueError("head_run_id must be canonical non-empty text or None")
        if any(
            not isinstance(name, str) or not name or name != name.strip()
            for name in projections
        ):
            raise ValueError("Projection names must be canonical non-empty text")
        if len(set(projections)) != len(projections):
            raise ValueError("Trace Projection names must be unique")
        if history_cursor is not None and (
            not isinstance(history_cursor, str) or not history_cursor
        ):
            raise ValueError("history_cursor must be non-empty text or None")
        if not isinstance(at_run_start, bool):
            raise TypeError("at_run_start must be a bool")
        if at_run_start and history_cursor is not None:
            raise ValueError("at_run_start cannot be combined with a history cursor")
        sessions = tuple(self._sessions.get(_thread_scope(identity), ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        snapshot, resolved_head, resolved_limit = await resolve_history_request(
            self._store,
            identity=identity,
            head_run_id=head_run_id,
            history_cursor=history_cursor,
            limit=limit,
        )
        if not isinstance(snapshot, StoreThreadSnapshot) or _thread_scope(
            snapshot.key
        ) != _thread_scope(identity):
            raise TraceStoreProtocolError(
                "Trace Store returned an invalid thread snapshot"
            )
        return await build_trace_thread(
            store=self._store,
            snapshot=snapshot,
            head_run_id=resolved_head,
            limit=resolved_limit,
            graph_query_limits=self._graph_query_limits,
            projections=self._projections,
            projection_names=projections,
            at_run_start=at_run_start,
        )

    async def query(
        self,
        identity: ThreadIdentity,
        *,
        where: TraceGraphFilter | None = None,
        head_run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TraceGraphQuery:
        """Read the current execution graph for one namespaced conversation.

        Args:
            identity: Complete namespace and thread identity.
            where: Optional filters for nodes and retained public content.
            head_run_id: Branch head, required when the conversation has several heads.
            cursor: Cursor for the same generation, branch, filter, and current tail.
            limit: Maximum direct matches, before owning subagents and associated
                compaction context are included.

        Returns:
            Graph page with a closeable follow iterator on its current first page.

        Raises:
            TypeError: Identity or filter has the wrong public type.
            ValueError: Head or page limit is invalid.
            InvalidTraceCursor: The cursor no longer names this query's current tail.
            TraceStoreError: The graph cannot be read consistently in its namespace.
            TraceQuotaExceeded: Search or graph expansion exceeds the query budget.
        """

        if not isinstance(identity, ThreadIdentity):
            raise TypeError("identity must be a ThreadIdentity")
        if head_run_id is not None and (
            not isinstance(head_run_id, str)
            or not head_run_id
            or head_run_id != head_run_id.strip()
        ):
            raise ValueError("head_run_id must be canonical non-empty text or None")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._graph_query_limits.max_direct_nodes
        ):
            raise ValueError(
                "limit must be an integer between 1 and "
                f"{self._graph_query_limits.max_direct_nodes}"
            )
        if where is not None and not isinstance(where, TraceGraphFilter):
            raise TypeError("where must be a TraceGraphFilter or None")
        resolved_filter = where or TraceGraphFilter()
        page, key = await self._query_graph_page(
            identity,
            where=resolved_filter,
            head_run_id=head_run_id,
            cursor=cursor,
            limit=limit,
            expected_key=None,
        )

        async def refresh() -> TraceGraphPage:
            refreshed, _key = await self._query_graph_page(
                identity,
                where=resolved_filter,
                head_run_id=head_run_id,
                cursor=None,
                limit=limit,
                expected_key=key,
            )
            return refreshed

        return TraceGraphQuery(
            page,
            store=self._store,
            key=key,
            refresh=refresh,
            follow_enabled=cursor is None,
            max_page_bytes=self._graph_query_limits.max_page_bytes,
        )

    async def rebuild_graph(self, identity: ThreadIdentity) -> int:
        """Rebuild one conversation's execution graph from its retained facts.

        Args:
            identity: Complete namespace and thread identity to rebuild.

        Returns:
            Number of reconstructed graph nodes.

        Raises:
            TypeError: ``identity`` is not a ThreadIdentity.
            TraceThreadNotFound: No current generation exists for the identity.
            TraceStoreError: Storage cannot rebuild or the history changes during it.
        """

        if not isinstance(identity, ThreadIdentity):
            raise TypeError("identity must be a ThreadIdentity")
        sessions = tuple(self._sessions.get(_thread_scope(identity), ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        store = self._store
        if (
            not isinstance(store, TraceGraphRebuildStore)
            or not store.supports_graph_rebuild
        ):
            raise TraceStoreProtocolError("Trace Store does not rebuild Graph nodes")
        snapshot = await store.snapshot(identity)
        if _thread_scope(snapshot.key) != _thread_scope(identity):
            raise TraceStoreProtocolError("Trace snapshot belongs to another thread")
        return await store.rebuild_trace_graph(snapshot.key)

    async def _query_graph_page(
        self,
        identity: ThreadIdentity,
        *,
        where: TraceGraphFilter,
        head_run_id: str | None,
        cursor: str | None,
        limit: int,
        expected_key: TraceThreadKey | None,
    ) -> tuple[TraceGraphPage, TraceThreadKey]:
        sessions = tuple(self._sessions.get(_thread_scope(identity), ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        store = self._store
        if not isinstance(store, TraceGraphStore) or not store.supports_graph_queries:
            raise TraceStoreProtocolError(
                "Trace Store does not provide indexed Graph queries"
            )
        for _attempt in range(_GRAPH_QUERY_STABILITY_ATTEMPTS):
            snapshot = await store.snapshot(identity)
            if _thread_scope(snapshot.key) != _thread_scope(identity):
                raise TraceStoreProtocolError(
                    "Trace snapshot belongs to another thread"
                )
            if expected_key is not None and snapshot.key != expected_key:
                raise TraceStoreProtocolError(
                    "Trace Graph follow generation changed during the query"
                )
            core_state = await load_core_projection_state(
                store,
                snapshot.key,
                as_of_seq=snapshot.as_of_seq,
            )
            window = select_core_projection_window(
                core_state,
                head_run_id=head_run_id,
                turn_limit=max(1, len(core_state.turn_order)),
            )
            before_started_at: datetime | None = None
            before_node_id: str | None = None
            if cursor is not None:
                before_started_at, before_node_id = decode_graph_cursor(
                    cursor,
                    key=snapshot.key,
                    head_run_id=head_run_id,
                    as_of_seq=snapshot.as_of_seq,
                    where=where,
                )
            records = await store.query_trace_graph(
                snapshot.key,
                run_ids=tuple(sorted(window.selected_run_ids)),
                where=where,
                limit=limit,
                max_nodes=self._graph_query_limits.max_total_nodes,
                before_started_at=before_started_at,
                before_node_id=before_node_id,
            )
            if records.as_of_seq == snapshot.as_of_seq:
                break
        else:
            raise TraceStoreProtocolError(
                "Trace Graph changed repeatedly during one consistent query"
            )
        turn_ids = {
            window.run_turns[record.started_event.fact.identity.run_id]
            for record in records.nodes
            if record.started_event.fact.identity.run_id in window.run_turns
        }
        turns = trace_graph_turns(core_state, window, selected_turn_ids=turn_ids)
        nodes, ordered_ids = project_trace_graph_records(
            records.nodes,
            turns=turns,
            run_turns=window.run_turns,
            selected_run_ids=window.selected_run_ids,
        )
        direct_ids = frozenset(records.matched_node_ids)
        matched_node_ids = tuple(
            node_id for node_id in ordered_ids if node_id in direct_ids
        )
        next_cursor = (
            encode_graph_cursor(
                key=snapshot.key,
                head_run_id=head_run_id,
                as_of_seq=records.as_of_seq,
                where=where,
                before_started_at=records.next_started_at,
                before_node_id=records.next_node_id,
            )
            if records.has_more
            and records.next_started_at is not None
            and records.next_node_id is not None
            else None
        )
        relationship_missing = records.relationship_evidence_missing or any(
            node.link_issues for node in nodes
        )
        details_omitted = any(
            node.content_omitted or node.request_omitted or node.result_omitted
            for node in nodes
        )
        page = bound_graph_page(
            TraceGraphPage(
                turns=turns,
                nodes=nodes,
                ordered_node_ids=ordered_ids,
                matched_node_ids=matched_node_ids,
                next_cursor=next_cursor,
                as_of_seq=records.as_of_seq,
                completeness=TraceGraphCompleteness(
                    call_tracking_missing=not all(
                        core_state.runs[run_id].call_history_known
                        for run_id in window.selected_run_ids
                    ),
                    relationship_evidence_missing=relationship_missing,
                    details_omitted=details_omitted,
                ),
            ),
            max_bytes=self._graph_query_limits.max_page_bytes,
        )
        return (
            page,
            snapshot.key,
        )

    def _session_closed(self, session: _TracingSession) -> None:
        """Remove one settled session from same-Tracer read-your-writes flushing."""

        scope = _thread_scope(session._context.identity)
        sessions = self._sessions.get(scope)
        if sessions is None:
            return
        sessions.discard(session)
        if not sessions:
            self._sessions.pop(scope, None)


def _thread_scope(identity: ThreadIdentity) -> tuple[str, str]:
    return identity.namespace, identity.thread_id


def _projection_registry(
    projections: Sequence[RegisteredTraceProjection],
) -> Mapping[str, RegisteredTraceProjection]:
    values: dict[str, RegisteredTraceProjection] = {}
    for projection in projections:
        if not isinstance(projection, TraceProjection):
            raise TypeError("projection must implement TraceProjection")
        name = projection.name
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("Projection name must be canonical non-empty text")
        if name in values:
            raise ValueError(f"duplicate Trace Projection name: {name}")
        if not issubclass(projection.state_type, BaseModel) or not issubclass(
            projection.result_type, BaseModel
        ):
            raise TypeError("Projection state and result types must be Pydantic models")
        values[name] = projection
    return ProjectionRegistry(values)


__all__ = ["Tracer"]
