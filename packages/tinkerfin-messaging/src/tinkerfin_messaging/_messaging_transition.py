"""Pure Messaging lifecycle transitions shared by storage-oriented backends."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity, validate_request_digest
from .backend import is_final_run_status
from .backend_contract import (
    MessagingBackendSettings,
    MessagingRunReference,
    MessagingStateSnapshot,
    MessagingStorageEffect,
    MessagingTransition,
    MessagingTransitionResult,
    StoredMessageEvidence,
    StoredMessagingChannel,
    StoredMessagingRun,
    StoredMessagingStream,
)
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    CodecMismatch,
    InvalidCursor,
    MessageIdConflict,
    MessagingBackendProtocolError,
    MessagingQuotaExceeded,
    PublicationRejected,
    RunAlreadyActive,
    RunNotFound,
    RunRequestConflict,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
)
from .models import MessageEnvelope, RecoveryCheckpoint


def messaging_message_signature(
    *,
    identity: RunIdentity,
    codec_id: str,
    payload: bytes,
    checkpoint: RecoveryCheckpoint | None,
) -> str:
    """Return the stable current digest for one idempotent message commit.

    The digest uses the canonical byte domain shared by built-in Backends and persisted
    idempotency evidence while remaining storage-neutral at the extension boundary.

    Args:
        identity: Exact thread and semantic run identity for the message.
        codec_id: Stable codec identity persisted with the payload.
        payload: Encoded message bytes.
        checkpoint: Optional recovery position committed with the message.

    Returns:
        Lowercase hexadecimal SHA-256 digest of the complete commit evidence.

    Raises:
        TypeError: Payload or checkpoint has the wrong type.
        ValueError: An identifier is not canonical.
    """

    required_identity(identity)
    required_identifier("codec_id", codec_id)
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if checkpoint is not None and not isinstance(checkpoint, RecoveryCheckpoint):
        raise TypeError("checkpoint must be a RecoveryCheckpoint or None")
    digest = hashlib.sha256()
    digest.update(b"tinkerfin-messaging:redis-message\0")
    values = [identity.run_id.encode(), codec_id.encode(), payload]
    if checkpoint is not None:
        values.extend(
            [
                checkpoint.position,
                (checkpoint.last_message_id or "").encode(),
            ]
        )
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    digest.update(b"1" if checkpoint is not None else b"0")
    return digest.hexdigest()


def resolve_messaging_transition(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    """Resolve one Messaging transition from a consistent bounded state snapshot.

    The function performs no I/O and has no external side effects. Transactional
    backends call it while holding their serialization boundary, then atomically apply
    the returned replacements. A backend may call it again after a proven optimistic
    conflict because the same transition and state always produce the same effect.

    Args:
        transition: Complete framework-created lifecycle intent.
        state: Storage-clock-consistent evidence required by the transition.

    Returns:
        Complete atomic storage effect and observable transition result.

    Raises:
        MessagingError: Durable state rejects the requested lifecycle transition.
        TypeError: Transition or state values have the wrong public type.
        ValueError: Transition identifiers or cursor values are invalid.
    """

    if not isinstance(transition, MessagingTransition):
        raise TypeError("transition must be a MessagingTransition")
    if not isinstance(state, MessagingStateSnapshot):
        raise TypeError("state must be a MessagingStateSnapshot")
    _validate_transition_identity(transition)
    if transition.kind == "prepare_run":
        return _resolve_prepare_run(transition, state)
    if transition.kind in {"append_message", "publish_message"}:
        return _resolve_append_message(transition, state)
    if transition.kind == "begin_settlement":
        return _resolve_begin_settlement(transition, state)
    if transition.kind == "finish_run":
        return _resolve_finish_run(transition, state)
    if transition.kind == "request_cancellation":
        return _resolve_request_cancellation(transition, state)
    if transition.kind == "renew_producer_ownership":
        return _resolve_renew_producer_ownership(transition, state)
    if transition.kind == "reconcile_producer_ownership":
        return _resolve_reconcile_producer_ownership(transition, state)
    if transition.kind == "begin_generation_cleanup":
        return _resolve_begin_generation_cleanup(transition, state)
    if transition.kind == "finish_generation_cleanup":
        return _resolve_finish_generation_cleanup(transition, state)
    raise MessagingBackendProtocolError(
        "Messaging transition kind is unsupported",
        diagnostic_context={"transition_kind": transition.kind},
    )


def _validate_transition_identity(transition: MessagingTransition) -> None:
    required_identifier("transition_id", transition.transition_id)
    required_identifier("channel", transition.channel)
    required_identity(transition.identity)
    if not isinstance(transition.settings, MessagingBackendSettings):
        raise TypeError("transition.settings must be a MessagingBackendSettings")


def _resolve_prepare_run(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    codec_id = _required_text("codec_id", transition.codec_id)
    validate_request_digest(transition.request_digest)
    after_sequence = transition.after_sequence
    if after_sequence is not None and (
        isinstance(after_sequence, bool) or not isinstance(after_sequence, int)
    ):
        raise TypeError("after_sequence must be an integer or None")

    stored_channel = state.channel
    channel_effect = None
    if stored_channel is None:
        channel_effect = StoredMessagingChannel(
            channel=transition.channel,
            codec_id=codec_id,
            limits=transition.settings.limits,
            retention_policy=transition.settings.retention_policy,
        )
    else:
        if stored_channel.codec_id != codec_id:
            raise CodecMismatch(expected=stored_channel.codec_id, actual=codec_id)
        if stored_channel.limits != transition.settings.limits:
            raise MessagingBackendProtocolError(
                "Messaging channel uses different capacity limits"
            )
        if stored_channel.retention_policy != transition.settings.retention_policy:
            raise MessagingBackendProtocolError(
                "Messaging channel uses a different retention policy"
            )

    stream = state.stream
    if stream is not None and stream.disposition in {"deleting", "expiring"}:
        _raise_unavailable_stream(transition, stream)
    if stream is None or stream.disposition in {"deleted", "expired"}:
        previous_generation = (
            state.tombstone_generation
            if state.tombstone_generation is not None
            else (None if stream is None else stream.generation)
        )
        unavailable_reason = (
            state.tombstone_reason
            if state.tombstone_reason is not None
            else (None if stream is None else stream.disposition)
        )
        if unavailable_reason == "expired" and after_sequence not in {None, 0}:
            raise StreamExpired(
                channel=transition.channel,
                identity=transition.identity,
                generation=previous_generation or 1,
            )
        stream = StoredMessagingStream(
            channel=transition.channel,
            thread_id=transition.identity.thread_id,
            generation=1 if previous_generation is None else previous_generation + 1,
            disposition="active",
            latest_sequence=0,
            payload_bytes=0,
            next_producer_fence=1,
            active_run_id=None,
            retention_expired=False,
            message_sequence=0,
            control_sequence=0,
        )

    latest_sequence = stream.latest_sequence
    cursor = latest_sequence if after_sequence is None else after_sequence
    if cursor < 0 or cursor > latest_sequence:
        raise InvalidCursor(after=cursor, latest=latest_sequence)

    target_run = state.target_run
    run_effects: list[StoredMessagingRun] = []
    if target_run is not None:
        if target_run.generation != stream.generation:
            raise MessagingBackendProtocolError(
                "Messaging run generation conflicts with its stream"
            )
        if target_run.request_digest != transition.request_digest:
            raise RunRequestConflict(identity=transition.identity)
        if is_final_run_status(target_run.status) or target_run.producer_lease_active:
            return _prepared_attachment(
                transition,
                stream,
                cursor=cursor,
                channel_effect=channel_effect,
            )
        if target_run.status == "cancel_requested" and target_run.settlement_started:
            lost = _owner_lost_run(
                target_run,
                end_sequence=latest_sequence,
                message="producer lease expired during cancellation settlement",
            )
            return MessagingStorageEffect(
                result=_attachment_result(transition, stream, cursor),
                channel=channel_effect,
                stream=replace(
                    stream,
                    active_run_id=None,
                    control_sequence=stream.control_sequence + 1,
                ),
                runs=(lost,),
                lease_action="release",
                lease_run_id=target_run.identity.run_id,
                retention_action="start",
            )
        if target_run.recoverable and transition.recoverable:
            fence = stream.next_producer_fence
            producer_token = transition.transition_id
            recovered = replace(
                target_run,
                status=(
                    "cancel_requested"
                    if target_run.status == "cancel_requested"
                    else "running"
                ),
                settlement_started=False,
                cancellable=transition.cancellable,
                producer_token=producer_token,
                producer_fence=fence,
                producer_lease_active=True,
                failure_class="",
                failure_message="",
                local_failure=None,
            )
            recovered_stream = replace(
                stream,
                next_producer_fence=fence + 1,
                active_run_id=transition.identity.run_id,
                retention_expired=False,
                control_sequence=stream.control_sequence + 1,
            )
            return MessagingStorageEffect(
                result=MessagingTransitionResult(
                    kind=transition.kind,
                    run_reference=MessagingRunReference(
                        channel=transition.channel,
                        identity=transition.identity,
                        generation=stream.generation,
                        producer_token=producer_token,
                        producer_fence=fence,
                    ),
                    after_sequence=cursor,
                    is_producer_owner=True,
                    checkpoint=target_run.checkpoint,
                    recovered=True,
                    run_status=recovered.status,
                ),
                channel=channel_effect,
                stream=recovered_stream,
                runs=(recovered,),
                lease_action="acquire",
                lease_run_id=transition.identity.run_id,
                retention_action="clear",
            )
        lost = _owner_lost_run(
            target_run,
            end_sequence=latest_sequence,
            message="producer lease expired",
        )
        return MessagingStorageEffect(
            result=_attachment_result(transition, stream, cursor),
            channel=channel_effect,
            stream=replace(
                stream,
                active_run_id=None,
                control_sequence=stream.control_sequence + 1,
            ),
            runs=(lost,),
            lease_action="release",
            lease_run_id=transition.identity.run_id,
            retention_action="start",
        )

    active_run = state.active_run
    if active_run is not None:
        if active_run.producer_lease_active:
            raise RunAlreadyActive(
                active_identity=active_run.identity,
                requested_identity=transition.identity,
            )
        run_effects.append(
            _owner_lost_run(
                active_run,
                end_sequence=latest_sequence,
                message="producer lease expired",
            )
        )

    fence = stream.next_producer_fence
    producer_token = transition.transition_id
    created_run = StoredMessagingRun(
        identity=transition.identity,
        request_digest=transition.request_digest,
        generation=stream.generation,
        start_sequence=latest_sequence,
        end_sequence=latest_sequence,
        status="running",
        settlement_started=False,
        cancellable=transition.cancellable,
        recoverable=transition.recoverable,
        producer_token=producer_token,
        producer_fence=fence,
        producer_lease_active=True,
        checkpoint=None,
        failure_class="",
        failure_message="",
    )
    run_effects.append(created_run)
    prepared_stream = replace(
        stream,
        next_producer_fence=fence + 1,
        active_run_id=transition.identity.run_id,
        retention_expired=False,
        control_sequence=(
            stream.control_sequence + (1 if active_run is not None else 0)
        ),
    )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            run_reference=MessagingRunReference(
                channel=transition.channel,
                identity=transition.identity,
                generation=stream.generation,
                producer_token=producer_token,
                producer_fence=fence,
            ),
            after_sequence=cursor,
            is_producer_owner=True,
            recovered=False,
            run_status="running",
        ),
        channel=channel_effect,
        stream=prepared_stream,
        runs=tuple(run_effects),
        lease_action="acquire",
        lease_run_id=transition.identity.run_id,
        retention_action="clear",
    )


def _resolve_append_message(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    external = transition.kind == "publish_message"
    if external:
        run = _required_target_run(transition, state)
        stream = _required_stream(transition, state)
        reference = transition.run_reference
        if reference is None or reference.generation != stream.generation:
            raise PublicationRejected(
                identity=transition.identity, reason="generation_changed"
            )
        if transition.checkpoint is not None or transition.closes_publication:
            raise ValueError(
                "External publication cannot change source recovery or lifecycle"
            )
    else:
        run, stream, reference = _owned_run(transition, state)
    codec_id = _required_text("codec_id", transition.codec_id)
    message_id = _required_text("message_id", transition.message_id)
    payload = transition.payload
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    checkpoint = transition.checkpoint
    if checkpoint is not None and not isinstance(checkpoint, RecoveryCheckpoint):
        raise TypeError("checkpoint must be a RecoveryCheckpoint or None")
    if checkpoint is not None and checkpoint.last_message_id != message_id:
        raise ValueError("checkpoint.last_message_id must match message_id")
    limits = transition.settings.limits
    if len(payload) > limits.max_message_payload_bytes:
        raise MessagingQuotaExceeded(
            resource="message_payload_bytes",
            limit=limits.max_message_payload_bytes,
        )
    if (
        checkpoint is not None
        and len(checkpoint.position) > limits.max_checkpoint_bytes
    ):
        raise MessagingQuotaExceeded(
            resource="checkpoint_bytes",
            limit=limits.max_checkpoint_bytes,
        )
    channel_state = state.channel
    if channel_state is None:
        raise MessagingBackendProtocolError("Messaging channel state is missing")
    if channel_state.codec_id != codec_id:
        raise CodecMismatch(expected=channel_state.codec_id, actual=codec_id)
    signature = messaging_message_signature(
        identity=transition.identity,
        codec_id=codec_id,
        payload=payload,
        checkpoint=checkpoint,
    )
    existing = state.matching_message
    if existing is not None:
        if existing.signature != signature:
            raise MessageIdConflict(
                identity=transition.identity,
                message_id=message_id,
            )
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                envelope=existing.envelope.model_copy(deep=True),
                run_status=run.status,
            )
        )
    # Deduplicated retries return their original envelope even after settlement.
    # New publications race terminal commits under the backend serialization boundary.
    if external:
        if run.status != "running" or run.settlement_started or run.publication_closed:
            raise PublicationRejected(identity=transition.identity, reason="run_closed")
        if not run.producer_lease_active:
            raise PublicationRejected(identity=transition.identity, reason="owner_lost")
        if not run.publication_ready:
            raise PublicationRejected(
                identity=transition.identity, reason="run_not_ready"
            )
    if stream.latest_sequence >= limits.max_thread_messages:
        raise MessagingQuotaExceeded(
            resource="thread_messages",
            limit=limits.max_thread_messages,
        )
    if stream.payload_bytes + len(payload) > limits.max_thread_payload_bytes:
        raise MessagingQuotaExceeded(
            resource="thread_payload_bytes",
            limit=limits.max_thread_payload_bytes,
        )
    next_sequence = stream.latest_sequence + 1
    envelope = MessageEnvelope(
        channel=transition.channel,
        identity=transition.identity,
        seq=next_sequence,
        message_id=message_id,
        codec=codec_id,
        payload=bytes(payload),
        created_at=state.observed_at,
    )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            envelope=envelope.model_copy(deep=True),
            run_status=run.status,
        ),
        stream=replace(
            stream,
            latest_sequence=next_sequence,
            payload_bytes=stream.payload_bytes + len(payload),
            message_sequence=next_sequence,
        ),
        runs=(
            replace(
                run,
                end_sequence=next_sequence,
                publication_ready=run.publication_ready
                or (not external and transition.opens_publication),
                publication_closed=run.publication_closed
                or transition.closes_publication,
                checkpoint=run.checkpoint if checkpoint is None else checkpoint,
            ),
        ),
        message=StoredMessageEvidence(
            envelope=envelope,
            signature=signature,
            checkpoint=checkpoint,
        ),
        lease_run_id=None if external else reference.identity.run_id,
    )


def _resolve_begin_settlement(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    run, _stream, _reference = _owned_run(transition, state)
    if run.settlement_started:
        raise BackendOwnershipLost(
            f"Producer for run {transition.identity.run_id!r} cannot begin settlement"
        )
    if run.status not in {"running", "cancel_requested"}:
        raise BackendOwnershipLost(
            f"Producer for run {transition.identity.run_id!r} cannot begin settlement"
        )
    cancellation_preceded = run.status == "cancel_requested"
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            cancellation_preceded_settlement=cancellation_preceded,
            run_status=run.status,
        ),
        runs=(replace(run, settlement_started=True),),
    )


def _resolve_finish_run(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    run, stream, _reference = _owned_run(transition, state)
    final_status = transition.final_status
    if final_status not in {"completed", "cancelled", "failed", "owner_lost"}:
        raise TypeError("final_status must be a terminal run status")
    failure = transition.failure
    if run.status == "cancel_requested" and final_status == "completed":
        final_status = "failed"
        failure = RuntimeError(
            "producer completed without settling accepted cancellation"
        )
    failure_class, failure_message = _failure_evidence(failure)
    finished = replace(
        run,
        end_sequence=stream.latest_sequence,
        status=final_status,
        settlement_started=True,
        producer_token=None,
        producer_lease_active=False,
        failure_class=failure_class,
        failure_message=failure_message,
        local_failure=failure,
    )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            run_status=final_status,
        ),
        stream=replace(
            stream,
            active_run_id=(
                None
                if stream.active_run_id == transition.identity.run_id
                else stream.active_run_id
            ),
            control_sequence=stream.control_sequence + 1,
        ),
        runs=(finished,),
        lease_action="release",
        lease_run_id=transition.identity.run_id,
        retention_action="start",
    )


def _resolve_request_cancellation(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    run = _required_target_run(transition, state)
    stream = _required_stream(transition, state)
    if is_final_run_status(run.status) or run.settlement_started:
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cancellation_requested_by_transition=False,
                run_status=run.status,
            )
        )
    if not run.cancellable:
        raise CancellationUnsupported(identity=transition.identity)
    if run.status == "cancel_requested":
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cancellation_requested_by_transition=False,
                run_status=run.status,
            )
        )
    if run.status != "running":
        raise MessagingBackendProtocolError(
            "Messaging run has an invalid active cancellation status"
        )
    cancelled = replace(run, status="cancel_requested")
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            cancellation_requested_by_transition=True,
            run_status="cancel_requested",
        ),
        stream=replace(
            stream,
            control_sequence=stream.control_sequence + 1,
        ),
        runs=(cancelled,),
    )


def _resolve_renew_producer_ownership(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    reference = _required_reference(transition)
    run = state.target_run
    if run is None:
        _raise_reference_tombstone(transition, state, reference.generation)
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                producer_ownership_confirmed=False,
            )
        )
    if not _reference_matches_run(reference, run):
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                producer_ownership_confirmed=False,
            )
        )
    if not run.producer_lease_active or is_final_run_status(run.status):
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                producer_ownership_confirmed=False,
                run_status=run.status,
            )
        )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            producer_ownership_confirmed=True,
            run_status=run.status,
        ),
        lease_action="renew",
        lease_run_id=transition.identity.run_id,
    )


def _resolve_reconcile_producer_ownership(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    run = _required_target_run(transition, state)
    stream = _required_stream(transition, state)
    reference = transition.run_reference
    if reference is not None:
        if (
            reference.channel != transition.channel
            or reference.identity != transition.identity
        ):
            raise ValueError("run_reference conflicts with the transition identity")
        if (
            reference.generation != stream.generation
            or reference.generation != run.generation
        ):
            _raise_reference_tombstone(
                transition,
                state,
                reference.generation,
            )
            raise MessagingBackendProtocolError(
                "Messaging ownership snapshot does not match the requested generation"
            )
    if is_final_run_status(run.status) or run.producer_lease_active:
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                run_status=run.status,
            )
        )
    message = (
        "producer lease expired during cancellation settlement"
        if run.status == "cancel_requested" and run.settlement_started
        else "producer lease expired"
    )
    lost = _owner_lost_run(
        run,
        end_sequence=stream.latest_sequence,
        message=message,
    )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            run_status="owner_lost",
        ),
        stream=replace(
            stream,
            active_run_id=(
                None
                if stream.active_run_id == transition.identity.run_id
                else stream.active_run_id
            ),
            control_sequence=stream.control_sequence + 1,
        ),
        runs=(lost,),
        lease_action="release",
        lease_run_id=transition.identity.run_id,
        retention_action="start",
    )


def _resolve_begin_generation_cleanup(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    reason = transition.cleanup_reason
    if reason not in {"deleted", "expired"}:
        raise TypeError("cleanup_reason must be deleted or expired")
    stream = state.stream
    if stream is None:
        tombstone_generation = state.tombstone_generation
        tombstone_reason = state.tombstone_reason
        if (tombstone_generation is None) != (tombstone_reason is None):
            raise MessagingBackendProtocolError(
                "Messaging cleanup tombstone evidence is incomplete"
            )
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cleanup_generation=tombstone_generation,
                cleanup_reason=tombstone_reason,
                cleanup_required=False,
            )
        )
    if stream.disposition in {"deleted", "expired"}:
        finalized_reason = "deleted" if stream.disposition == "deleted" else "expired"
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cleanup_generation=stream.generation,
                cleanup_reason=finalized_reason,
                cleanup_required=False,
            )
        )
    if stream.disposition in {"deleting", "expiring"}:
        sealed_reason = "deleted" if stream.disposition == "deleting" else "expired"
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cleanup_generation=stream.generation,
                cleanup_reason=sealed_reason,
                cleanup_required=True,
            )
        )
    lost: StoredMessagingRun | None = None
    if stream.active_run_id is not None:
        owner = state.target_run
        if owner is None or owner.identity.run_id != stream.active_run_id:
            owner = state.active_run
        active_identity = (
            owner.identity
            if owner is not None
            else RunIdentity(
                namespace=transition.identity.namespace,
                thread_id=transition.identity.thread_id,
                run_id=stream.active_run_id,
            )
        )
        if owner is None or owner.producer_lease_active:
            raise StreamDeleteConflict(
                channel=transition.channel,
                identity=transition.identity,
                active_identity=active_identity,
            )
        # Deletion fences expired producers even when their source is recoverable.
        # No caller-side status read is required; owner loss and sealing commit
        # together. See the real Redis expired-producer deletion contract.
        lost = _owner_lost_run(
            owner,
            end_sequence=stream.latest_sequence,
            message="producer lease expired",
        )
    if reason == "expired" and not stream.retention_expired:
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cleanup_required=False,
            )
        )
    disposition = "deleting" if reason == "deleted" else "expiring"
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            cleanup_generation=stream.generation,
            cleanup_reason=reason,
            cleanup_required=True,
        ),
        stream=replace(
            stream,
            disposition=disposition,
            active_run_id=None,
            control_sequence=stream.control_sequence + 1,
        ),
        runs=() if lost is None else (lost,),
        lease_action="none" if lost is None else "release",
        lease_run_id=None if lost is None else lost.identity.run_id,
        retention_action="clear",
    )


def _resolve_finish_generation_cleanup(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> MessagingStorageEffect:
    reason = transition.cleanup_reason
    if reason not in {"deleted", "expired"}:
        raise TypeError("cleanup_reason must be deleted or expired")
    generation = transition.cleanup_generation
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError("cleanup_generation must be an integer")
    if generation < 1:
        raise ValueError("cleanup_generation must be positive")
    stream = state.stream
    if stream is None:
        if (
            state.tombstone_generation == generation
            and state.tombstone_reason == reason
        ):
            return MessagingStorageEffect(
                result=MessagingTransitionResult(
                    kind=transition.kind,
                    cleanup_generation=generation,
                    cleanup_reason=reason,
                    cleanup_required=False,
                )
            )
        if state.tombstone_generation == generation:
            raise MessagingBackendProtocolError(
                "Messaging generation was finalized for a different cleanup reason"
            )
        raise MessagingBackendProtocolError(
            "Messaging generation cleanup state is missing"
        )
    if stream.generation != generation:
        raise MessagingBackendProtocolError(
            "Messaging cleanup snapshot does not match the requested generation"
        )
    expected = "deleting" if reason == "deleted" else "expiring"
    if stream.disposition == reason:
        return MessagingStorageEffect(
            result=MessagingTransitionResult(
                kind=transition.kind,
                cleanup_generation=generation,
                cleanup_reason=reason,
                cleanup_required=False,
            )
        )
    if stream.disposition != expected:
        raise MessagingBackendProtocolError(
            "Messaging generation is not sealed for the requested cleanup"
        )
    final_stream = replace(
        stream,
        disposition=reason,
        active_run_id=None,
        control_sequence=stream.control_sequence + 1,
    )
    return MessagingStorageEffect(
        result=MessagingTransitionResult(
            kind=transition.kind,
            cleanup_generation=generation,
            cleanup_reason=reason,
            cleanup_required=False,
        ),
        stream=final_stream,
        tombstone_reason=reason,
    )


def _prepared_attachment(
    transition: MessagingTransition,
    stream: StoredMessagingStream,
    *,
    cursor: int,
    channel_effect: StoredMessagingChannel | None,
) -> MessagingStorageEffect:
    return MessagingStorageEffect(
        result=_attachment_result(transition, stream, cursor),
        channel=channel_effect,
    )


def _attachment_result(
    transition: MessagingTransition,
    stream: StoredMessagingStream,
    cursor: int,
) -> MessagingTransitionResult:
    return MessagingTransitionResult(
        kind=transition.kind,
        run_reference=MessagingRunReference(
            channel=transition.channel,
            identity=transition.identity,
            generation=stream.generation,
            producer_token=None,
            producer_fence=None,
        ),
        after_sequence=cursor,
        is_producer_owner=False,
        recovered=False,
    )


def _owned_run(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> tuple[StoredMessagingRun, StoredMessagingStream, MessagingRunReference]:
    reference = _required_reference(transition)
    run = _required_target_run(transition, state)
    stream = _required_stream(transition, state)
    if stream.generation != reference.generation:
        _raise_reference_tombstone(transition, state, reference.generation)
        raise BackendOwnershipLost(
            f"Producer for run {transition.identity.run_id!r} lost its generation"
        )
    if not _reference_matches_run(reference, run) or not run.producer_lease_active:
        raise BackendOwnershipLost(
            f"Producer for run {transition.identity.run_id!r} lost its ownership fence"
        )
    if is_final_run_status(run.status):
        raise BackendOwnershipLost(
            f"Producer for run {transition.identity.run_id!r} is already terminal"
        )
    return run, stream, reference


def _required_reference(transition: MessagingTransition) -> MessagingRunReference:
    reference = transition.run_reference
    if not isinstance(reference, MessagingRunReference):
        raise TypeError("run_reference must be a MessagingRunReference")
    if (
        reference.channel != transition.channel
        or reference.identity != transition.identity
    ):
        raise ValueError("run_reference conflicts with the transition identity")
    if reference.producer_token is None or reference.producer_fence is None:
        raise BackendOwnershipLost(
            f"Run {transition.identity.run_id!r} has no producer ownership evidence"
        )
    return reference


def _required_target_run(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> StoredMessagingRun:
    run = state.target_run
    if run is None:
        _raise_reference_tombstone(
            transition,
            state,
            None
            if transition.run_reference is None
            else transition.run_reference.generation,
        )
        raise RunNotFound(identity=transition.identity)
    return run


def _required_stream(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
) -> StoredMessagingStream:
    stream = state.stream
    if stream is None:
        _raise_reference_tombstone(
            transition,
            state,
            None
            if transition.run_reference is None
            else transition.run_reference.generation,
        )
        raise RunNotFound(identity=transition.identity)
    if stream.disposition != "active":
        _raise_unavailable_stream(transition, stream)
    return stream


def _reference_matches_run(
    reference: MessagingRunReference,
    run: StoredMessagingRun,
) -> bool:
    return (
        run.generation == reference.generation
        and run.identity == reference.identity
        and run.producer_token == reference.producer_token
        and run.producer_fence == reference.producer_fence
    )


def _owner_lost_run(
    run: StoredMessagingRun,
    *,
    end_sequence: int,
    message: str,
) -> StoredMessagingRun:
    failure = RuntimeError(message)
    return replace(
        run,
        end_sequence=end_sequence,
        status="owner_lost",
        settlement_started=True,
        producer_token=None,
        producer_lease_active=False,
        failure_class="tinkerfin_messaging.OwnerLost",
        failure_message=message,
        local_failure=failure,
    )


def _failure_evidence(error: BaseException | None) -> tuple[str, str]:
    if error is None:
        return "", ""
    error_type = type(error)
    return f"{error_type.__module__}.{error_type.__qualname__}", str(error)


def _required_text(name: str, value: str | None) -> str:
    if value is None:
        raise TypeError(f"{name} must be a string")
    return required_identifier(name, value)


def _raise_reference_tombstone(
    transition: MessagingTransition,
    state: MessagingStateSnapshot,
    generation: int | None,
) -> None:
    tombstone_generation = state.tombstone_generation
    if tombstone_generation is None:
        return
    if generation is not None and generation != tombstone_generation:
        return
    if state.tombstone_reason == "expired":
        raise StreamExpired(
            channel=transition.channel,
            identity=transition.identity,
            generation=tombstone_generation,
        )
    if state.tombstone_reason == "deleted":
        raise StreamDeleted(
            channel=transition.channel,
            identity=transition.identity,
            generation=tombstone_generation,
        )


def _raise_unavailable_stream(
    transition: MessagingTransition,
    stream: StoredMessagingStream,
) -> None:
    if stream.disposition in {"expired", "expiring"}:
        raise StreamExpired(
            channel=transition.channel,
            identity=transition.identity,
            generation=stream.generation,
        )
    raise StreamDeleted(
        channel=transition.channel,
        identity=transition.identity,
        generation=stream.generation,
    )


__all__ = ["messaging_message_signature", "resolve_messaging_transition"]
