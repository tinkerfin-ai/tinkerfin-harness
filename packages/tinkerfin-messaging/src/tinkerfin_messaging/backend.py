"""Backend contract and in-memory committed-message implementation."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Never, TypeGuard

from tinkerfin_contracts import RunIdentity

from ._capacity import _ExpiryIndex, checkpoint_bytes
from ._identity import required_identifier, required_identity, thread_key
from .errors import (
    CodecMismatch,
    InvalidCursor,
    MessagingQuotaExceeded,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
)
from .limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint
from .retention import MessagingRetentionPolicy

if TYPE_CHECKING:
    from .backend_contract import (
        CommittedMessagePage,
        CommittedMessageQuery,
        MessagingBackendSettings,
        MessagingChangeWait,
        MessagingStateQuery,
        MessagingStateSnapshot,
        MessagingStorageEffect,
        MessagingTransition,
        MessagingTransitionResult,
        StoredMessagingRun,
        StreamGenerationPurge,
        StreamGenerationPurgeResult,
    )

RunStatus = Literal[
    "running",
    "cancel_requested",
    "completed",
    "cancelled",
    "failed",
    "owner_lost",
]
ActiveRunStatus = Literal["running", "cancel_requested"]
FinalRunStatus = Literal["completed", "cancelled", "failed", "owner_lost"]
FailedRunStatus = Literal["failed", "owner_lost"]
_ACTIVE_RUN_STATUSES = frozenset({"running", "cancel_requested"})
_FINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "failed", "owner_lost"})
_FAILED_RUN_STATUSES = frozenset({"failed", "owner_lost"})


def is_active_run_status(status: object) -> TypeGuard[ActiveRunStatus]:
    """Return whether a value names a non-terminal durable run state."""

    return isinstance(status, str) and status in _ACTIVE_RUN_STATUSES


def is_final_run_status(status: object) -> TypeGuard[FinalRunStatus]:
    """Return whether a value names an authoritative terminal run state."""

    return isinstance(status, str) and status in _FINAL_RUN_STATUSES


def is_failed_run_status(status: object) -> TypeGuard[FailedRunStatus]:
    """Return whether a terminal state represents failed producer delivery."""

    return isinstance(status, str) and status in _FAILED_RUN_STATUSES


class _RunRecord:
    def __init__(
        self,
        *,
        identity: RunIdentity,
        start_seq: int,
        owner_token: str,
        cancellable: bool,
        recoverable: bool,
        request_digest: str | None = None,
    ) -> None:
        self.identity = identity
        self.start_seq = start_seq
        self.end_seq = start_seq
        self.owner_token: str | None = owner_token
        self.fence = 1
        self.cancellable = cancellable
        self.recoverable = recoverable
        self.request_digest = request_digest
        self.status: RunStatus = "running"
        self.settling = False
        self.publication_closed = False
        self.publication_ready = False
        self.error: BaseException | None = None
        self.checkpoint: RecoveryCheckpoint | None = None


class _StreamState:
    """Own one in-memory generation, its active Run, and terminal deadline."""

    def __init__(self, *, generation: int) -> None:
        self.generation = generation
        self.disposition: Literal[
            "active",
            "deleting",
            "deleted",
            "expiring",
            "expired",
        ] = "active"
        self.deleted = False
        self.condition = asyncio.Condition()
        self.messages: list[MessageEnvelope] = []
        self.by_message_id: dict[str, MessageEnvelope] = {}
        self.signatures: dict[
            str, tuple[str, str, bytes, RecoveryCheckpoint | None]
        ] = {}
        self.runs: dict[str, _RunRecord] = {}
        self.active_identity: RunIdentity | None = None
        self.payload_bytes = 0
        self.retained_bytes = 0
        self.retained_records = 1
        self.expires_at_monotonic: float | None = None
        self.control_sequence = 0


class _ChannelState:
    """Coordinate thread generations and retain stale-handle dispositions."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.lock = asyncio.Lock()
        self.codec: str | None = None
        self.streams: dict[str, _StreamState] = {}
        self.next_generations: dict[str, int] = {}
        self.tombstones: dict[str, dict[int, Literal["deleted", "expired"]]] = {}


class MemoryBackend:
    """Keep ordered logs and run coordination in one event loop process."""

    def __init__(
        self,
        *,
        limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
        retention_policy: MessagingRetentionPolicy = MessagingRetentionPolicy(),
    ) -> None:
        """Initialize process-local streams with capacity and retention limits.

        Args:
            limits: Immutable individual, per-thread, and instance-wide capacity contract.
            retention_policy: Terminal replay deadline policy; disabled by default.

        Raises:
            TypeError: Either policy has the wrong public type.
        """

        if not isinstance(limits, MessagingLimits):
            raise TypeError("limits must be a MessagingLimits")
        if not isinstance(retention_policy, MessagingRetentionPolicy):
            raise TypeError("retention_policy must be a MessagingRetentionPolicy")
        self._channels: dict[str, _ChannelState] = {}
        self._total_bytes = 0
        self._total_records = 0
        self._expirations = _ExpiryIndex()
        self._limits = limits
        self._retention_policy = retention_policy

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        """Return immutable settings for the process-local Messaging store.

        Returns:
            Capacity and retention settings with external producer leases disabled.
        """

        from .backend_contract import MessagingBackendSettings

        return MessagingBackendSettings(
            limits=self._limits,
            retention_policy=self._retention_policy,
            producer_renew_interval_seconds=None,
            producer_lease_seconds=None,
            change_wait_timeout_seconds=None,
        )

    async def prepare_messaging_storage(self) -> None:
        """Confirm that process-local storage requires no external preparation.

        Returns:
            ``None`` because in-memory structures are created lazily.
        """

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        """Resolve and atomically apply one in-memory Messaging transition.

        Args:
            transition: Complete framework-created lifecycle transition.

        Returns:
            Result committed with the transition's storage effect.

        Raises:
            MessagingError: Current state rejects the transition.
            TypeError: The transition has the wrong public type.
            ValueError: Transition identifiers or options are invalid.
        """

        from ._messaging_transition import resolve_messaging_transition
        from .backend_contract import MessagingTransition

        if not isinstance(transition, MessagingTransition):
            raise TypeError("transition must be a MessagingTransition")
        selected_generation = (
            transition.cleanup_generation
            if transition.kind == "finish_generation_cleanup"
            else (
                None
                if transition.run_reference is None
                else transition.run_reference.generation
            )
        )
        if transition.kind in {"prepare_run", "append_message", "publish_message"}:
            self._reclaim_expired()
        channel_state = self._channel(transition.channel)
        async with channel_state.lock:
            stream_state = self._expire_if_due(
                channel_state,
                thread_key(transition.identity),
            )
            if stream_state is None:
                snapshot = self._memory_state_snapshot(
                    channel_state,
                    transition.channel,
                    transition.identity,
                    generation=selected_generation,
                    message_id=transition.message_id,
                    include_active_run=True,
                )
                effect = resolve_messaging_transition(transition, snapshot)
                self._apply_memory_effect(
                    channel_state,
                    transition.identity,
                    effect,
                )
                return effect.result
            async with stream_state.condition:
                snapshot = self._memory_state_snapshot(
                    channel_state,
                    transition.channel,
                    transition.identity,
                    generation=selected_generation,
                    message_id=transition.message_id,
                    include_active_run=True,
                )
                effect = resolve_messaging_transition(transition, snapshot)
                self._apply_memory_effect(
                    channel_state,
                    transition.identity,
                    effect,
                )
                stream_state.condition.notify_all()
                return effect.result

    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        """Load one consistent bounded process-local Messaging snapshot.

        Args:
            query: Channel, generation, run, and optional message evidence to load.

        Returns:
            Defensive state snapshot observed under the channel and stream locks.

        Raises:
            TypeError: The query has the wrong public type.
            ValueError: Query identifiers are invalid.
        """

        from .backend_contract import MessagingStateQuery

        if not isinstance(query, MessagingStateQuery):
            raise TypeError("query must be a MessagingStateQuery")
        required_identifier("channel", query.channel)
        required_identity(query.identity)
        channel_state = self._channels.get(query.channel)
        if channel_state is None:
            return self._empty_memory_snapshot()
        async with channel_state.lock:
            stream_state = self._expire_if_due(
                channel_state,
                thread_key(query.identity),
            )
            if stream_state is None:
                return self._memory_state_snapshot(
                    channel_state,
                    query.channel,
                    query.identity,
                    generation=query.generation,
                    message_id=query.message_id,
                    include_active_run=query.include_active_run,
                )
            async with stream_state.condition:
                return self._memory_state_snapshot(
                    channel_state,
                    query.channel,
                    query.identity,
                    generation=query.generation,
                    message_id=query.message_id,
                    include_active_run=query.include_active_run,
                )

    async def read_committed_messages(
        self,
        query: CommittedMessageQuery,
    ) -> CommittedMessagePage:
        """Read one bounded ascending page from an exact in-memory generation.

        Args:
            query: Exact generation, exclusive cursor, upper bound, and page limit.

        Returns:
            Defensive committed messages and their observed change cursor.

        Raises:
            InvalidCursor: The exclusive cursor exceeds the generation tail.
            StreamDeleted: The exact generation was deleted.
            StreamExpired: The exact generation exceeded retention.
            TypeError: A cursor, limit, or query has the wrong type.
            ValueError: A cursor or limit is outside its supported range.
        """

        from .backend_contract import (
            CommittedMessagePage,
            CommittedMessageQuery,
            MessagingChangeCursor,
        )

        if not isinstance(query, CommittedMessageQuery):
            raise TypeError("query must be a CommittedMessageQuery")
        if isinstance(query.after_sequence, bool) or not isinstance(
            query.after_sequence, int
        ):
            raise TypeError("after_sequence must be an integer")
        if query.after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if isinstance(query.limit, bool) or not isinstance(query.limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= query.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        channel_state = self._channels.get(query.channel)
        if channel_state is None:
            self._raise_memory_generation_unavailable(query)
        assert channel_state is not None
        async with channel_state.lock:
            stream_state = self._expire_if_due(
                channel_state,
                thread_key(query.identity),
            )
            if stream_state is None or stream_state.generation != query.generation:
                self._raise_memory_generation_unavailable(query)
            assert stream_state is not None
            async with stream_state.condition:
                if stream_state.disposition != "active":
                    self._raise_memory_stream_disposition(
                        channel=query.channel,
                        identity=query.identity,
                        generation=stream_state.generation,
                        disposition=stream_state.disposition,
                    )
                latest = len(stream_state.messages)
                if query.after_sequence > latest:
                    raise InvalidCursor(after=query.after_sequence, latest=latest)
                through = (
                    latest
                    if query.through_sequence is None
                    else min(query.through_sequence, latest)
                )
                run_record = stream_state.runs.get(query.identity.run_id)
                if (
                    query.stop_at_run_terminal
                    and run_record is not None
                    and is_final_run_status(run_record.status)
                ):
                    through = min(through, run_record.end_seq)
                end = min(through, query.after_sequence + query.limit)
                messages = tuple(
                    message.model_copy(deep=True)
                    for message in stream_state.messages[query.after_sequence : end]
                )
                return CommittedMessagePage(
                    generation=stream_state.generation,
                    latest_sequence=latest,
                    messages=messages,
                    change_cursor=MessagingChangeCursor(
                        message_sequence=latest,
                        control_sequence=stream_state.control_sequence,
                    ),
                    run_state=(
                        None
                        if run_record is None
                        else self._stored_memory_run(
                            run_record,
                            stream_state.generation,
                        )
                    ),
                )

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        """Wait for an in-memory message or control change with a bounded timeout.

        Args:
            wait: Exact generation, observed cursor, and optional wait duration.

        Returns:
            ``None`` after a change, spurious wakeup, or timeout.

        Raises:
            StreamDeleted: The exact generation was deleted while waiting.
            StreamExpired: The exact generation exceeded retention while waiting.
            TypeError: The wait value has the wrong public type.
        """

        from .backend_contract import MessagingChangeWait

        if not isinstance(wait, MessagingChangeWait):
            raise TypeError("wait must be a MessagingChangeWait")
        channel_state = self._channels.get(wait.channel)
        if channel_state is None:
            self._raise_memory_generation_unavailable(wait)
        assert channel_state is not None
        stream_state = self._expire_if_due(
            channel_state,
            thread_key(wait.identity),
        )
        if stream_state is None or stream_state.generation != wait.generation:
            self._raise_memory_generation_unavailable(wait)
        assert stream_state is not None
        async with stream_state.condition:
            if stream_state.disposition != "active":
                self._raise_memory_stream_disposition(
                    channel=wait.channel,
                    identity=wait.identity,
                    generation=wait.generation,
                    disposition=stream_state.disposition,
                )
            if (
                len(stream_state.messages) != wait.after.message_sequence
                or stream_state.control_sequence != wait.after.control_sequence
            ):
                return
            try:
                # Keep notification and cancellation in the caller's task so a
                # completed condition wait cannot consume concurrent cancellation.
                async with asyncio.timeout(wait.timeout_seconds):
                    await stream_state.condition.wait()
            except TimeoutError:
                return

    async def purge_stream_generation(
        self,
        purge: StreamGenerationPurge,
    ) -> StreamGenerationPurgeResult:
        """Remove one bounded batch from a sealed in-memory generation.

        Args:
            purge: Exact sealed generation and maximum records to remove.

        Returns:
            Removed record count and whether generation-private records are exhausted.

        Raises:
            StreamDeleteConflict: The generation remains active.
            TypeError: The purge value or bound has the wrong type.
            ValueError: The deletion bound is not positive.
        """

        from .backend_contract import (
            StreamGenerationPurge,
            StreamGenerationPurgeResult,
        )

        if not isinstance(purge, StreamGenerationPurge):
            raise TypeError("purge must be a StreamGenerationPurge")
        if isinstance(purge.maximum_records, bool) or not isinstance(
            purge.maximum_records, int
        ):
            raise TypeError("maximum_records must be an integer")
        if purge.maximum_records < 1:
            raise ValueError("maximum_records must be positive")
        channel_state = self._channels.get(purge.channel)
        if channel_state is None:
            return StreamGenerationPurgeResult(removed_records=0, complete=True)
        async with channel_state.lock:
            stream_state = channel_state.streams.get(thread_key(purge.identity))
            if stream_state is None or stream_state.generation != purge.generation:
                return StreamGenerationPurgeResult(removed_records=0, complete=True)
            async with stream_state.condition:
                if stream_state.disposition not in {"deleting", "expiring"}:
                    raise StreamDeleteConflict(
                        channel=purge.channel,
                        identity=purge.identity,
                        active_identity=(
                            stream_state.active_identity or purge.identity
                        ),
                    )
                removed = 0
                while stream_state.messages and removed < purge.maximum_records:
                    message = stream_state.messages.pop()
                    stream_state.by_message_id.pop(message.message_id, None)
                    signature = stream_state.signatures.pop(message.message_id, None)
                    released_bytes = len(message.payload) + checkpoint_bytes(
                        None if signature is None else signature[3]
                    )
                    self._release_memory_records(stream_state, released_bytes, 1)
                    removed += 1
                while stream_state.runs and removed < purge.maximum_records:
                    run = stream_state.runs.pop(next(iter(stream_state.runs)))
                    self._release_memory_records(
                        stream_state, checkpoint_bytes(run.checkpoint), 1
                    )
                    removed += 1
                complete = not stream_state.messages and not stream_state.runs
                return StreamGenerationPurgeResult(
                    removed_records=removed,
                    complete=complete,
                )

    @staticmethod
    def _empty_memory_snapshot() -> MessagingStateSnapshot:
        from .backend_contract import MessagingStateSnapshot

        return MessagingStateSnapshot(
            observed_at=datetime.now(UTC),
            channel=None,
            stream=None,
            target_run=None,
            active_run=None,
            matching_message=None,
        )

    def _memory_state_snapshot(
        self,
        channel_state: _ChannelState,
        channel: str,
        identity: RunIdentity,
        *,
        generation: int | None,
        message_id: str | None,
        include_active_run: bool,
    ) -> MessagingStateSnapshot:
        from ._messaging_transition import messaging_message_signature
        from .backend_contract import (
            MessagingStateSnapshot,
            StoredMessageEvidence,
            StoredMessagingChannel,
            StoredMessagingStream,
        )

        stream_state = channel_state.streams.get(thread_key(identity))
        selected_state = stream_state
        if selected_state is not None and (
            generation is not None and selected_state.generation != generation
        ):
            selected_state = None
        tombstone = self._tombstone_reason(
            channel_state,
            thread_key(identity),
            generation,
        )
        stored_channel = (
            None
            if channel_state.codec is None
            else StoredMessagingChannel(
                channel=channel,
                codec_id=channel_state.codec,
                limits=self._limits,
                retention_policy=self._retention_policy,
            )
        )
        if selected_state is None:
            return MessagingStateSnapshot(
                observed_at=datetime.now(UTC),
                channel=stored_channel,
                stream=None,
                target_run=None,
                active_run=None,
                matching_message=None,
                tombstone_generation=None if tombstone is None else tombstone[0],
                tombstone_reason=None if tombstone is None else tombstone[1],
            )

        target_record = selected_state.runs.get(identity.run_id)
        active_record = None
        if include_active_run and selected_state.active_identity is not None:
            active_record = selected_state.runs.get(
                selected_state.active_identity.run_id
            )
            if active_record is target_record:
                active_record = None
        matching_message = None
        if message_id is not None:
            envelope = selected_state.by_message_id.get(message_id)
            signature_values = selected_state.signatures.get(message_id)
            if envelope is not None and signature_values is not None:
                run_id, codec_id, payload, checkpoint = signature_values
                matching_message = StoredMessageEvidence(
                    envelope=envelope.model_copy(deep=True),
                    signature=messaging_message_signature(
                        identity=RunIdentity(
                            namespace=identity.namespace,
                            thread_id=identity.thread_id,
                            run_id=run_id,
                        ),
                        codec_id=codec_id,
                        payload=payload,
                        checkpoint=checkpoint,
                    ),
                    checkpoint=checkpoint,
                )
        next_fence = 1 + max(
            (record.fence for record in selected_state.runs.values()),
            default=0,
        )
        return MessagingStateSnapshot(
            observed_at=datetime.now(UTC),
            channel=stored_channel,
            stream=StoredMessagingStream(
                channel=channel,
                thread_id=identity.thread_id,
                generation=selected_state.generation,
                disposition=selected_state.disposition,
                latest_sequence=len(selected_state.messages),
                payload_bytes=selected_state.payload_bytes,
                next_producer_fence=next_fence,
                active_run_id=(
                    None
                    if selected_state.active_identity is None
                    else selected_state.active_identity.run_id
                ),
                retention_expired=False,
                message_sequence=len(selected_state.messages),
                control_sequence=selected_state.control_sequence,
            ),
            target_run=(
                None
                if target_record is None
                else self._stored_memory_run(target_record, selected_state.generation)
            ),
            active_run=(
                None
                if active_record is None
                else self._stored_memory_run(active_record, selected_state.generation)
            ),
            matching_message=matching_message,
            tombstone_generation=None if tombstone is None else tombstone[0],
            tombstone_reason=None if tombstone is None else tombstone[1],
        )

    @staticmethod
    def _stored_memory_run(
        record: _RunRecord,
        generation: int,
    ) -> StoredMessagingRun:
        from .backend_contract import StoredMessagingRun

        return StoredMessagingRun(
            identity=record.identity,
            generation=generation,
            start_sequence=record.start_seq,
            request_digest=record.request_digest,
            end_sequence=record.end_seq,
            status=record.status,
            settlement_started=record.settling,
            publication_closed=record.publication_closed,
            publication_ready=record.publication_ready,
            cancellable=record.cancellable,
            recoverable=record.recoverable,
            producer_token=record.owner_token,
            producer_fence=record.fence,
            producer_lease_active=(
                not is_final_run_status(record.status)
                and record.owner_token is not None
            ),
            checkpoint=record.checkpoint,
            failure_class=(
                ""
                if record.error is None
                else f"{type(record.error).__module__}.{type(record.error).__qualname__}"
            ),
            failure_message="" if record.error is None else str(record.error),
            local_failure=record.error,
        )

    def _apply_memory_effect(
        self,
        channel_state: _ChannelState,
        identity: RunIdentity,
        effect: MessagingStorageEffect,
    ) -> None:
        # Every quota check and charge runs without yielding, including across channel
        # locks. Rejected transitions leave no channel, generation, or counter behind.
        total_bytes, total_records, generation_records = self._effect_capacity(
            channel_state, identity, effect
        )
        if self._total_bytes + total_bytes > self._limits.max_total_bytes:
            raise MessagingQuotaExceeded(
                resource="total_bytes", limit=self._limits.max_total_bytes
            )
        if self._total_records + total_records > self._limits.max_total_records:
            raise MessagingQuotaExceeded(
                resource="total_records", limit=self._limits.max_total_records
            )
        if effect.channel is not None:
            if channel_state.codec is None:
                channel_state.codec = effect.channel.codec_id
                self._channels[channel_state.channel] = channel_state
            elif channel_state.codec != effect.channel.codec_id:
                raise CodecMismatch(
                    expected=channel_state.codec,
                    actual=effect.channel.codec_id,
                )
        stream_effect = effect.stream
        stream_state = channel_state.streams.get(thread_key(identity))
        if stream_effect is not None and (
            stream_state is None or stream_state.generation != stream_effect.generation
        ):
            stream_state = _StreamState(generation=stream_effect.generation)
            channel_state.streams[thread_key(identity)] = stream_state
        self._total_bytes += total_bytes
        self._total_records += total_records
        if stream_state is not None:
            stream_state.retained_bytes += total_bytes
            stream_state.retained_records += generation_records
        if stream_effect is not None:
            assert stream_state is not None
            stream_state.disposition = stream_effect.disposition
            stream_state.deleted = stream_effect.disposition != "active"
            stream_state.payload_bytes = stream_effect.payload_bytes
            stream_state.control_sequence = stream_effect.control_sequence
            stream_state.active_identity = (
                None
                if stream_effect.active_run_id is None
                else RunIdentity(
                    namespace=identity.namespace,
                    thread_id=identity.thread_id,
                    run_id=stream_effect.active_run_id,
                )
            )
        if effect.runs:
            if stream_state is None:
                raise RuntimeError("Messaging run effect requires a stream")
            for stored_run in effect.runs:
                record = stream_state.runs.get(stored_run.identity.run_id)
                if record is None:
                    record = _RunRecord(
                        identity=stored_run.identity,
                        start_seq=stored_run.start_sequence,
                        owner_token=stored_run.producer_token or "",
                        cancellable=stored_run.cancellable,
                        recoverable=stored_run.recoverable,
                        request_digest=stored_run.request_digest,
                    )
                    stream_state.runs[stored_run.identity.run_id] = record
                record.start_seq = stored_run.start_sequence
                record.end_seq = stored_run.end_sequence
                record.owner_token = stored_run.producer_token
                record.fence = stored_run.producer_fence
                record.cancellable = stored_run.cancellable
                record.recoverable = stored_run.recoverable
                record.request_digest = stored_run.request_digest
                record.status = stored_run.status
                record.settling = stored_run.settlement_started
                record.publication_closed = stored_run.publication_closed
                record.publication_ready = stored_run.publication_ready
                record.error = stored_run.local_failure
                record.checkpoint = stored_run.checkpoint
        if effect.message is not None:
            if stream_state is None:
                raise RuntimeError("Messaging message effect requires a stream")
            envelope = effect.message.envelope.model_copy(deep=True)
            if envelope.message_id not in stream_state.by_message_id:
                stream_state.messages.append(envelope)
                stream_state.by_message_id[envelope.message_id] = envelope
                stream_state.signatures[envelope.message_id] = (
                    envelope.identity.run_id,
                    envelope.codec,
                    bytes(envelope.payload),
                    effect.message.checkpoint,
                )
        if stream_state is not None:
            if effect.retention_action == "clear":
                stream_state.expires_at_monotonic = None
            elif effect.retention_action == "start":
                terminal_ttl = self._retention_policy.terminal_ttl_seconds
                stream_state.expires_at_monotonic = (
                    None if terminal_ttl is None else time.monotonic() + terminal_ttl
                )
            if effect.retention_action != "none":
                self._expirations.set(
                    (channel_state.channel, thread_key(identity)),
                    stream_state.expires_at_monotonic,
                )
        if effect.tombstone_reason is not None:
            if stream_state is None:
                raise RuntimeError("Messaging tombstone effect requires a stream")
            self._retire_memory_generation(
                channel_state,
                thread_key(identity),
                stream_state,
                effect.tombstone_reason,
            )

    def _effect_capacity(
        self,
        channel: _ChannelState,
        identity: RunIdentity,
        effect: MessagingStorageEffect,
    ) -> tuple[int, int, int]:
        state = channel.streams.get(thread_key(identity))
        records = int(channel.codec is None and effect.channel is not None)
        if effect.stream is not None and state is None:
            records += 1 + int(thread_key(identity) not in channel.next_generations)
        generation_records = 0
        retained_bytes = 0
        for run in effect.runs:
            previous = None if state is None else state.runs.get(run.identity.run_id)
            generation_records += int(previous is None)
            retained_bytes += checkpoint_bytes(run.checkpoint) - checkpoint_bytes(
                None if previous is None else previous.checkpoint
            )
        message = effect.message
        if message is not None and (
            state is None or message.envelope.message_id not in state.by_message_id
        ):
            generation_records += 1
            retained_bytes += len(message.envelope.payload) + checkpoint_bytes(
                message.checkpoint
            )
        return retained_bytes, records + generation_records, generation_records

    def _release_memory_records(
        self, state: _StreamState, released_bytes: int, records: int
    ) -> None:
        self._total_bytes -= released_bytes
        self._total_records -= records
        state.retained_bytes -= released_bytes
        state.retained_records -= records

    def _retire_memory_generation(
        self,
        channel: _ChannelState,
        thread_id: str,
        state: _StreamState,
        reason: Literal["deleted", "expired"],
    ) -> None:
        # The generation record becomes its tombstone; cleanup never needs spare quota.
        self._total_bytes -= state.retained_bytes
        self._total_records -= state.retained_records - 1
        state.deleted = True
        state.disposition = reason
        channel.streams.pop(thread_id, None)
        channel.next_generations[thread_id] = state.generation + 1
        self._record_tombstone(
            channel, thread_id, generation=state.generation, reason=reason
        )
        self._expirations.set((channel.channel, thread_id), None)

    def _reclaim_expired(self) -> None:
        # Bounded admission work can release other threads without scanning all channels.
        now = time.monotonic()
        for _ in range(64):
            key = self._expirations.pop_due(now)
            if key is None:
                return
            channel, thread = key
            self._expire_if_due(self._channels[channel], thread)

    def _raise_memory_generation_unavailable(
        self,
        query: CommittedMessageQuery | MessagingChangeWait,
    ) -> Never:
        channel_state = self._channels.get(query.channel)
        if channel_state is not None:
            tombstone = self._tombstone_reason(
                channel_state,
                thread_key(query.identity),
                query.generation,
            )
            if tombstone is not None:
                generation, reason = tombstone
                self._raise_tombstone(
                    channel=query.channel,
                    identity=query.identity,
                    generation=generation,
                    reason=reason,
                )
        raise StreamDeleted(
            channel=query.channel,
            identity=query.identity,
            generation=query.generation,
        )

    @staticmethod
    def _raise_memory_stream_disposition(
        *,
        channel: str,
        identity: RunIdentity,
        generation: int,
        disposition: str,
    ) -> Never:
        if disposition in {"expiring", "expired"}:
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=generation,
            )
        raise StreamDeleted(
            channel=channel,
            identity=identity,
            generation=generation,
        )

    @staticmethod
    def _record_tombstone(
        channel_state: _ChannelState,
        thread_id: str,
        *,
        generation: int,
        reason: Literal["deleted", "expired"],
    ) -> None:
        """Retain why an old generation can no longer satisfy a bound cursor."""

        channel_state.tombstones.setdefault(thread_id, {})[generation] = reason

    def _expire_if_due(
        self,
        channel_state: _ChannelState,
        thread_id: str,
    ) -> _StreamState | None:
        """Lazily retire one terminal generation using a monotonic deadline."""

        state = channel_state.streams.get(thread_id)
        if state is None:
            return None
        deadline = state.expires_at_monotonic
        if deadline is None or time.monotonic() < deadline:
            return state
        self._retire_memory_generation(channel_state, thread_id, state, "expired")
        return None

    @staticmethod
    def _tombstone_reason(
        channel_state: _ChannelState,
        thread_id: str,
        generation: int | None,
    ) -> tuple[int, Literal["deleted", "expired"]] | None:
        """Resolve an exact or latest unavailable generation disposition."""

        tombstones = channel_state.tombstones.get(thread_id, {})
        if generation is not None:
            reason = tombstones.get(generation)
            return None if reason is None else (generation, reason)
        if not tombstones:
            return None
        latest = max(tombstones)
        return latest, tombstones[latest]

    @staticmethod
    def _raise_tombstone(
        *,
        channel: str,
        identity: RunIdentity,
        generation: int,
        reason: Literal["deleted", "expired"],
    ) -> None:
        """Raise the stable error associated with one generation tombstone."""

        if reason == "expired":
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=generation,
            )
        raise StreamDeleted(
            channel=channel,
            identity=identity,
            generation=generation,
        )

    def _channel(self, channel: str) -> _ChannelState:
        state = self._channels.get(channel)
        if state is None:
            state = _ChannelState(channel)
        return state
