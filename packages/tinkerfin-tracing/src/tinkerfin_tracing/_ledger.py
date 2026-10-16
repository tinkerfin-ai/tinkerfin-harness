"""Single semantic reducer shared by every Trace Ledger backend."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from ._graph_reducer import graph_node_mutations
from .backend import (
    StoredTraceCheckpoint,
    StoredTraceEvent,
    TraceLedgerChange,
    TraceLedgerCommitResult,
    TraceLedgerState,
    TraceLedgerStorageEffect,
    TraceLedgerThreadState,
    TraceLedgerWriterState,
)
from .errors import (
    TraceProjectionCheckpointConflict,
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from .facts import RunFact, TraceEvent
from .store import TraceThreadKey, _event


def resolve_ledger_change(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    """Resolve one backend-independent Ledger transition from locked current state.

    Backends call this pure function after reading all state named by ``change`` at
    ``state.observed_at``. The returned effect contains only physical replacements,
    appends, removals, and namespace accounting deltas. A backend may call the function
    again after an optimistic-write conflict because it has no external side effects.

    Args:
        change: Framework-created change request with canonical payload evidence.
        state: Consistent storage-clock state for the affected namespace and thread.

    Returns:
        Complete atomic storage effect and public commit result.

    Raises:
        TraceStoreError: The requested transition violates current Ledger state.
    """

    _validate_change_scope(change, state)
    if change.kind == "open_writer":
        return _resolve_open_writer(change, state)
    if change.kind == "append_events":
        return _resolve_append_events(change, state)
    if change.kind == "renew_writer":
        return _resolve_renew_writer(change, state)
    if change.kind == "close_writer":
        return _resolve_close_writer(change, state)
    if change.kind == "save_projection_checkpoint":
        return _resolve_save_checkpoint(change, state)
    if change.kind == "delete_generation":
        return _resolve_delete_generation(change, state)
    raise TraceStoreProtocolError("Trace Ledger change kind is unsupported")


def _validate_change_scope(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> None:
    """Reject contradictory identities before any quota or persistence effect.

    A shared backend serves many namespaces. Every supplied identity must name the
    same thread, including idempotency evidence that bypasses a fresh append.
    Generation mismatches remain the individual transition's stale-key decision.
    """

    keys = tuple(
        key
        for key in (
            change.identity,
            change.key,
            None if change.checkpoint is None else change.checkpoint.key,
            None if state.thread is None else state.thread.key,
        )
        if key is not None
    )
    if (
        any(key.namespace != change.namespace for key in keys)
        or len({key.thread_id for key in keys}) > 1
    ):
        raise TraceStoreProtocolError("Trace Ledger thread identities conflict")
    if (
        change.checkpoint is not None
        and change.key is not None
        and change.checkpoint.key != change.key
    ):
        raise TraceStoreProtocolError("Trace checkpoint generation identities conflict")
    run_ids = {
        run_id
        for run_id in (
            change.run_id,
            None if change.identity is None else change.identity.run_id,
            None if change.checkpoint is None else change.checkpoint.run_id,
            None if state.target_writer is None else state.target_writer.run_id,
        )
        if run_id is not None
    }
    if len(run_ids) > 1:
        raise TraceStoreProtocolError("Trace Ledger Run identities conflict")
    facts = tuple(draft.fact for draft in change.facts) + tuple(
        event.fact for event in change.proven_events
    )
    if any(
        not keys
        or fact.identity.namespace != change.namespace
        or fact.identity.thread_id != keys[0].thread_id
        or fact.identity.run_id not in run_ids
        for fact in facts
    ):
        raise TraceStoreProtocolError(
            "Trace writer can append facts only for its bound Run"
        )
    if change.key is not None and any(
        event.generation != change.key.generation for event in change.proven_events
    ):
        raise TraceStoreProtocolError(
            "Proven Trace events belong to another generation"
        )


def _resolve_open_writer(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    identity = change.identity
    owner_token = change.owner_token
    if identity is None or owner_token is None:
        raise TraceStoreProtocolError("Writer open change is incomplete")

    thread = state.thread
    created = thread is None
    if thread is None:
        if state.namespace_thread_count >= change.limits.max_tracer_threads:
            raise TraceQuotaExceeded(
                "Tracer thread quota is exhausted",
                context={"resource": "tracer_threads"},
            )
        thread = TraceLedgerThreadState(
            key=TraceThreadKey(
                namespace=change.namespace,
                thread_id=identity.thread_id,
                generation=uuid4().hex,
            ),
            next_seq=1,
            persisted_bytes=0,
        )
    elif thread.key.namespace != change.namespace:
        raise TraceStoreProtocolError("Trace namespace state is inconsistent")

    existing = state.target_writer
    if existing is not None:
        if (
            existing.owner_token == owner_token
            and existing.active
            and not existing.closed_committed
        ):
            renewed = TraceLedgerWriterState(
                run_id=existing.run_id,
                owner_token=existing.owner_token,
                fence=existing.fence,
                lease_expires_at=_writer_lease_expiration(change, state),
                active=True,
                terminal_committed=existing.terminal_committed,
                closed_committed=existing.closed_committed,
                committed_events=existing.committed_events,
                remaining_event_reserve=existing.remaining_event_reserve,
                remaining_byte_reserve=existing.remaining_byte_reserve,
            )
            return TraceLedgerStorageEffect(
                result=TraceLedgerCommitResult(
                    kind="open_writer",
                    key=thread.key,
                    run_id=identity.run_id,
                    owner_token=owner_token,
                    fence=existing.fence,
                ),
                writer=renewed,
            )
        if not existing.active or existing.closed_committed:
            raise TraceRunConflict("Trace Run identity already exists")
        if (
            not change.enforce_writer_lease
            or existing.lease_expires_at > state.observed_at
        ):
            raise TraceRunConflict("Trace Run writer is still active")
        replacement = TraceLedgerWriterState(
            run_id=existing.run_id,
            owner_token=owner_token,
            fence=existing.fence + 1,
            lease_expires_at=_writer_lease_expiration(change, state),
            active=True,
            terminal_committed=existing.terminal_committed,
            closed_committed=existing.closed_committed,
            committed_events=existing.committed_events,
            remaining_event_reserve=existing.remaining_event_reserve,
            remaining_byte_reserve=existing.remaining_byte_reserve,
        )
        return TraceLedgerStorageEffect(
            result=TraceLedgerCommitResult(
                kind="open_writer",
                key=thread.key,
                run_id=identity.run_id,
                owner_token=owner_token,
                fence=replacement.fence,
            ),
            writer=replacement,
        )

    event_reserve = change.limits.terminal_reserve_events_per_run
    byte_reserve = change.limits.terminal_reserve_bytes_per_run
    if thread.next_seq - 1 + state.thread_reserved_events + event_reserve > (
        change.limits.max_thread_events
    ):
        raise TraceQuotaExceeded(
            "Thread cannot reserve terminal events",
            context={"resource": "thread_events"},
        )
    if thread.persisted_bytes + state.thread_reserved_bytes + byte_reserve > (
        change.limits.max_thread_bytes
    ):
        raise TraceQuotaExceeded(
            "Thread cannot reserve terminal bytes",
            context={"resource": "thread_bytes"},
        )
    if (
        state.namespace_persisted_bytes + state.namespace_reserved_bytes + byte_reserve
        > change.limits.max_tracer_bytes
    ):
        raise TraceQuotaExceeded(
            "Tracer cannot reserve terminal bytes",
            context={"resource": "tracer_bytes"},
        )

    writer = TraceLedgerWriterState(
        run_id=identity.run_id,
        owner_token=owner_token,
        fence=1,
        lease_expires_at=_writer_lease_expiration(change, state),
        active=True,
        terminal_committed=False,
        closed_committed=False,
        committed_events=0,
        remaining_event_reserve=event_reserve,
        remaining_byte_reserve=byte_reserve,
    )
    return TraceLedgerStorageEffect(
        result=TraceLedgerCommitResult(
            kind="open_writer",
            key=thread.key,
            run_id=identity.run_id,
            owner_token=owner_token,
            fence=writer.fence,
        ),
        thread=thread if created else None,
        writer=writer,
        namespace_thread_delta=1 if created else 0,
        namespace_reserved_bytes_delta=byte_reserve,
    )


def _resolve_append_events(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    if change.proven_events:
        if change.key is None:
            raise TraceStoreProtocolError("Proven append has no generation")
        thread = _require_exact_thread(change.key, state)
        writer = state.target_writer
        renewed = None
        if (
            writer is not None
            and writer.active
            and writer.owner_token == change.owner_token
            and writer.fence == change.fence
        ):
            renewed = TraceLedgerWriterState(
                run_id=writer.run_id,
                owner_token=writer.owner_token,
                fence=writer.fence,
                lease_expires_at=_writer_lease_expiration(change, state),
                active=True,
                terminal_committed=writer.terminal_committed,
                closed_committed=writer.closed_committed,
                committed_events=writer.committed_events,
                remaining_event_reserve=writer.remaining_event_reserve,
                remaining_byte_reserve=writer.remaining_byte_reserve,
            )
        return TraceLedgerStorageEffect(
            result=TraceLedgerCommitResult(
                kind="append_events",
                key=thread.key,
                run_id=change.run_id,
                events=change.proven_events,
            ),
            writer=renewed,
        )
    if not change.facts:
        raise TraceStoreProtocolError("Trace append requires at least one fact")
    thread, writer = _owned_writer(
        change,
        state,
        require_unexpired=change.enforce_writer_lease,
    )
    if not change.facts:
        return TraceLedgerStorageEffect(
            result=TraceLedgerCommitResult(
                kind="append_events",
                key=thread.key,
                run_id=writer.run_id,
            )
        )
    facts = tuple(item.fact for item in change.facts)
    terminal_batch = all(
        isinstance(fact, RunFact) and fact.phase in {"terminal", "closed"}
        for fact in facts
    )
    contains_terminal = any(
        isinstance(fact, RunFact) and fact.phase in {"terminal", "closed"}
        for fact in facts
    )
    if not change.mandatory and contains_terminal:
        raise TraceStoreProtocolError(
            "Terminal and closed Run facts require a mandatory append"
        )
    if change.mandatory != terminal_batch:
        raise TraceStoreProtocolError(
            "mandatory append is reserved for terminal and closed Run facts"
        )

    # Public model_copy/model_construct can bypass Pydantic construction checks.
    # Reject invalid lifecycle scope before producing any batch storage effect, so
    # writer flags, quotas, and Graph rows cannot commit a contradictory Run terminal.
    if any(
        isinstance(fact, RunFact)
        and (fact.graph_namespace != () or fact.parent_subagent_id is not None)
        for fact in facts
    ):
        raise TraceStoreProtocolError("Run lifecycle facts require the root scope")

    terminal_committed = writer.terminal_committed
    closed_committed = writer.closed_committed
    if change.mandatory:
        phases = tuple(fact.phase for fact in facts if isinstance(fact, RunFact))
        if phases.count("terminal") > 1 or phases.count("closed") > 1:
            raise TraceStoreProtocolError(
                "Run terminal and closed facts must be exactly-once"
            )
        if terminal_committed and "terminal" in phases:
            raise TraceStoreProtocolError("Run terminal fact is already committed")
        if closed_committed:
            raise TraceStoreProtocolError("Run closed fact is already committed")
        if "closed" in phases and not (
            terminal_committed
            or (
                "terminal" in phases
                and phases.index("terminal") < phases.index("closed")
            )
        ):
            raise TraceStoreProtocolError(
                "Run closed fact requires a committed terminal fact"
            )
        terminal_committed = terminal_committed or "terminal" in phases
        closed_committed = "closed" in phases
    elif terminal_committed or closed_committed:
        raise TraceStoreProtocolError(
            "Ordinary Trace facts cannot follow the Run terminal"
        )

    public_events: list[TraceEvent] = []
    stored_events: list[StoredTraceEvent] = []
    next_seq = thread.next_seq
    for draft in change.facts:
        fact = draft.fact
        if (
            fact.identity.namespace != thread.key.namespace
            or fact.identity.thread_id != thread.key.thread_id
            or fact.identity.run_id != writer.run_id
        ):
            raise TraceStoreProtocolError(
                "Trace writer can append facts only for its bound Run"
            )
        event = _event(
            event_id=draft.event_id,
            trace_seq=next_seq,
            generation=thread.key.generation,
            fact=fact,
            copy_fact=False,
        )
        if event.persisted_bytes > change.limits.max_event_bytes:
            raise TraceQuotaExceeded(
                "Trace event exceeds max_event_bytes",
                context={"resource": "event_bytes"},
            )
        public_events.append(event)
        if change.persist_canonical_event_records:
            stored_events.append(
                StoredTraceEvent(
                    event_id=event.event_id,
                    trace_seq=event.trace_seq,
                    run_id=writer.run_id,
                    fact_kind=fact.kind,
                    occurred_at=fact.occurred_at,
                    canonical_payload=draft.canonical_payload,
                    payload_digest=draft.payload_digest,
                    persisted_bytes=event.persisted_bytes,
                )
            )
        next_seq += 1

    added_bytes = sum(event.persisted_bytes for event in public_events)
    if change.mandatory:
        if (
            len(public_events) > writer.remaining_event_reserve
            or added_bytes > writer.remaining_byte_reserve
        ):
            raise TraceQuotaExceeded(
                "Run terminal reserve is exhausted",
                context={"resource": "terminal_reserve"},
            )
    else:
        if next_seq - 1 + state.thread_reserved_events > (
            change.limits.max_thread_events
        ):
            raise TraceQuotaExceeded(
                "Thread event quota is exhausted",
                context={"resource": "thread_events"},
            )
        if thread.persisted_bytes + added_bytes + state.thread_reserved_bytes > (
            change.limits.max_thread_bytes
        ):
            raise TraceQuotaExceeded(
                "Thread byte quota is exhausted",
                context={"resource": "thread_bytes"},
            )
        if (
            state.namespace_persisted_bytes
            + added_bytes
            + state.namespace_reserved_bytes
            > change.limits.max_tracer_bytes
        ):
            raise TraceQuotaExceeded(
                "Tracer byte quota is exhausted",
                context={"resource": "tracer_bytes"},
            )

    remaining_event_reserve = writer.remaining_event_reserve
    remaining_byte_reserve = writer.remaining_byte_reserve
    reserve_delta = 0
    if change.mandatory:
        previous_reserve = remaining_byte_reserve
        if closed_committed:
            remaining_event_reserve = 0
            remaining_byte_reserve = 0
        else:
            remaining_event_reserve -= len(public_events)
            remaining_byte_reserve -= added_bytes
        reserve_delta = remaining_byte_reserve - previous_reserve

    updated_writer = TraceLedgerWriterState(
        run_id=writer.run_id,
        owner_token=writer.owner_token,
        fence=writer.fence,
        lease_expires_at=_writer_lease_expiration(change, state),
        active=True,
        terminal_committed=terminal_committed,
        closed_committed=closed_committed,
        committed_events=writer.committed_events + len(public_events),
        remaining_event_reserve=remaining_event_reserve,
        remaining_byte_reserve=remaining_byte_reserve,
    )
    updated_thread = TraceLedgerThreadState(
        key=thread.key,
        next_seq=next_seq,
        persisted_bytes=thread.persisted_bytes + added_bytes,
    )
    copied_events = tuple(event.model_copy(deep=True) for event in public_events)
    return TraceLedgerStorageEffect(
        result=TraceLedgerCommitResult(
            kind="append_events",
            key=thread.key,
            run_id=writer.run_id,
            events=copied_events,
        ),
        thread=updated_thread,
        writer=updated_writer,
        events=tuple(stored_events),
        validated_events=tuple(public_events),
        graph_node_mutations=graph_node_mutations(tuple(public_events)),
        namespace_persisted_bytes_delta=added_bytes,
        namespace_reserved_bytes_delta=reserve_delta,
    )


def _resolve_renew_writer(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    thread, writer = _owned_writer(
        change,
        state,
        require_unexpired=(change.enforce_writer_lease and not change.proven_renewal),
    )
    renewed = TraceLedgerWriterState(
        run_id=writer.run_id,
        owner_token=writer.owner_token,
        fence=writer.fence,
        lease_expires_at=_writer_lease_expiration(change, state),
        active=writer.active,
        terminal_committed=writer.terminal_committed,
        closed_committed=writer.closed_committed,
        committed_events=writer.committed_events,
        remaining_event_reserve=writer.remaining_event_reserve,
        remaining_byte_reserve=writer.remaining_byte_reserve,
    )
    return TraceLedgerStorageEffect(
        result=TraceLedgerCommitResult(
            kind="renew_writer",
            key=thread.key,
            run_id=writer.run_id,
        ),
        writer=renewed,
    )


def _resolve_close_writer(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    key = change.key
    run_id = change.run_id
    if key is None or run_id is None:
        raise TraceStoreProtocolError("Writer close change is incomplete")
    thread = state.thread
    writer = state.target_writer
    result = TraceLedgerCommitResult(
        kind="close_writer",
        key=key,
        run_id=run_id,
    )
    if thread is None or thread.key != key or writer is None:
        return TraceLedgerStorageEffect(result=result)
    if (
        writer.owner_token != change.owner_token
        or writer.fence != change.fence
        or not writer.active
    ):
        return TraceLedgerStorageEffect(result=result)

    reserve_delta = -writer.remaining_byte_reserve
    if writer.committed_events == 0:
        remove_thread = thread.next_seq == 1 and state.writer_count == 1
        return TraceLedgerStorageEffect(
            result=result,
            remove_thread=remove_thread,
            remove_writer_run_id=run_id,
            namespace_thread_delta=-1 if remove_thread else 0,
            namespace_reserved_bytes_delta=reserve_delta,
        )
    closed = TraceLedgerWriterState(
        run_id=writer.run_id,
        owner_token=writer.owner_token,
        fence=writer.fence,
        lease_expires_at=writer.lease_expires_at,
        active=False,
        terminal_committed=writer.terminal_committed,
        closed_committed=writer.closed_committed,
        committed_events=writer.committed_events,
        remaining_event_reserve=0,
        remaining_byte_reserve=0,
    )
    return TraceLedgerStorageEffect(
        result=result,
        writer=closed,
        namespace_reserved_bytes_delta=reserve_delta,
    )


def _resolve_save_checkpoint(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    checkpoint = change.checkpoint
    payload = change.canonical_checkpoint_state
    digest = change.checkpoint_state_digest
    if checkpoint is None or payload is None or digest is None:
        raise TraceStoreProtocolError("Projection checkpoint change is incomplete")
    thread = _require_exact_thread(checkpoint.key, state)
    if checkpoint.as_of_seq > thread.next_seq - 1:
        raise TraceStoreProtocolError(
            "Projection checkpoint exceeds the committed Trace prefix"
        )
    current = state.current_checkpoint
    if current is not None and current.as_of_seq == checkpoint.as_of_seq:
        if current.state_digest != digest or current.canonical_state != payload:
            raise TraceStoreProtocolError(
                "Projection checkpoint idempotency evidence conflicts"
            )
        return TraceLedgerStorageEffect(
            result=TraceLedgerCommitResult(
                kind="save_projection_checkpoint",
                key=checkpoint.key,
                checkpoint=checkpoint.model_copy(deep=True),
            )
        )
    current_seq = None if current is None else current.as_of_seq
    if current_seq != change.expected_checkpoint_as_of_seq:
        raise TraceProjectionCheckpointConflict(
            "Projection checkpoint compare-and-swap failed",
            context={
                "projection": checkpoint.projection_name,
                "current_as_of_seq": current_seq,
            },
        )
    if current is not None and checkpoint.as_of_seq <= current.as_of_seq:
        raise TraceProjectionCheckpointConflict(
            "Projection checkpoint must advance its fixed prefix",
            context={
                "projection": checkpoint.projection_name,
                "current_as_of_seq": current.as_of_seq,
            },
        )
    stored = StoredTraceCheckpoint(
        key=checkpoint.key,
        projection_name=checkpoint.projection_name,
        run_id=checkpoint.run_id,
        as_of_seq=checkpoint.as_of_seq,
        canonical_state=payload,
        state_digest=digest,
    )
    return TraceLedgerStorageEffect(
        result=TraceLedgerCommitResult(
            kind="save_projection_checkpoint",
            key=checkpoint.key,
            checkpoint=checkpoint.model_copy(deep=True),
        ),
        checkpoint=stored,
    )


def _resolve_delete_generation(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> TraceLedgerStorageEffect:
    key = change.key
    if key is None:
        raise TraceStoreProtocolError("Generation delete change is incomplete")
    thread = _require_exact_thread(key, state)
    if state.active_writers:
        raise TraceRunConflict(
            "Active Trace writers prevent deletion",
            context={"active_run_count": len(state.active_writers)},
        )
    return TraceLedgerStorageEffect(
        result=TraceLedgerCommitResult(kind="delete_generation", key=key),
        delete_generation=True,
        namespace_thread_delta=-1,
        namespace_persisted_bytes_delta=-thread.persisted_bytes,
        namespace_reserved_bytes_delta=-state.thread_reserved_bytes,
    )


def _owned_writer(
    change: TraceLedgerChange,
    state: TraceLedgerState,
    *,
    require_unexpired: bool,
) -> tuple[TraceLedgerThreadState, TraceLedgerWriterState]:
    key = change.key
    run_id = change.run_id
    if key is None or run_id is None:
        raise TraceStoreProtocolError("Owned writer change is incomplete")
    thread = _require_exact_thread(key, state)
    writer = state.target_writer
    if writer is None or writer.run_id != run_id:
        raise TraceStoreProtocolError("Trace writer no longer owns its Run")
    if (
        not writer.active
        or writer.owner_token != change.owner_token
        or writer.fence != change.fence
        or (require_unexpired and writer.lease_expires_at <= state.observed_at)
    ):
        raise TraceStoreProtocolError("Trace writer lease ownership was lost")
    return thread, writer


def _require_exact_thread(
    key: TraceThreadKey,
    state: TraceLedgerState,
) -> TraceLedgerThreadState:
    thread = state.thread
    if thread is None or thread.key != key:
        raise TraceThreadNotFound(
            "Trace generation does not exist",
            context={"thread_id": key.thread_id},
        )
    return thread


def _writer_lease_expiration(
    change: TraceLedgerChange,
    state: TraceLedgerState,
) -> datetime:
    if not change.enforce_writer_lease:
        return datetime.max.replace(tzinfo=state.observed_at.tzinfo)
    return state.observed_at + timedelta(seconds=change.options.writer_lease_seconds)


__all__ = ["resolve_ledger_change"]
