"""Fixed-as-of Trace handles, event pagination, and live semantic following."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TypeVar, cast

from pydantic import BaseModel, Field, JsonValue, ValidationError, model_validator

from tinkerfin_contracts import ThreadIdentity

from ._graph_projection import (
    project_trace_graph_records,
    reduce_trace_graph_records,
)
from ._models import TraceModel
from .errors import (
    InvalidTraceCursor,
    TraceProjectionCheckpointConflict,
    TraceProjectionFailed,
    TraceQuotaExceeded,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from .facts import TraceEvent, TraceSemanticFact
from .follow import TraceFollow, _close_trace_source, create_trace_follow
from .graph import (
    TraceGraph,
    TraceGraphCompleteness,
    TraceGraphFilter,
    TraceGraphQueryLimits,
    bound_graph,
    graph_delta,
)
from .projection import (
    CoreProjection,
    CoreProjectionState,
    RegisteredTraceProjection,
    advance_core_projection_state,
    advance_projection_state,
    empty_core_projection_state,
    finish_projection,
    project_core_checkpoint,
    select_core_projection_window,
    trace_graph_turns,
)
from .store import (
    StoreThreadSnapshot,
    StoreWriterSnapshot,
    TraceGraphStore,
    TraceProjectionCheckpoint,
    TraceStore,
    TraceStoreUpdate,
    TraceThreadKey,
)
from .views import (
    TraceCompleteness,
    TraceEntityDelta,
    TraceEventPage,
    TraceInteraction,
    TraceMessage,
    TraceReasoning,
    TraceState,
    TraceStatus,
    TraceSummary,
    TraceUpdate,
)

EntityT = TypeVar("EntityT", bound=BaseModel)
_CORE_PROJECTION_NAME = "tinkerfin.core.summary"


class _CursorPayload(TraceModel, frozen=True):
    namespace: str
    thread_id: str
    generation: str
    head_run_id: str
    page_as_of_seq: int = Field(ge=0)
    last_seq: int = Field(ge=0)

    @model_validator(mode="after")
    def last_sequence_is_inside_the_page_prefix(self) -> _CursorPayload:
        if self.last_seq > self.page_as_of_seq:
            raise ValueError("cursor last_seq exceeds page_as_of_seq")
        return self


class _HistoryCursorPayload(TraceModel, frozen=True):
    namespace: str
    thread_id: str
    generation: str
    head_run_id: str
    page_as_of_seq: int = Field(ge=1)
    persisted_bytes: int = Field(ge=1)
    active_writers: tuple[StoreWriterSnapshot, ...]
    observed_at: datetime
    loaded_turns: int = Field(ge=1)


class TraceThread:
    """Own one generation-bound, fixed-as-of semantic Trace view."""

    def __init__(
        self,
        *,
        store: TraceStore,
        snapshot: StoreThreadSnapshot,
        core_state: CoreProjectionState,
        core: CoreProjection,
        graph: TraceGraph,
        graph_query_limits: TraceGraphQueryLimits,
        turn_limit: int,
        head_requested: str | None,
        projections: Mapping[str, RegisteredTraceProjection],
        projection_names: tuple[str, ...],
        projection_results: Mapping[str, BaseModel],
    ) -> None:
        """Bind one fixed Store prefix to materialized core and business views.

        The handle borrows the Store and never closes it. It owns only materialized
        immutable views and its local history-window size; deleting the exact generation
        invalidates every subsequent operation on this handle.

        Args:
            store: Borrowed Store used for paging, follow, Projection, and deletion.
            snapshot: Exact generation and fixed global as-of boundary.
            core_state: Incremental framework Projection state at that boundary.
            core: Materialized selected-head view for the initial Turn window.
            graph: Canonical Graph for the same fixed prefix and Turn window.
            graph_query_limits: Bounds applied to history Graph materialization.
            turn_limit: Number of latest Turns currently visible.
            head_requested: Optional explicit head retained for follow projection.
            projections: Registered business Projection implementations.
            projection_names: Requested Projection names in caller order.
            projection_results: Materialized results at the fixed prefix.
        """

        self._store = store
        self._snapshot = snapshot
        self._core_state = core_state
        self._core = core
        self._graph = graph
        self._graph_query_limits = graph_query_limits
        self._turn_limit = turn_limit
        self._head_requested = head_requested
        self._projection_registry = projections
        self._projection_names = projection_names
        self._projection_results = MappingProxyType(dict(projection_results))
        self._deleted = False

    @property
    def key(self) -> TraceThreadKey:
        """Return the exact Store generation bound to this handle."""

        return self._snapshot.key

    @property
    def as_of_seq(self) -> int:
        """Return the immutable global Ledger prefix used by this handle."""

        return self._snapshot.as_of_seq

    @property
    def observed_at(self) -> datetime:
        """Return the storage UTC time when this fixed view's ownership was read."""
        return self._snapshot.observed_at

    @property
    def head_run_id(self) -> str:
        """Return the selected current Run head for this fixed prefix."""

        self._ensure_live()
        return self._core.selected_head

    @property
    def available_heads(self) -> tuple[str, ...]:
        """Return every selectable Run head present in this fixed prefix."""

        self._ensure_live()
        return self._core.available_heads

    @property
    def messages(self) -> tuple[TraceMessage, ...]:
        """Return chronological semantic messages in the loaded Turn window."""

        self._ensure_live()
        return tuple(message.model_copy(deep=True) for message in self._core.messages)

    @property
    def reasoning(self) -> tuple[TraceReasoning, ...]:
        """Return explicitly extracted reasoning in the loaded Turn window."""

        self._ensure_live()
        return tuple(item.model_copy(deep=True) for item in self._core.reasoning)

    @property
    def graph(self) -> TraceGraph:
        """Return the canonical execution Graph in the loaded Turn window."""

        self._ensure_live()
        return self._graph.model_copy(deep=True)

    @property
    def state(self) -> TraceState:
        """Return complete head-scoped state at this handle's as-of sequence."""

        self._ensure_live()
        return self._core.state.model_copy(deep=True)

    @property
    def interactions(self) -> tuple[TraceInteraction, ...]:
        """Return interactions in the loaded Turn window."""

        self._ensure_live()
        return tuple(
            interaction.model_copy(deep=True) for interaction in self._core.interactions
        )

    @property
    def status(self) -> TraceStatus:
        """Return the selected head's current execution status."""

        self._ensure_live()
        return self._core.summary.status.model_copy(deep=True)

    @property
    def completeness(self) -> TraceCompleteness:
        """Return structural and retained-payload completeness signals."""

        self._ensure_live()
        return self._core.summary.completeness.model_copy(deep=True)

    @property
    def summary(self) -> TraceSummary:
        """Return complete cumulative status for this selected fixed-as-of lineage."""

        self._ensure_live()
        return self._core.summary.model_copy(deep=True)

    @property
    def has_older(self) -> bool:
        """Return whether earlier Turns can be loaded into this handle."""

        self._ensure_live()
        return self._core.has_older

    @property
    def message_count(self) -> int:
        """Return full selected-lineage user and assistant message count."""

        self._ensure_live()
        return self._core.summary.message_count

    @property
    def tool_call_count(self) -> int:
        """Return full selected-lineage Tool proposal count."""

        self._ensure_live()
        return self._core.summary.tool_call_count

    @property
    def history_cursor(self) -> str | None:
        """Return an opaque fixed-as-of cursor for expanding the Turn window."""

        self._ensure_live()
        if not self._core.has_older:
            return None
        return _encode_history_cursor(
            _HistoryCursorPayload(
                namespace=self.key.namespace,
                thread_id=self.key.thread_id,
                generation=self.key.generation,
                head_run_id=self.head_run_id,
                page_as_of_seq=self.as_of_seq,
                persisted_bytes=self._snapshot.persisted_bytes,
                active_writers=self._snapshot.active_writers,
                observed_at=self.observed_at,
                loaded_turns=self._turn_limit,
            )
        )

    @property
    def projections(self) -> Mapping[str, BaseModel]:
        """Return results for explicitly requested business Projections."""

        self._ensure_live()
        return MappingProxyType(
            {
                name: result.model_copy(deep=True)
                for name, result in self._projection_results.items()
            }
        )

    async def load_older(self, *, limit: int = 100) -> TraceThread:
        """Expand the Turn window without changing this handle's as-of prefix.

        Args:
            limit: Positive number of additional earlier Turns to expose.

        Returns:
            This same generation-bound handle after local materialization expands.

        Raises:
            ValueError: ``limit`` is not positive and bounded.
            TraceThreadNotFound: This handle was deleted.
        """

        self._ensure_live()
        _validate_limit(limit)
        if not self._core.has_older:
            return self
        next_limit = self._turn_limit + limit
        next_core = project_core_checkpoint(
            self._core_state,
            head_run_id=self._core.selected_head,
            turn_limit=next_limit,
            active_run_ids=self._snapshot.active_run_ids,
        )
        next_graph = await _materialize_history_graph(
            self._store,
            key=self.key,
            as_of_seq=self.as_of_seq,
            core_state=self._core_state,
            core=next_core,
            turn_limit=next_limit,
            limits=self._graph_query_limits,
        )
        self._turn_limit = next_limit
        self._core = next_core
        self._graph = next_graph
        return self

    async def events(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TraceEventPage:
        """Read one ascending selected-lineage page at this exact fixed prefix.

        Args:
            cursor: Optional opaque cursor returned by the preceding page.
            limit: Positive bounded maximum number of events.

        Returns:
            Immutable page plus another cursor when selected events remain.

        Raises:
            InvalidTraceCursor: The cursor belongs to another generation, head, or
                as-of boundary.
            ValueError: ``limit`` is invalid.
            TraceThreadNotFound: This handle or exact generation is unavailable.
            TraceStoreError: The bounded Store read fails.
        """

        self._ensure_live()
        _validate_limit(limit)
        if cursor is None:
            page_as_of_seq = self.as_of_seq
            last_seq = 0
        else:
            payload = _decode_cursor(cursor)
            if (
                payload.namespace != self.key.namespace
                or payload.thread_id != self.key.thread_id
                or payload.generation != self.key.generation
                or payload.head_run_id != self.head_run_id
                or payload.page_as_of_seq != self.as_of_seq
            ):
                raise InvalidTraceCursor("Trace cursor belongs to another handle")
            page_as_of_seq = payload.page_as_of_seq
            last_seq = payload.last_seq
        items, final_seq = await _read_selected_events(
            self._store,
            self.key,
            selected_run_ids=self._core.selected_run_ids,
            after_seq=last_seq,
            as_of_seq=page_as_of_seq,
            limit=limit,
        )
        next_cursor = (
            _encode_cursor(
                _CursorPayload(
                    namespace=self.key.namespace,
                    thread_id=self.key.thread_id,
                    generation=self.key.generation,
                    head_run_id=self.head_run_id,
                    page_as_of_seq=page_as_of_seq,
                    last_seq=final_seq,
                )
            )
            if final_seq < page_as_of_seq
            else None
        )
        return TraceEventPage(
            items=items,
            next_cursor=next_cursor,
            page_as_of_seq=page_as_of_seq,
        )

    def follow(self) -> TraceFollow[TraceUpdate]:
        """Follow committed events and execution changes after this fixed view.

        The iterator follows the exact generation and selected head. Sibling-branch
        batches advance internal projection state but are not emitted. Writer loss
        updates status and completeness at the same sequence without inventing events.
        Normal exhaustion, cancellation, and explicit close settle the borrowed Store
        follower. Use ``async with`` when the consumer may break early.

        Returns:
            A single-use follower of defensive updates in committed global sequence
            order. Use it as an asynchronous context manager when iteration may stop
            early, or call ``aclose()`` explicitly.

        Raises:
            TraceFollowLifecycleError: The returned follower is reused concurrently or
                entered after closing.
            TraceThreadNotFound: The generation is deleted or replaced.
            TraceStoreProtocolError: A batch is discontinuous or malformed.
            TraceProjectionFailed: A requested business Projection fails.
            TraceStoreError: Snapshot, follow, or checkpoint access fails.
        """

        async def updates() -> AsyncIterator[TraceUpdate]:
            self._ensure_live()
            core_state = self._core_state
            previous = self._core
            previous_graph = self._graph
            batches = self._store.follow(self.key, after_seq=self.as_of_seq)
            primary_error: BaseException | None = None
            try:
                async for stored_update in batches:
                    if not isinstance(stored_update, TraceStoreUpdate):
                        raise TraceStoreProtocolError(
                            "Trace Store returned an invalid follow update"
                        )
                    batch = stored_update.events
                    _validate_event_batch(
                        batch,
                        key=self.key,
                        expected_after=core_state.as_of_seq,
                    )
                    core_state = advance_core_projection_state(core_state, batch)
                    if stored_update.as_of_seq != core_state.as_of_seq:
                        raise TraceStoreProtocolError(
                            "Trace Store follow cursor conflicts with its events"
                        )
                    current = project_core_checkpoint(
                        core_state,
                        head_run_id=self._head_requested,
                        turn_limit=self._turn_limit,
                        active_run_ids=stored_update.active_run_ids,
                    )
                    selected_batch = tuple(
                        event
                        for event in batch
                        if event.fact.identity.run_id in current.selected_run_ids
                    )
                    if not selected_batch and current.summary == previous.summary:
                        previous = current
                        continue
                    current_graph = await _materialize_history_graph(
                        self._store,
                        key=self.key,
                        as_of_seq=core_state.as_of_seq,
                        core_state=core_state,
                        core=current,
                        turn_limit=self._turn_limit,
                        limits=self._graph_query_limits,
                    )
                    projection_results = await _projection_results(
                        self._projection_registry,
                        self._projection_names,
                        store=self._store,
                        key=self.key,
                        core_state=core_state,
                        head_run_id=current.selected_head,
                        run_ids=current.selected_run_ids,
                        as_of_seq=core_state.as_of_seq,
                    )
                    update = TraceUpdate(
                        generation=self.key.generation,
                        observed_at=stored_update.observed_at,
                        as_of_seq=core_state.as_of_seq,
                        events=selected_batch,
                        facts=tuple(event.fact for event in selected_batch),
                        messages=_entity_delta(previous.messages, current.messages),
                        reasoning=_entity_delta(
                            previous.reasoning,
                            current.reasoning,
                        ),
                        graph=graph_delta(
                            previous_graph,
                            current_graph,
                            max_bytes=self._graph_query_limits.max_page_bytes,
                        ),
                        interactions=_entity_delta(
                            previous.interactions,
                            current.interactions,
                        ),
                        state=current.state,
                        summary=current.summary,
                        projections={
                            name: result.model_dump(mode="json", by_alias=True)
                            for name, result in projection_results.items()
                        },
                    )
                    previous = current
                    previous_graph = current_graph
                    yield update.model_copy(deep=True)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                await _close_trace_source(
                    batches,
                    primary_error=primary_error,
                )

        return create_trace_follow(updates)

    async def delete(self) -> None:
        """Delete this exact inactive generation and invalidate the handle.

        Raises:
            TraceRunConflict: An active writer still owns the generation.
            TraceThreadNotFound: The handle was already invalidated or replaced.
            TraceStoreError: Durable deletion fails.
        """

        self._ensure_live()
        await self._store.delete(self.key)
        self._deleted = True

    def _ensure_live(self) -> None:
        if self._deleted:
            raise TraceThreadNotFound(
                "Trace handle generation was deleted",
                context={"thread_id": self.key.thread_id},
            )


async def resolve_history_request(
    store: TraceStore,
    *,
    identity: ThreadIdentity,
    head_run_id: str | None,
    history_cursor: str | None,
    limit: int,
) -> tuple[StoreThreadSnapshot, str | None, int]:
    """Resolve a latest or opaque fixed-as-of history request before projection."""

    _validate_limit(limit)
    if history_cursor is None:
        return await store.snapshot(identity), head_run_id, limit
    payload = _decode_history_cursor(history_cursor)
    if (
        payload.namespace != identity.namespace
        or payload.thread_id != identity.thread_id
    ):
        raise InvalidTraceCursor("Trace history cursor belongs to another thread")
    if head_run_id is not None and head_run_id != payload.head_run_id:
        raise InvalidTraceCursor("Trace history cursor belongs to another Run head")
    current = await store.snapshot(identity)
    if (
        current.key.namespace != payload.namespace
        or current.key.generation != payload.generation
        or current.as_of_seq < payload.page_as_of_seq
    ):
        raise InvalidTraceCursor("Trace history cursor belongs to another generation")
    return (
        StoreThreadSnapshot(
            key=current.key,
            as_of_seq=payload.page_as_of_seq,
            persisted_bytes=payload.persisted_bytes,
            active_writers=payload.active_writers,
            observed_at=payload.observed_at,
        ),
        payload.head_run_id,
        payload.loaded_turns + limit,
    )


async def build_trace_thread(
    *,
    store: TraceStore,
    snapshot: StoreThreadSnapshot,
    head_run_id: str | None,
    limit: int,
    graph_query_limits: TraceGraphQueryLimits,
    projections: Mapping[str, RegisteredTraceProjection],
    projection_names: tuple[str, ...],
) -> TraceThread:
    """Validate a Store snapshot and construct one fixed-as-of Trace handle."""

    _validate_limit(limit)
    _validate_snapshot(snapshot)
    if len(set(projection_names)) != len(projection_names):
        raise ValueError("Trace Projection names must be unique")
    core_state = await load_core_projection_state(
        store,
        snapshot.key,
        as_of_seq=snapshot.as_of_seq,
    )
    core = project_core_checkpoint(
        core_state,
        head_run_id=head_run_id,
        turn_limit=limit,
        active_run_ids=snapshot.active_run_ids,
    )
    graph = await _materialize_history_graph(
        store,
        key=snapshot.key,
        as_of_seq=snapshot.as_of_seq,
        core_state=core_state,
        core=core,
        turn_limit=limit,
        limits=graph_query_limits,
    )
    return TraceThread(
        store=store,
        snapshot=snapshot,
        core_state=core_state,
        core=core,
        graph=graph,
        graph_query_limits=graph_query_limits,
        turn_limit=limit,
        head_requested=head_run_id,
        projections=projections,
        projection_names=projection_names,
        projection_results=await _projection_results(
            projections,
            projection_names,
            store=store,
            key=snapshot.key,
            core_state=core_state,
            head_run_id=core.selected_head,
            run_ids=core.selected_run_ids,
            as_of_seq=snapshot.as_of_seq,
        ),
    )


def _validate_snapshot(snapshot: StoreThreadSnapshot) -> None:
    if not isinstance(snapshot, StoreThreadSnapshot):
        raise TraceStoreProtocolError("Trace Store returned an invalid thread snapshot")
    if snapshot.as_of_seq == 0 and snapshot.persisted_bytes != 0:
        raise TraceStoreProtocolError(
            "Trace Store returned an inconsistent persisted byte count"
        )
    if any(
        writer.committed_events > snapshot.as_of_seq
        for writer in snapshot.active_writers
    ):
        raise TraceStoreProtocolError(
            "Trace Store returned invalid active writer metadata"
        )


async def load_core_projection_state(
    store: TraceStore,
    key: TraceThreadKey,
    *,
    as_of_seq: int,
) -> CoreProjectionState:
    """Load and CAS-catch-up the disposable core state to one fixed prefix.

    A healthy writer normally leaves an exact checkpoint. Missing or stale cache is
    rebuilt from bounded Ledger pages; a concurrent checkpoint winner causes a retry,
    while a winner beyond this historical prefix leaves the locally rebuilt fixed state
    valid without attempting to overwrite newer cache history.

    Args:
        store: Trace Store owning Ledger pages and disposable checkpoints.
        key: Exact immutable Trace generation to project.
        as_of_seq: Fixed global Ledger prefix required by the caller.

    Returns:
        Core projection state exactly representing ``as_of_seq``.

    Raises:
        ValueError: ``as_of_seq`` is negative.
        TraceStoreError: Ledger or checkpoint access fails.
        TraceStoreProtocolError: Stored checkpoint or event evidence is inconsistent.
    """

    if as_of_seq < 0:
        raise ValueError("as_of_seq must be non-negative")
    while True:
        checkpoint = await store.load_projection_checkpoint(
            key,
            projection_name=_CORE_PROJECTION_NAME,
            run_id=None,
            as_of_seq=as_of_seq,
        )
        if checkpoint is None:
            state = empty_core_projection_state()
            expected_seq: int | None = None
        else:
            try:
                state = CoreProjectionState.model_validate_json(
                    json.dumps(
                        checkpoint.state,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                )
            except ValidationError as error:
                raise TraceStoreProtocolError(
                    "Trace Store returned an invalid core Projection checkpoint",
                    cause=error,
                ) from error
            if state.as_of_seq != checkpoint.as_of_seq or (
                state.as_of_seq > 0 and state.generation != key.generation
            ):
                raise TraceStoreProtocolError(
                    "Core Projection checkpoint metadata is inconsistent"
                )
            expected_seq = checkpoint.as_of_seq
        cursor = state.as_of_seq
        while cursor < as_of_seq:
            batch = await store.read_events(
                key,
                after_seq=cursor,
                as_of_seq=as_of_seq,
                limit=store.limits.follow_batch_size,
            )
            if not batch:
                raise TraceStoreProtocolError(
                    "Trace Store returned a gap while rebuilding core Projection"
                )
            _validate_event_batch(batch, key=key, expected_after=cursor)
            state = advance_core_projection_state(state, batch)
            cursor = state.as_of_seq
        if state.as_of_seq == (0 if checkpoint is None else checkpoint.as_of_seq):
            return state
        candidate = TraceProjectionCheckpoint(
            key=key,
            projection_name=_CORE_PROJECTION_NAME,
            run_id=None,
            as_of_seq=state.as_of_seq,
            state=cast(
                JsonValue,
                state.model_dump(mode="json", by_alias=True),
            ),
        )
        try:
            await store.save_projection_checkpoint(
                candidate,
                expected_as_of_seq=expected_seq,
            )
        except TraceProjectionCheckpointConflict as error:
            current = error.context.get("current_as_of_seq")
            if isinstance(current, int) and current > as_of_seq:
                return state
            continue
        return state


async def read_lineage_events(
    store: TraceStore,
    key: TraceThreadKey,
    *,
    run_ids: frozenset[str],
    as_of_seq: int,
) -> tuple[TraceEvent, ...]:
    """Load selected lineage events for controlled cache recovery or writer hydration."""

    return await _read_events_for_runs(
        store,
        key,
        run_ids=run_ids,
        after_seq=0,
        as_of_seq=as_of_seq,
    )


async def _read_lineage_facts(
    store: TraceStore,
    key: TraceThreadKey,
    *,
    run_ids: frozenset[str],
    as_of_seq: int,
) -> tuple[TraceSemanticFact, ...]:
    events = await read_lineage_events(
        store,
        key,
        run_ids=run_ids,
        as_of_seq=as_of_seq,
    )
    return tuple(event.fact for event in events)


async def _read_events_for_runs(
    store: TraceStore,
    key: TraceThreadKey,
    *,
    run_ids: frozenset[str],
    after_seq: int,
    as_of_seq: int,
) -> tuple[TraceEvent, ...]:
    """Scan bounded contiguous pages and retain only requested semantic Runs."""

    selected: list[TraceEvent] = []
    cursor = after_seq
    while cursor < as_of_seq:
        batch = await store.read_events(
            key,
            after_seq=cursor,
            as_of_seq=as_of_seq,
            limit=store.limits.follow_batch_size,
        )
        if not batch:
            raise TraceStoreProtocolError(
                "Trace Store returned a gap inside a fixed event range"
            )
        _validate_event_batch(batch, key=key, expected_after=cursor)
        cursor = batch[-1].trace_seq
        selected.extend(
            event for event in batch if event.fact.identity.run_id in run_ids
        )
    return tuple(selected)


async def _materialize_history_graph(
    store: TraceStore,
    *,
    key: TraceThreadKey,
    as_of_seq: int,
    core_state: CoreProjectionState,
    core: CoreProjection,
    turn_limit: int,
    limits: TraceGraphQueryLimits,
) -> TraceGraph:
    """Build one fixed-prefix Graph without persisting a second Graph copy."""

    window = select_core_projection_window(
        core_state,
        head_run_id=core.selected_head,
        turn_limit=turn_limit,
    )
    where = TraceGraphFilter()
    records = None
    call_history_known = bool(window.visible_run_ids) and all(
        core_state.runs[run_id].call_history_known for run_id in window.visible_run_ids
    )
    relationship_evidence_missing = False
    if isinstance(store, TraceGraphStore) and store.supports_graph_queries:
        current = await store.query_trace_graph(
            key,
            run_ids=tuple(sorted(window.selected_run_ids)),
            started_run_ids=tuple(sorted(window.visible_run_ids)),
            where=where,
            limit=limits.max_direct_nodes,
            max_nodes=limits.max_total_nodes,
        )
        if current.as_of_seq == as_of_seq:
            if current.has_more:
                raise TraceQuotaExceeded(
                    "Trace history Graph exceeds max_direct_nodes",
                    context={"resource": "graph_direct_nodes"},
                )
            records = current.nodes
            relationship_evidence_missing = current.relationship_evidence_missing
    if records is None:
        # A fixed-prefix rebuild needs the whole lineage to resolve a later
        # settlement's original start. Keep the existing bounded Ledger pages,
        # then select visible starts before applying the Graph output quota.
        events = await _read_events_for_runs(
            store,
            key,
            run_ids=window.selected_run_ids,
            after_seq=0,
            as_of_seq=as_of_seq,
        )
        records = reduce_trace_graph_records(
            events,
            run_ids=window.selected_run_ids,
        )
        records = tuple(
            record
            for record in records
            if record.started_event.fact.identity.run_id in window.visible_run_ids
        )
        if len(records) > limits.max_direct_nodes:
            raise TraceQuotaExceeded(
                "Trace history Graph exceeds max_direct_nodes",
                context={"resource": "graph_direct_nodes"},
            )
    turns = trace_graph_turns(
        core_state,
        window,
        selected_turn_ids=set(window.visible_turns),
    )
    nodes, ordered_ids = project_trace_graph_records(
        records,
        turns=turns,
        run_turns=window.run_turns,
        selected_run_ids=window.selected_run_ids,
    )
    relationship_missing = relationship_evidence_missing or any(
        node.link_issues for node in nodes
    )
    details_omitted = any(
        node.content_omitted or node.request_omitted or node.result_omitted
        for node in nodes
    )
    return bound_graph(
        TraceGraph(
            turns=turns,
            nodes=nodes,
            ordered_node_ids=ordered_ids,
            matched_node_ids=ordered_ids,
            as_of_seq=as_of_seq,
            completeness=TraceGraphCompleteness(
                call_tracking_missing=not call_history_known,
                relationship_evidence_missing=relationship_missing,
                details_omitted=details_omitted,
            ),
        ),
        max_bytes=limits.max_page_bytes,
    )


def _validate_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    if value < 1 or value > 1000:
        raise ValueError("limit must be between 1 and 1000")


async def _projection_results(
    registry: Mapping[str, RegisteredTraceProjection],
    names: tuple[str, ...],
    *,
    store: TraceStore,
    key: TraceThreadKey,
    core_state: CoreProjectionState,
    head_run_id: str,
    run_ids: frozenset[str],
    as_of_seq: int,
) -> dict[str, BaseModel]:
    """Finish requested business Projections from Run-scoped incremental cache."""

    results: dict[str, BaseModel] = {}
    for name in names:
        projection = registry.get(name)
        if projection is None:
            raise TraceProjectionFailed(
                "Unknown Trace Projection",
                context={"projection": name},
            )
        state = await load_projection_state(
            projection,
            store=store,
            key=key,
            core_state=core_state,
            head_run_id=head_run_id,
            run_ids=run_ids,
            as_of_seq=as_of_seq,
        )
        results[name] = finish_projection(projection, state)
    return results


async def load_projection_state(
    projection: RegisteredTraceProjection,
    *,
    store: TraceStore,
    key: TraceThreadKey,
    core_state: CoreProjectionState,
    head_run_id: str,
    run_ids: frozenset[str],
    as_of_seq: int,
) -> BaseModel:
    """Load, increment, and CAS one Run-scoped custom Projection state."""

    while True:
        checkpoint: TraceProjectionCheckpoint | None = None
        checkpoint_run_id: str | None = head_run_id
        visited: set[str] = set()
        while checkpoint_run_id is not None and checkpoint_run_id not in visited:
            visited.add(checkpoint_run_id)
            checkpoint = await store.load_projection_checkpoint(
                key,
                projection_name=projection.name,
                run_id=checkpoint_run_id,
                as_of_seq=as_of_seq,
            )
            if checkpoint is not None:
                break
            run = core_state.runs.get(checkpoint_run_id)
            checkpoint_run_id = None if run is None else run.parent_run_id
        if checkpoint is None:
            facts = await _read_lineage_facts(
                store,
                key,
                run_ids=run_ids,
                as_of_seq=as_of_seq,
            )
            state = advance_projection_state(
                projection,
                projection.initial_state(),
                facts,
            )
            expected_seq: int | None = None
        else:
            try:
                state = projection.state_type.model_validate_json(
                    json.dumps(
                        checkpoint.state,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                )
            except (TypeError, ValueError, ValidationError) as error:
                raise TraceStoreProtocolError(
                    "Trace Store returned an invalid Projection checkpoint",
                    context={"projection": projection.name},
                    cause=error,
                ) from error
            tail = await _read_events_for_runs(
                store,
                key,
                run_ids=run_ids,
                after_seq=checkpoint.as_of_seq,
                as_of_seq=as_of_seq,
            )
            state = advance_projection_state(
                projection,
                state,
                tuple(event.fact for event in tail),
            )
            expected_seq = (
                checkpoint.as_of_seq if checkpoint.run_id == head_run_id else None
            )
        checkpoint_seq = 0 if checkpoint is None else checkpoint.as_of_seq
        if (
            checkpoint is not None
            and checkpoint_seq == as_of_seq
            and checkpoint.run_id == head_run_id
        ):
            return state
        candidate = TraceProjectionCheckpoint(
            key=key,
            projection_name=projection.name,
            run_id=head_run_id,
            as_of_seq=as_of_seq,
            state=cast(
                JsonValue,
                state.model_dump(mode="json", by_alias=True),
            ),
        )
        try:
            await store.save_projection_checkpoint(
                candidate,
                expected_as_of_seq=expected_seq,
            )
        except TraceProjectionCheckpointConflict as error:
            current = error.context.get("current_as_of_seq")
            if isinstance(current, int) and current > as_of_seq:
                return state
            continue
        return state


async def _read_selected_events(
    store: TraceStore,
    key: TraceThreadKey,
    *,
    selected_run_ids: frozenset[str],
    after_seq: int,
    as_of_seq: int,
    limit: int,
) -> tuple[tuple[TraceEvent, ...], int]:
    """Read a bounded lineage page while advancing through one fixed prefix.

    The Store cursor advances across unselected sibling facts so pagination cannot loop
    on them. Every batch must be contiguous and remain at or below ``as_of_seq``;
    an empty batch before that boundary is a persistent gap, not end-of-history.
    """

    selected: list[TraceEvent] = []
    cursor = after_seq
    read_limit = max(limit, store.limits.follow_batch_size)
    while cursor < as_of_seq and len(selected) < limit:
        batch = await store.read_events(
            key,
            after_seq=cursor,
            as_of_seq=as_of_seq,
            limit=read_limit,
        )
        if not batch:
            raise TraceStoreProtocolError(
                "Trace Store returned a gap inside a fixed event prefix"
            )
        _validate_event_batch(batch, key=key, expected_after=cursor)
        for event in batch:
            if event.trace_seq > as_of_seq:
                raise TraceStoreProtocolError(
                    "Trace Store returned an event beyond the requested prefix"
                )
            cursor = event.trace_seq
            if event.fact.identity.run_id in selected_run_ids:
                selected.append(event)
                if len(selected) == limit:
                    break
    return tuple(selected), cursor


def _validate_event_batch(
    batch: tuple[TraceEvent, ...],
    *,
    key: TraceThreadKey,
    expected_after: int,
) -> None:
    if not isinstance(batch, tuple):
        raise TraceStoreProtocolError("Trace Store returned an invalid event batch")
    expected = expected_after + 1
    for event in batch:
        if (
            not isinstance(event, TraceEvent)
            or event.generation != key.generation
            or event.trace_seq != expected
        ):
            raise TraceStoreProtocolError(
                "Trace Store returned non-contiguous generation events"
            )
        expected += 1


def _entity_delta(
    previous: tuple[EntityT, ...],
    current: tuple[EntityT, ...],
) -> TraceEntityDelta[EntityT]:
    previous_by_id = {_entity_id(item): item for item in previous}
    current_by_id = {_entity_id(item): item for item in current}
    upserts = tuple(
        item
        for item_id, item in current_by_id.items()
        if previous_by_id.get(item_id) != item
    )
    removes = tuple(sorted(set(previous_by_id) - set(current_by_id)))
    return TraceEntityDelta(upserts=upserts, removes=removes)


def _entity_id(item: BaseModel) -> str:
    dumped = cast(Mapping[str, object], item.model_dump(mode="python"))
    value = dumped.get("id")
    if not isinstance(value, str):
        raise TypeError("Trace update entity must expose a string ID")
    return value


def _encode_cursor(payload: _CursorPayload) -> str:
    encoded = payload.model_dump_json(by_alias=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(value: str) -> _CursorPayload:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            (value + padding).encode(),
            altchars=b"-_",
            validate=True,
        )
        return _CursorPayload.model_validate_json(decoded, strict=True)
    except (ValueError, TypeError, ValidationError):
        raise InvalidTraceCursor("Trace cursor is invalid") from None


def _encode_history_cursor(payload: _HistoryCursorPayload) -> str:
    encoded = payload.model_dump_json(by_alias=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_history_cursor(value: str) -> _HistoryCursorPayload:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            (value + padding).encode(),
            altchars=b"-_",
            validate=True,
        )
        return _HistoryCursorPayload.model_validate_json(decoded, strict=True)
    except (ValueError, TypeError, ValidationError):
        raise InvalidTraceCursor("Trace history cursor is invalid") from None


__all__ = ["TraceThread", "build_trace_thread", "resolve_history_request"]
