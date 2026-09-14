"""Storage-neutral Messaging transition contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging._messaging_ledger import _MessagingLedger
from tinkerfin_messaging._messaging_transition import messaging_message_signature
from tinkerfin_messaging.backend import MemoryBackend, RunStatus
from tinkerfin_messaging.backend_contract import (
    CommittedMessagePage,
    CommittedMessageQuery,
    MessagingBackend,
    MessagingBackendSettings,
    MessagingChangeWait,
    MessagingCleanupReason,
    MessagingRunReference,
    MessagingStateQuery,
    MessagingStateSnapshot,
    MessagingStreamDisposition,
    MessagingTransition,
    MessagingTransitionResult,
    StoredMessageEvidence,
    StoredMessagingChannel,
    StoredMessagingRun,
    StoredMessagingStream,
    resolve_messaging_transition,
)
from tinkerfin_messaging.errors import (
    MessageIdConflict,
    MessagingBackendProtocolError,
    MessagingQuotaExceeded,
    RunAlreadyActive,
    RunProducerFailed,
    StreamDeleteConflict,
    StreamDeleted,
)
from tinkerfin_messaging.limits import MessagingLimits
from tinkerfin_messaging.models import MessageEnvelope, RecoveryCheckpoint
from tinkerfin_messaging.retention import MessagingRetentionPolicy
from tinkerfin_messaging.testing import verify_messaging_backend


def _identity(run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)


def _settings(*, max_messages: int = 100) -> MessagingBackendSettings:
    return MessagingBackendSettings(
        limits=MessagingLimits(
            max_message_payload_bytes=1024,
            max_checkpoint_bytes=1024,
            max_thread_messages=max_messages,
            max_thread_payload_bytes=4096,
        ),
        retention_policy=MessagingRetentionPolicy.disabled(),
        producer_renew_interval_seconds=None,
        producer_lease_seconds=None,
        change_wait_timeout_seconds=0.1,
    )


def _stream(
    *,
    generation: int = 1,
    disposition: MessagingStreamDisposition = "active",
    active_run_id: str | None = None,
    latest_sequence: int = 0,
    payload_bytes: int = 0,
    retention_expired: bool = False,
) -> StoredMessagingStream:
    return StoredMessagingStream(
        channel="events",
        thread_id="thread-1",
        generation=generation,
        disposition=disposition,
        latest_sequence=latest_sequence,
        payload_bytes=payload_bytes,
        next_producer_fence=1,
        active_run_id=active_run_id,
        retention_expired=retention_expired,
        message_sequence=latest_sequence,
        control_sequence=0,
    )


def _run(
    *,
    identity: RunIdentity | None = None,
    status: RunStatus = "running",
    producer_token: str | None = "owner-1",
    producer_fence: int = 1,
    producer_lease_active: bool = True,
    cancellable: bool = True,
    recoverable: bool = False,
) -> StoredMessagingRun:
    return StoredMessagingRun(
        identity=identity or _identity(),
        generation=1,
        start_sequence=0,
        end_sequence=0,
        status=status,
        settlement_started=False,
        cancellable=cancellable,
        recoverable=recoverable,
        producer_token=producer_token,
        producer_fence=producer_fence,
        producer_lease_active=producer_lease_active,
        checkpoint=None,
        failure_class="",
        failure_message="",
    )


def _channel(settings: MessagingBackendSettings) -> StoredMessagingChannel:
    return StoredMessagingChannel(
        channel="events",
        codec_id="tests.bytes",
        limits=settings.limits,
        retention_policy=settings.retention_policy,
    )


def _state(
    *,
    settings: MessagingBackendSettings | None = None,
    stream: StoredMessagingStream | None = None,
    target_run: StoredMessagingRun | None = None,
    active_run: StoredMessagingRun | None = None,
    matching_message: StoredMessageEvidence | None = None,
    tombstone_generation: int | None = None,
    tombstone_reason: MessagingCleanupReason | None = None,
) -> MessagingStateSnapshot:
    resolved_settings = settings or _settings()
    return MessagingStateSnapshot(
        observed_at=datetime(2026, 8, 31, 4, 0, tzinfo=UTC),
        channel=None if stream is None else _channel(resolved_settings),
        stream=stream,
        target_run=target_run,
        active_run=active_run,
        matching_message=matching_message,
        tombstone_generation=tombstone_generation,
        tombstone_reason=tombstone_reason,
    )


def _prepare_transition(
    *,
    identity: RunIdentity | None = None,
    settings: MessagingBackendSettings | None = None,
    recoverable: bool = False,
) -> MessagingTransition:
    return MessagingTransition(
        kind="prepare_run",
        transition_id="owner-new",
        channel="events",
        identity=identity or _identity(),
        settings=settings or _settings(),
        codec_id="tests.bytes",
        after_sequence=0,
        cancellable=True,
        recoverable=recoverable,
    )


def _reference() -> MessagingRunReference:
    return MessagingRunReference(
        channel="events",
        identity=_identity(),
        generation=1,
        producer_token="owner-1",
        producer_fence=1,
    )


class _ExpiredLeaseObservationBackend(MemoryBackend):
    """Expose an expired lease without mutating state during reads or waits."""

    def __init__(self) -> None:
        super().__init__()
        self.expiry_visible = False
        self.reconciled = False
        self.reconciled_generations: list[int | None] = []
        self.wait_calls = 0

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind != "reconcile_producer_ownership":
            return await super().commit_messaging_transition(transition)
        reference = transition.run_reference
        self.reconciled_generations.append(
            None if reference is None else reference.generation
        )
        self.reconciled = True
        return MessagingTransitionResult(
            kind=transition.kind,
            run_status="owner_lost",
        )

    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        snapshot = await super().load_messaging_state(query)
        run = snapshot.target_run
        if not self.expiry_visible or run is None:
            return snapshot
        return replace(snapshot, target_run=self._observed_run(run))

    async def read_committed_messages(
        self,
        query: CommittedMessageQuery,
    ) -> CommittedMessagePage:
        page = await super().read_committed_messages(query)
        run = page.run_state
        if not self.expiry_visible or run is None:
            return page
        return replace(page, run_state=self._observed_run(run))

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        del wait
        self.wait_calls += 1
        raise AssertionError("expired ownership must be reconciled before waiting")

    def _observed_run(self, run: StoredMessagingRun) -> StoredMessagingRun:
        if not self.reconciled:
            return replace(run, producer_lease_active=False)
        return replace(
            run,
            status="owner_lost",
            settlement_started=True,
            producer_token=None,
            producer_lease_active=False,
            failure_class="tinkerfin_messaging.OwnerLost",
            failure_message="producer lease expired",
            local_failure=RuntimeError("producer lease expired"),
        )


def test_backend_settings_require_complete_ordered_lease_durations() -> None:
    assert MemoryBackend().messaging_settings.change_wait_timeout_seconds is None
    with pytest.raises(ValueError, match="both be set or disabled"):
        replace(_settings(), producer_lease_seconds=15)
    with pytest.raises(ValueError, match="shorter"):
        replace(
            _settings(),
            producer_renew_interval_seconds=15,
            producer_lease_seconds=15,
        )


def test_message_signature_preserves_current_redis_evidence() -> None:
    assert (
        messaging_message_signature(
            identity=RunIdentity(
                namespace="test", thread_id="conversation-1", run_id="run-1"
            ),
            codec_id="text",
            payload=b"payload",
            checkpoint=None,
        )
        == "c42f38656e51cdde504a1f0bf4b07b7ce060380dfcffe3a9e20b1131fb9e0866"
    )


def test_prepare_creates_one_owner_with_channel_and_stream_effects() -> None:
    effect = resolve_messaging_transition(_prepare_transition(), _state())

    assert effect.channel is not None
    assert effect.stream is not None
    assert effect.stream.generation == 1
    assert effect.stream.active_run_id == "run-1"
    assert effect.lease_action == "acquire"
    assert effect.result.is_producer_owner is True
    assert effect.result.run_reference is not None
    assert effect.result.run_reference.producer_token == "owner-new"
    assert effect.runs[0].status == "running"


def test_prepare_attaches_to_an_existing_live_run() -> None:
    settings = _settings()
    existing = _run()
    effect = resolve_messaging_transition(
        _prepare_transition(settings=settings),
        _state(
            settings=settings,
            stream=_stream(active_run_id="run-1"),
            target_run=existing,
        ),
    )

    assert effect.result.is_producer_owner is False
    assert effect.result.run_reference is not None
    assert effect.result.run_reference.producer_token is None
    assert effect.runs == ()


def test_prepare_recovers_one_expired_recoverable_owner() -> None:
    settings = _settings()
    checkpoint = RecoveryCheckpoint(position=b"42", last_message_id="message-1")
    existing = replace(
        _run(recoverable=True, producer_lease_active=False),
        checkpoint=checkpoint,
    )
    effect = resolve_messaging_transition(
        _prepare_transition(settings=settings, recoverable=True),
        _state(
            settings=settings,
            stream=_stream(active_run_id="run-1"),
            target_run=existing,
        ),
    )

    assert effect.result.is_producer_owner is True
    assert effect.result.recovered is True
    assert effect.result.checkpoint == checkpoint
    assert effect.lease_action == "acquire"
    assert effect.runs[0].producer_fence == 1


def test_prepare_rejects_a_different_live_owner() -> None:
    settings = _settings()
    active = _run(identity=_identity("run-active"))
    with pytest.raises(RunAlreadyActive):
        resolve_messaging_transition(
            _prepare_transition(identity=_identity("run-new"), settings=settings),
            _state(
                settings=settings,
                stream=_stream(active_run_id="run-active"),
                active_run=active,
            ),
        )


def test_append_allocates_sequence_and_commits_checkpoint() -> None:
    settings = _settings()
    run = _run()
    checkpoint = RecoveryCheckpoint(position=b"1", last_message_id="message-1")
    effect = resolve_messaging_transition(
        MessagingTransition(
            kind="append_message",
            transition_id="append-1",
            channel="events",
            identity=_identity(),
            settings=settings,
            run_reference=_reference(),
            codec_id="tests.bytes",
            message_id="message-1",
            payload=b"value",
            checkpoint=checkpoint,
        ),
        _state(
            settings=settings,
            stream=_stream(active_run_id="run-1"),
            target_run=run,
        ),
    )

    assert effect.message is not None
    assert effect.message.envelope.seq == 1
    assert effect.message.checkpoint == checkpoint
    assert effect.stream is not None
    assert effect.stream.payload_bytes == 5
    assert effect.runs[0].checkpoint == checkpoint


def test_append_is_idempotent_and_content_sensitive() -> None:
    settings = _settings()
    run = _run()
    envelope = MessageEnvelope(
        channel="events",
        identity=_identity(),
        seq=1,
        message_id="message-1",
        codec="tests.bytes",
        payload=b"value",
        created_at=datetime(2026, 8, 31, 4, 0, tzinfo=UTC),
    )
    evidence = StoredMessageEvidence(
        envelope=envelope,
        signature=messaging_message_signature(
            identity=_identity(),
            codec_id="tests.bytes",
            payload=b"value",
            checkpoint=None,
        ),
        checkpoint=None,
    )
    transition = MessagingTransition(
        kind="append_message",
        transition_id="append-1",
        channel="events",
        identity=_identity(),
        settings=settings,
        run_reference=_reference(),
        codec_id="tests.bytes",
        message_id="message-1",
        payload=b"value",
    )
    state = _state(
        settings=settings,
        stream=_stream(active_run_id="run-1", latest_sequence=1),
        target_run=run,
        matching_message=evidence,
    )

    effect = resolve_messaging_transition(transition, state)
    assert effect.result.envelope == envelope
    assert effect.message is None

    with pytest.raises(MessageIdConflict):
        resolve_messaging_transition(replace(transition, payload=b"different"), state)


def test_append_enforces_thread_message_quota() -> None:
    settings = _settings(max_messages=1)
    with pytest.raises(MessagingQuotaExceeded, match="thread_messages"):
        resolve_messaging_transition(
            MessagingTransition(
                kind="append_message",
                transition_id="append-2",
                channel="events",
                identity=_identity(),
                settings=settings,
                run_reference=_reference(),
                codec_id="tests.bytes",
                message_id="message-2",
                payload=b"value",
            ),
            _state(
                settings=settings,
                stream=_stream(active_run_id="run-1", latest_sequence=1),
                target_run=_run(),
            ),
        )


def test_cancellation_settlement_and_finish_preserve_terminal_semantics() -> None:
    settings = _settings()
    stream = _stream(active_run_id="run-1")
    run = _run()
    cancel = MessagingTransition(
        kind="request_cancellation",
        transition_id="cancel-1",
        channel="events",
        identity=_identity(),
        settings=settings,
    )
    cancelled = resolve_messaging_transition(
        cancel,
        _state(settings=settings, stream=stream, target_run=run),
    )
    assert cancelled.result.cancellation_requested_by_transition is True
    cancelled_run = cancelled.runs[0]

    settlement = resolve_messaging_transition(
        MessagingTransition(
            kind="begin_settlement",
            transition_id="settle-1",
            channel="events",
            identity=_identity(),
            settings=settings,
            run_reference=_reference(),
        ),
        _state(settings=settings, stream=stream, target_run=cancelled_run),
    )
    assert settlement.result.cancellation_preceded_settlement is True

    finished = resolve_messaging_transition(
        MessagingTransition(
            kind="finish_run",
            transition_id="finish-1",
            channel="events",
            identity=_identity(),
            settings=settings,
            run_reference=_reference(),
            final_status="cancelled",
        ),
        _state(settings=settings, stream=stream, target_run=settlement.runs[0]),
    )
    assert finished.result.run_status == "cancelled"
    assert finished.stream is not None
    assert finished.stream.active_run_id is None
    assert finished.retention_action == "start"


def test_reconcile_marks_an_expired_owner_lost() -> None:
    settings = _settings()
    effect = resolve_messaging_transition(
        MessagingTransition(
            kind="reconcile_producer_ownership",
            transition_id="reconcile-1",
            channel="events",
            identity=_identity(),
            settings=settings,
        ),
        _state(
            settings=settings,
            stream=_stream(active_run_id="run-1"),
            target_run=_run(producer_lease_active=False),
        ),
    )

    assert effect.result.run_status == "owner_lost"
    assert effect.runs[0].status == "owner_lost"
    assert effect.retention_action == "start"


def test_reconcile_rejects_a_snapshot_from_another_generation() -> None:
    settings = _settings()
    transition = MessagingTransition(
        kind="reconcile_producer_ownership",
        transition_id="reconcile-stale-generation",
        channel="events",
        identity=_identity(),
        settings=settings,
        run_reference=_reference(),
    )

    with pytest.raises(MessagingBackendProtocolError, match="requested generation"):
        resolve_messaging_transition(
            transition,
            _state(
                settings=settings,
                stream=_stream(generation=2, active_run_id="run-1"),
                target_run=replace(_run(producer_lease_active=False), generation=2),
            ),
        )


@pytest.mark.parametrize(
    "observation",
    ["follow", "wait_for_cancel", "wait_finished"],
)
async def test_passive_observers_reconcile_an_expired_backend_lease(
    observation: str,
) -> None:
    backend = _ExpiredLeaseObservationBackend()
    ledger = _MessagingLedger(backend)
    prepared = await ledger.prepare(
        channel="events",
        identity=_identity(),
        codec="tests.bytes",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    backend.expiry_visible = True

    if observation == "follow":
        with pytest.raises(RunProducerFailed):
            await anext(ledger.follow(prepared.handle, after=0))
    elif observation == "wait_for_cancel":
        assert await ledger.wait_for_cancel(prepared.handle) is False
    else:
        assert await ledger.wait_finished(prepared.handle) == "owner_lost"

    assert backend.reconciled_generations == [prepared.handle.generation]
    assert backend.wait_calls == 0


def test_generation_cleanup_requires_an_inactive_stream() -> None:
    settings = _settings()
    transition = MessagingTransition(
        kind="begin_generation_cleanup",
        transition_id="delete-1",
        channel="events",
        identity=_identity(),
        settings=settings,
        cleanup_reason="deleted",
    )
    with pytest.raises(StreamDeleteConflict):
        resolve_messaging_transition(
            transition,
            _state(
                settings=settings,
                stream=_stream(active_run_id="run-1"),
                active_run=_run(),
            ),
        )

    sealed = resolve_messaging_transition(
        transition,
        _state(settings=settings, stream=_stream()),
    )
    assert sealed.result.cleanup_required is True
    assert sealed.result.cleanup_generation == 1
    assert sealed.result.cleanup_reason == "deleted"
    assert sealed.stream is not None
    assert sealed.stream.disposition == "deleting"

    completed = resolve_messaging_transition(
        replace(
            transition,
            kind="finish_generation_cleanup",
            transition_id="delete-finish-1",
            cleanup_generation=1,
        ),
        _state(settings=settings, stream=sealed.stream),
    )
    assert completed.stream is not None
    assert completed.stream.disposition == "deleted"
    assert completed.tombstone_reason == "deleted"


@pytest.mark.parametrize("same_run", [True, False])
@pytest.mark.parametrize("recoverable", [True, False])
def test_generation_cleanup_fences_an_expired_producer(
    same_run: bool, recoverable: bool
) -> None:
    owner = _run(
        identity=_identity("run-1" if same_run else "other-run"),
        producer_lease_active=False,
        recoverable=recoverable,
    )
    effect = resolve_messaging_transition(
        MessagingTransition(
            kind="begin_generation_cleanup",
            transition_id="delete-expired-owner",
            channel="events",
            identity=_identity(),
            settings=_settings(),
            cleanup_reason="deleted",
        ),
        _state(
            stream=_stream(active_run_id=owner.identity.run_id),
            target_run=owner if same_run else None,
            active_run=None if same_run else owner,
        ),
    )
    assert effect.result.cleanup_required is True
    assert effect.stream is not None
    assert effect.stream.disposition == "deleting"
    assert effect.stream.active_run_id is None
    assert len(effect.runs) == 1
    assert effect.runs[0].identity == owner.identity
    assert effect.runs[0].status == "owner_lost"
    assert effect.runs[0].producer_token is None
    assert effect.lease_action == "release"
    assert effect.lease_run_id == owner.identity.run_id


def test_generation_cleanup_keeps_the_first_sealed_reason() -> None:
    settings = _settings()
    effect = resolve_messaging_transition(
        MessagingTransition(
            kind="begin_generation_cleanup",
            transition_id="delete-after-expiry-seal",
            channel="events",
            identity=_identity(),
            settings=settings,
            cleanup_reason="deleted",
        ),
        _state(
            settings=settings,
            stream=_stream(disposition="expiring", retention_expired=True),
        ),
    )

    assert effect.stream is None
    assert effect.result.cleanup_required is True
    assert effect.result.cleanup_generation == 1
    assert effect.result.cleanup_reason == "expired"


def test_generation_cleanup_finish_is_generation_bound_and_idempotent() -> None:
    settings = _settings()
    finish = MessagingTransition(
        kind="finish_generation_cleanup",
        transition_id="delete-finish-repeat",
        channel="events",
        identity=_identity(),
        settings=settings,
        cleanup_generation=1,
        cleanup_reason="deleted",
    )
    finalized = _state(
        settings=settings,
        tombstone_generation=1,
        tombstone_reason="deleted",
    )

    repeated = resolve_messaging_transition(finish, finalized)
    assert repeated.stream is None
    assert repeated.tombstone_reason is None
    assert repeated.result.cleanup_generation == 1
    assert repeated.result.cleanup_reason == "deleted"

    with pytest.raises(MessagingBackendProtocolError, match="different cleanup reason"):
        resolve_messaging_transition(
            replace(finish, cleanup_reason="expired"),
            finalized,
        )
    with pytest.raises(MessagingBackendProtocolError, match="requested generation"):
        resolve_messaging_transition(
            finish,
            _state(settings=settings, stream=_stream(generation=2)),
        )


async def test_memory_backend_implements_the_storage_oriented_contract() -> None:
    backend = MemoryBackend()
    settings = backend.messaging_settings

    assert isinstance(backend, MessagingBackend)
    await backend.prepare_messaging_storage()
    prepared = await backend.commit_messaging_transition(
        _prepare_transition(settings=settings)
    )
    assert prepared.run_reference is not None
    committed = await backend.commit_messaging_transition(
        MessagingTransition(
            kind="append_message",
            transition_id="append-memory-1",
            channel="events",
            identity=_identity(),
            settings=settings,
            run_reference=prepared.run_reference,
            codec_id="tests.bytes",
            message_id="message-1",
            payload=b"value",
        )
    )
    assert committed.envelope is not None
    assert committed.envelope.seq == 1

    page = await backend.read_committed_messages(
        CommittedMessageQuery(
            channel="events",
            identity=_identity(),
            generation=prepared.run_reference.generation,
            after_sequence=0,
            through_sequence=None,
            limit=10,
        )
    )
    assert [message.message_id for message in page.messages] == ["message-1"]


@pytest.mark.parametrize("timeout_seconds", [None, 1.0])
async def test_memory_change_wait_preserves_cancellation_during_notification(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float | None,
) -> None:
    backend = MemoryBackend()
    settings = backend.messaging_settings
    prepared = await backend.commit_messaging_transition(
        _prepare_transition(settings=settings)
    )
    assert prepared.run_reference is not None
    query = CommittedMessageQuery(
        channel="events",
        identity=_identity(),
        generation=prepared.run_reference.generation,
        after_sequence=0,
        through_sequence=None,
        limit=10,
    )
    page = await backend.read_committed_messages(query)
    entered = asyncio.Event()
    condition_wait = asyncio.Condition.wait

    async def cancel_after_notification(condition: asyncio.Condition) -> bool:
        entered.set()
        result = await condition_wait(condition)
        pending.cancel()
        return result

    monkeypatch.setattr(asyncio.Condition, "wait", cancel_after_notification)
    pending = asyncio.create_task(
        backend.wait_for_messaging_change(
            MessagingChangeWait(
                channel="events",
                identity=_identity(),
                generation=prepared.run_reference.generation,
                after=page.change_cursor,
                timeout_seconds=timeout_seconds,
            )
        )
    )
    append = MessagingTransition(
        kind="append_message",
        transition_id="append-notification",
        channel="events",
        identity=_identity(),
        settings=settings,
        run_reference=prepared.run_reference,
        codec_id="tests.bytes",
        message_id="message-1",
        payload=b"first",
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await backend.commit_messaging_transition(append)
        done, _ = await asyncio.wait({pending}, timeout=1)
        assert pending in done
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with asyncio.timeout(1):
            await backend.commit_messaging_transition(
                replace(
                    append,
                    transition_id="append-after-cancellation",
                    message_id="message-2",
                    payload=b"second",
                )
            )
            page = await backend.read_committed_messages(query)
            assert [message.payload for message in page.messages] == [
                b"first",
                b"second",
            ]
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_framework_ledger_preserves_memory_run_and_replay_behavior() -> None:
    ledger = _MessagingLedger(MemoryBackend())
    await ledger.prepare_storage()
    prepared = await ledger.prepare(
        channel="events",
        identity=_identity(),
        codec="tests.bytes",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    assert prepared.is_owner is True
    first = await ledger.append(
        prepared.handle,
        message_id="message-1",
        codec="tests.bytes",
        payload=b"first",
    )
    assert first.seq == 1

    follower = ledger.follow(prepared.handle, after=0)
    assert (await anext(follower)).message_id == "message-1"
    assert await ledger.request_cancel(prepared.handle) is True
    assert await ledger.wait_for_cancel(prepared.handle) is True
    assert await ledger.begin_settlement(prepared.handle) is True
    await ledger.finish(prepared.handle, status="cancelled")
    with pytest.raises(StopAsyncIteration):
        await anext(follower)
    assert await ledger.wait_finished(prepared.handle) == "cancelled"

    await ledger.delete_stream(channel="events", identity=_identity())
    with pytest.raises(StreamDeleted):
        await ledger.failure(prepared.handle)


async def test_public_backend_verifier_accepts_memory_storage() -> None:
    @asynccontextmanager
    async def open_backend() -> AsyncIterator[MessagingBackend]:
        yield MemoryBackend()

    await verify_messaging_backend(open_backend)
