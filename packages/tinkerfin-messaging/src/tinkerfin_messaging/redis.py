"""Optional Redis backend with atomic logs, leases, and fencing."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Never, cast
from uuid import uuid4

from redis.asyncio import Redis

from tinkerfin_contracts import RunIdentity

from . import _redis_capacity, _redis_control, _redis_journal
from ._identity import required_identifier, required_identity
from ._messaging_ledger import BackendRunHandle as _BackendRunHandle
from ._redis_control import (
    _AsyncRedisClient,
    _RedisKeys,
    _RedisScriptValue,
    _RedisStreamScope,
    _RunSnapshot,
    _StreamControl,
)
from ._redis_notifications import _RedisNotifications, _validate_wait
from ._redis_scripts import _MESSAGING_STATE_SNAPSHOT_SCRIPT
from .backend import (
    RunStatus,
    is_final_run_status,
)
from .backend_contract import (
    CommittedMessagePage,
    CommittedMessageQuery,
    MessagingBackendSettings,
    MessagingChangeCursor,
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
    StreamGenerationPurge,
    StreamGenerationPurgeResult,
)
from .errors import InvalidCursor, RunNotFound, StreamDeleted, StreamExpired
from .limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint
from .retention import MessagingRetentionPolicy


class RedisBackend:
    """Persist ordered streams and distributed run leases in real Redis.

    The injected client is borrowed and must use ``decode_responses=False`` so
    arbitrary payload bytes survive round trips unchanged. Total limits are shared by
    all channels under ``key_prefix``; every key in that scope uses one Cluster hash
    slot. Terminal retention is opt-in and indexed across threads for later admission.
    """

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = "tinkerfin-messaging",
        producer_lease_seconds: float = 15.0,
        generation_cleanup_retry_seconds: float = 0.1,
        limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
        retention_policy: MessagingRetentionPolicy = MessagingRetentionPolicy(),
    ) -> None:
        """Initialize a backend that borrows one binary Redis client.

        Args:
            client: Open asynchronous Redis client with response decoding disabled.
            key_prefix: Deployment namespace for every durable key.
            producer_lease_seconds: Producer ownership duration on the Redis clock.
            generation_cleanup_retry_seconds: Delay before retrying cleanup ownership.
            limits: Immutable individual, per-thread, and prefix-wide capacity contract.
            retention_policy: Terminal replay deadline policy; disabled by default.

        Raises:
            TypeError: A timing or limits value has the wrong type.
            ValueError: An identifier, timing value, or client mode is invalid.
        """

        required_identifier("key_prefix", key_prefix)
        if isinstance(producer_lease_seconds, bool) or not isinstance(
            producer_lease_seconds, int | float
        ):
            raise TypeError("producer_lease_seconds must be a number")
        if isinstance(generation_cleanup_retry_seconds, bool) or not isinstance(
            generation_cleanup_retry_seconds, int | float
        ):
            raise TypeError("generation_cleanup_retry_seconds must be a number")
        resolved_lease_ttl = float(producer_lease_seconds)
        resolved_poll_interval = float(generation_cleanup_retry_seconds)
        if not math.isfinite(resolved_lease_ttl) or resolved_lease_ttl <= 0:
            raise ValueError("producer_lease_seconds must be a finite positive number")
        if not math.isfinite(resolved_poll_interval) or resolved_poll_interval <= 0:
            raise ValueError(
                "generation_cleanup_retry_seconds must be a finite positive number"
            )
        if not isinstance(limits, MessagingLimits):
            raise TypeError("limits must be a MessagingLimits")
        if not isinstance(retention_policy, MessagingRetentionPolicy):
            raise TypeError("retention_policy must be a MessagingRetentionPolicy")
        connection_options = cast(
            Mapping[str, object],
            client.get_connection_kwargs(),
        )
        if connection_options.get("decode_responses", False):
            raise ValueError("RedisBackend requires decode_responses=False")
        self._client = cast(_AsyncRedisClient, client)
        self._prefix = key_prefix
        self._namespace = f"{key_prefix}:{{{self._digest(key_prefix)}}}"
        self._capacity_key = f"{self._namespace}:capacity"
        self._expirations_key = f"{self._namespace}:expirations"
        self._notifications_key = f"{self._namespace}:notifications"
        self._notification_counter_key = f"{self._namespace}:notification-counter"
        self._notifications = _RedisNotifications(self)
        self._lease_ttl = resolved_lease_ttl
        self._lease_ms = max(1, math.ceil(resolved_lease_ttl * 1000))
        self._poll_interval = resolved_poll_interval
        self._socket_timeout_budget_ms = self._socket_timeout_budget(
            connection_options.get("socket_timeout")
        )
        self._worker_id = uuid4().hex
        self._limits = limits
        self._retention_policy = retention_policy
        terminal_ttl = retention_policy.terminal_ttl_seconds
        self._retention_ms = (
            0 if terminal_ttl is None else max(1, math.ceil(terminal_ttl * 1000))
        )

    @property
    def messaging_settings(self) -> MessagingBackendSettings:
        """Return immutable settings shared by this Redis deployment.

        Returns:
            Capacity, retention, lease, renewal, and bounded change-wait settings.
        """

        return MessagingBackendSettings(
            limits=self._limits,
            retention_policy=self._retention_policy,
            producer_renew_interval_seconds=self._lease_ttl / 3,
            producer_lease_seconds=self._lease_ttl,
            change_wait_timeout_seconds=5.0,
        )

    async def prepare_messaging_storage(self) -> None:
        """Confirm that Redis structures remain lazily and atomically initialized.

        Returns:
            ``None`` because the first transition creates and validates current keys.
        """

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        """Commit one framework transition through the current Redis state machines.

        Generation fencing, idempotency, and total-capacity accounting commit in the
        same Lua transition. Framework values are translated at this Redis boundary;
        callers receive storage-neutral results and stable package exceptions.

        Args:
            transition: Complete framework-defined lifecycle intent.

        Returns:
            Storage-neutral result committed by the matching Redis transition.

        Raises:
            MessagingError: Current durable state rejects the transition.
            TypeError: The transition or a required field has the wrong type.
            ValueError: Transition identifiers or options are invalid.
        """

        if not isinstance(transition, MessagingTransition):
            raise TypeError("transition must be a MessagingTransition")
        kind = transition.kind
        if kind in {"prepare_run", "append_message", "publish_message"}:
            await _redis_capacity.reclaim_expired(self)
        if kind == "prepare_run":
            codec_id = self._required_transition_text(
                "codec_id",
                transition.codec_id,
            )
            prepared = await _redis_journal.prepare(
                self,
                channel=transition.channel,
                identity=transition.identity,
                codec=codec_id,
                after=transition.after_sequence,
                cancellable=transition.cancellable,
                recoverable=transition.recoverable,
            )
            return MessagingTransitionResult(
                kind=kind,
                run_reference=self._storage_reference(prepared.handle),
                after_sequence=prepared.after,
                is_producer_owner=prepared.is_owner,
                checkpoint=prepared.checkpoint,
                recovered=prepared.recovered,
            )
        if kind in {"append_message", "publish_message"}:
            reference = self._required_transition_reference(transition)
            message_id = self._required_transition_text(
                "message_id",
                transition.message_id,
            )
            codec_id = self._required_transition_text(
                "codec_id",
                transition.codec_id,
            )
            payload = transition.payload
            if not isinstance(payload, bytes):
                raise TypeError("payload must be bytes")
            envelope = await _redis_journal.append(
                self,
                self._internal_handle(reference),
                message_id=message_id,
                codec=codec_id,
                payload=payload,
                checkpoint=transition.checkpoint,
                external=kind == "publish_message",
                closes_publication=transition.closes_publication,
                opens_publication=transition.opens_publication,
            )
            return MessagingTransitionResult(kind=kind, envelope=envelope)
        if kind == "begin_settlement":
            reference = self._required_transition_reference(transition)
            cancellation_preceded = await _redis_control.begin_settlement(
                self,
                self._internal_handle(reference),
            )
            return MessagingTransitionResult(
                kind=kind,
                cancellation_preceded_settlement=cancellation_preceded,
            )
        if kind == "finish_run":
            reference = self._required_transition_reference(transition)
            status = transition.final_status
            if status not in {"completed", "cancelled", "failed", "owner_lost"}:
                raise TypeError("final_status must be a terminal run status")
            await _redis_control.finish(
                self,
                self._internal_handle(reference),
                status=status,
                error=transition.failure,
            )
            return MessagingTransitionResult(kind=kind, run_status=status)
        if kind == "request_cancellation":
            initiated = await _redis_control.request_cancel(
                self,
                _BackendRunHandle(
                    channel=transition.channel,
                    identity=transition.identity,
                    owner_token=None,
                    fence=None,
                    generation=(
                        None
                        if transition.run_reference is None
                        else transition.run_reference.generation
                    ),
                ),
            )
            return MessagingTransitionResult(
                kind=kind,
                cancellation_requested_by_transition=initiated,
            )
        if kind == "renew_producer_ownership":
            reference = self._required_transition_reference(transition)
            renewed = await _redis_control.renew(
                self,
                self._internal_handle(reference),
            )
            return MessagingTransitionResult(
                kind=kind,
                producer_ownership_confirmed=renewed,
            )
        if kind == "reconcile_producer_ownership":
            reference = transition.run_reference
            if reference is None:
                status = await _redis_control._reconcile_current_run_status(
                    self,
                    channel=transition.channel,
                    identity=transition.identity,
                )
            else:
                handle = self._internal_handle(reference)
                keys = await self._keys_for_handle(handle)
                snapshot = await self._settled_run_snapshot(
                    keys,
                    transition.identity,
                )
                status = snapshot.status
            return MessagingTransitionResult(kind=kind, run_status=status)
        if kind == "begin_generation_cleanup":
            reason = transition.cleanup_reason
            if reason not in {"deleted", "expired"}:
                raise TypeError("cleanup_reason must be deleted or expired")
            (
                generation,
                actual_reason,
                cleanup_token,
                cleanup_required,
            ) = await _redis_control._begin_generation_cleanup(
                self,
                channel=transition.channel,
                identity=transition.identity,
                requested_reason=reason,
            )
            return MessagingTransitionResult(
                kind=kind,
                cleanup_generation=generation,
                cleanup_reason=actual_reason,
                cleanup_token=cleanup_token,
                cleanup_required=cleanup_required,
            )
        if kind == "finish_generation_cleanup":
            generation = transition.cleanup_generation
            reason = transition.cleanup_reason
            if isinstance(generation, bool) or not isinstance(generation, int):
                raise TypeError("cleanup_generation must be an integer")
            if reason not in {"deleted", "expired"}:
                raise TypeError("cleanup_reason must be deleted or expired")
            await _redis_control._finish_generation_cleanup(
                self,
                channel=transition.channel,
                identity=transition.identity,
                generation=generation,
                reason=reason,
                cleanup_token=transition.cleanup_token,
            )
            return MessagingTransitionResult(
                kind=kind,
                cleanup_generation=generation,
                cleanup_reason=reason,
                cleanup_token=transition.cleanup_token,
                cleanup_required=False,
            )
        raise TypeError(f"unsupported Messaging transition kind: {kind}")

    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        """Load current Redis channel, generation, run, and message evidence.

        Args:
            query: Exact channel, optional generation, run, and message evidence.

        Returns:
            Storage-clock-consistent values decoded from the current Redis shape.

        Raises:
            MessagingBackendError: Redis values are unavailable or malformed.
            TypeError: The query has the wrong public type.
            ValueError: Query identifiers are invalid.
        """

        if not isinstance(query, MessagingStateQuery):
            raise TypeError("query must be a MessagingStateQuery")
        required_identifier("channel", query.channel)
        required_identity(query.identity)
        generation = query.generation
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int)
        ):
            raise TypeError("generation must be an integer or None")
        if generation is not None and generation < 1:
            raise ValueError("generation must be positive")
        if query.message_id is not None:
            required_identifier("message_id", query.message_id)
        if (
            generation is not None
            and query.message_id is None
            and not query.include_active_run
        ):
            keys = self._keys(
                query.channel,
                query.identity,
                generation=generation,
            )
            try:
                snapshot = await self._settled_run_snapshot(keys, query.identity)
            except (RunNotFound, StreamDeleted, StreamExpired):
                return await self._load_atomic_messaging_state(query)
            return self._state_from_run_snapshot(
                query.channel,
                query.identity,
                generation,
                snapshot,
            )
        return await self._load_atomic_messaging_state(query)

    async def read_committed_messages(
        self,
        query: CommittedMessageQuery,
    ) -> CommittedMessagePage:
        """Read one generation-fenced Redis message page without changing its shape.

        Args:
            query: Exact generation, exclusive cursor, upper bound, and page limit.

        Returns:
            Ascending decoded messages and the observed message and control cursors.

        Raises:
            InvalidCursor: The exclusive cursor exceeds the generation tail.
            StreamDeleted: The exact generation was deleted.
            StreamExpired: The exact generation exceeded retention.
            MessagingBackendError: Redis evidence is unavailable or malformed.
        """

        if not isinstance(query, CommittedMessageQuery):
            raise TypeError("query must be a CommittedMessageQuery")
        handle = _BackendRunHandle(
            channel=query.channel,
            identity=query.identity,
            owner_token=None,
            fence=None,
            generation=query.generation,
        )
        keys = await self._keys_for_handle(handle)
        if query.stop_at_run_terminal:
            snapshot = await self._settled_run_snapshot(
                keys,
                query.identity,
                after=query.after_sequence,
            )
            messages = snapshot.messages
            if query.through_sequence is not None:
                messages = tuple(
                    message
                    for message in messages
                    if message.seq <= query.through_sequence
                )
            return CommittedMessagePage(
                generation=query.generation,
                latest_sequence=snapshot.latest_seq,
                messages=tuple(messages[: query.limit]),
                change_cursor=MessagingChangeCursor(
                    message_sequence=snapshot.latest_seq,
                    control_sequence=snapshot.signal_cursor,
                ),
                run_state=self._stored_run_from_snapshot(
                    query.identity,
                    query.generation,
                    snapshot,
                ),
            )
        latest_value = await _redis_control._redis_call(
            "Messaging message tail read",
            self._client.hget(keys.meta, "seq"),
        )
        latest = 0 if latest_value is None else int(self._text(latest_value))
        if query.after_sequence > latest:
            raise InvalidCursor(after=query.after_sequence, latest=latest)
        maximum_sequence = (
            latest
            if query.through_sequence is None
            else min(query.through_sequence, latest)
        )
        entries: Sequence[tuple[bytes, Mapping[bytes, bytes]]] = ()
        if query.after_sequence < maximum_sequence:
            entries = cast(
                Sequence[tuple[bytes, Mapping[bytes, bytes]]],
                await _redis_control._redis_call(
                    "Messaging message page read",
                    self._client.xrange(
                        keys.messages,
                        min=f"({query.after_sequence}-0",
                        max=f"{maximum_sequence}-0",
                        count=query.limit,
                    ),
                ),
            )
        signal_value = await _redis_control._redis_call(
            "Messaging control cursor read",
            self._client.hget(keys.control, "signal_seq"),
        )
        if not await self._is_current_generation(keys):
            await _redis_control._raise_generation_unavailable(
                self,
                handle,
                generation=query.generation,
            )
        messages = tuple(
            self._decode_entry(query.channel, query.identity, entry)
            for entry in entries
        )
        return CommittedMessagePage(
            generation=query.generation,
            latest_sequence=latest,
            messages=messages,
            change_cursor=MessagingChangeCursor(
                message_sequence=latest,
                control_sequence=(
                    0 if signal_value is None else int(self._text(signal_value))
                ),
            ),
        )

    async def wait_for_messaging_change(self, wait: MessagingChangeWait) -> None:
        """Wait until the generation may have new messages or control changes.

        Args:
            wait: Exact generation, observed message/control cursor, and timeout.

        Returns:
            ``None`` after Redis reports data, a lifecycle signal, or a timeout.

        Raises:
            StreamDeleted: The exact generation was deleted while waiting.
            StreamExpired: The exact generation exceeded retention while waiting.
            MessagingBackendError: Redis change evidence cannot be observed safely.
        """

        if not isinstance(wait, MessagingChangeWait):
            raise TypeError("wait must be a MessagingChangeWait")
        _validate_wait(wait)
        await self._notifications.wait(wait)

    async def purge_stream_generation(
        self,
        purge: StreamGenerationPurge,
    ) -> StreamGenerationPurgeResult:
        """Remove one bounded batch from an exact sealed Redis generation.

        The method joins or reacquires the generation's short-lived cleanup lease,
        deletes no more than the requested record count, and leaves the tombstone and
        shared channel configuration for the finish transition.

        Args:
            purge: Exact generation and requested physical deletion bound.

        Returns:
            The number of private records removed and whether none remain.

        Raises:
            StreamDeleteConflict: The exact generation is still active.
            MessagingBackendError: Cleanup ownership or state cannot be proven.
            TypeError: The purge value or one of its fields has the wrong type.
            ValueError: The generation or deletion bound is not positive.
        """

        if not isinstance(purge, StreamGenerationPurge):
            raise TypeError("purge must be a StreamGenerationPurge")
        return await _redis_control._purge_stream_generation(self, purge)

    @staticmethod
    def _storage_reference(handle: _BackendRunHandle) -> MessagingRunReference:
        generation = handle.generation
        if generation is None:
            raise _redis_control._redis_protocol_error(
                "Redis preparation returned no stream generation"
            )
        return MessagingRunReference(
            channel=handle.channel,
            identity=handle.identity,
            generation=generation,
            producer_token=handle.owner_token,
            producer_fence=handle.fence,
        )

    @staticmethod
    def _internal_handle(reference: MessagingRunReference) -> _BackendRunHandle:
        return _BackendRunHandle(
            channel=reference.channel,
            identity=reference.identity,
            owner_token=reference.producer_token,
            fence=reference.producer_fence,
            generation=reference.generation,
        )

    @staticmethod
    def _required_transition_reference(
        transition: MessagingTransition,
    ) -> MessagingRunReference:
        reference = transition.run_reference
        if not isinstance(reference, MessagingRunReference):
            raise TypeError("run_reference must be a MessagingRunReference")
        return reference

    @staticmethod
    def _required_transition_text(name: str, value: str | None) -> str:
        if value is None:
            raise TypeError(f"{name} must be a string")
        return required_identifier(name, value)

    async def _load_atomic_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        """Read all requested Redis state at one script execution point."""

        scope = self._scope(query.channel, query.identity)
        while True:
            control = await self._read_control(scope)
            expected_generation = 0 if control is None else control.generation
            selected_generation = (
                query.generation
                if query.generation is not None
                else (1 if control is None else control.generation)
            )
            current_generation = max(1, expected_generation)
            current_keys = self._keys(
                query.channel,
                query.identity,
                generation=current_generation,
            )
            selected_keys = self._keys(
                query.channel,
                query.identity,
                generation=selected_generation,
            )
            expected_active_run_key = ""
            expected_active_lease_key = ""
            if (
                query.include_active_run
                and control is not None
                and control.state == "active"
                and selected_generation == control.generation
            ):
                raw_routing = await _redis_control._redis_call(
                    "Messaging active run routing read",
                    self._client.hgetall(current_keys.meta),
                )
                routing = self._raw_hash(
                    raw_routing,
                    field="Messaging active run routing",
                )
                expected_active_run_key = self._hash_text(
                    routing,
                    "active_key",
                    default="",
                )
                expected_active_lease_key = self._hash_text(
                    routing,
                    "active_lease",
                    default="",
                )
                self._validate_active_routing_keys(
                    current_keys,
                    active_run_key=expected_active_run_key,
                    active_lease_key=expected_active_lease_key,
                )
            active_run_key = (
                expected_active_run_key
                or f"{current_keys.generation_base}:no-active-run"
            )
            active_lease_key = (
                expected_active_lease_key
                or f"{current_keys.generation_base}:no-active-lease"
            )
            dedupe_key = (
                f"{selected_keys.generation_base}:no-message-evidence"
                if query.message_id is None
                else (
                    f"{selected_keys.generation_base}:message:"
                    f"{self._digest(query.message_id)}"
                )
            )
            raw_response = await _redis_control._redis_call(
                "Messaging state snapshot",
                self._client.eval(
                    _MESSAGING_STATE_SNAPSHOT_SCRIPT,
                    10,
                    current_keys.control,
                    current_keys.channel_meta,
                    current_keys.meta,
                    selected_keys.run_key,
                    selected_keys.lease_key,
                    selected_keys.messages,
                    dedupe_key,
                    selected_keys.tombstone,
                    active_run_key,
                    active_lease_key,
                    str(expected_generation),
                    str(selected_generation),
                    "1" if query.include_active_run else "0",
                    expected_active_run_key,
                    expected_active_lease_key,
                    "" if query.message_id is None else query.message_id,
                ),
            )
            if not isinstance(raw_response, list) or not raw_response:
                raise _redis_control._redis_protocol_error(
                    "Redis Messaging state snapshot returned no structured response"
                )
            response = cast(list[_RedisScriptValue], raw_response)
            code = self._snapshot_text(response[0], field="state response code")
            if code in {"GENERATION_CHANGED", "ACTIVE_RUN_CHANGED"}:
                continue
            if code == "INVALID_CONTROL_STATE":
                detail = (
                    self._snapshot_text(response[1], field="invalid control state")
                    if len(response) > 1
                    else ""
                )
                raise _redis_control._redis_protocol_error(
                    f"Redis stream control has invalid state: {detail!r}"
                )
            if code != "OK" or len(response) != 17:
                raise _redis_control._redis_protocol_error(
                    f"unexpected Redis Messaging state snapshot response: {code}"
                )
            return self._decode_atomic_messaging_state(
                query,
                selected_generation=selected_generation,
                response=response,
            )

    def _decode_atomic_messaging_state(
        self,
        query: MessagingStateQuery,
        *,
        selected_generation: int,
        response: list[_RedisScriptValue],
    ) -> MessagingStateSnapshot:
        mode = self._snapshot_text(response[1], field="state mode")
        current_generation = self._snapshot_integer(
            response[2],
            field="state generation",
            minimum=0,
        )
        disposition = self._snapshot_text(response[3], field="stream disposition")
        seconds = self._snapshot_integer(
            response[4],
            field="state observed seconds",
            minimum=0,
        )
        microseconds = self._snapshot_integer(
            response[5],
            field="state observed microseconds",
            minimum=0,
        )
        if microseconds >= 1_000_000:
            raise _redis_control._redis_protocol_error(
                "Redis state snapshot has invalid observed microseconds"
            )
        retention_expired = self._snapshot_boolean(
            response[6],
            field="retention expiry",
        )
        control_values = self._snapshot_hash(response[7], field="stream control")
        channel_values = self._snapshot_hash(response[8], field="channel settings")
        stored_channel = self._stored_channel_from_values(
            query.channel,
            channel_values,
        )
        observed_at = datetime.fromtimestamp(
            seconds + microseconds / 1_000_000,
            tz=UTC,
        )
        if mode == "missing":
            return MessagingStateSnapshot(
                observed_at=observed_at,
                channel=stored_channel,
                stream=None,
                target_run=None,
                active_run=None,
                matching_message=None,
            )
        if mode == "unavailable":
            reason_text = self._snapshot_text(
                response[16],
                field="generation tombstone",
            )
            if reason_text not in {"", "deleted", "expired"}:
                raise _redis_control._redis_protocol_error(
                    "Redis generation tombstone is invalid"
                )
            reason = (
                None if not reason_text else cast(MessagingCleanupReason, reason_text)
            )
            return MessagingStateSnapshot(
                observed_at=observed_at,
                channel=stored_channel,
                stream=None,
                target_run=None,
                active_run=None,
                matching_message=None,
                tombstone_generation=(
                    selected_generation if reason is not None else None
                ),
                tombstone_reason=reason,
            )
        if mode == "sealed":
            if disposition not in {"deleting", "expiring"}:
                raise _redis_control._redis_protocol_error(
                    "Redis sealed snapshot has an invalid disposition"
                )
            return MessagingStateSnapshot(
                observed_at=observed_at,
                channel=stored_channel,
                stream=StoredMessagingStream(
                    channel=query.channel,
                    thread_id=query.identity.thread_id,
                    generation=current_generation,
                    disposition=cast(MessagingStreamDisposition, disposition),
                    latest_sequence=0,
                    payload_bytes=0,
                    next_producer_fence=1,
                    active_run_id=None,
                    retention_expired=retention_expired,
                    message_sequence=0,
                    control_sequence=self._hash_integer(
                        control_values,
                        "signal_seq",
                        default=0,
                    ),
                ),
                target_run=None,
                active_run=None,
                matching_message=None,
            )
        if mode != "active" or disposition != "active":
            raise _redis_control._redis_protocol_error(
                "Redis state snapshot has an invalid active disposition"
            )
        if stored_channel is None:
            raise _redis_control._redis_protocol_error(
                "Redis active stream has no channel settings"
            )
        meta_values = self._snapshot_hash(response[9], field="stream metadata")
        target_values = self._snapshot_hash(response[10], field="target run")
        target_lease_ttl_ms = self._snapshot_integer(
            response[11],
            field="target lease TTL",
            minimum=-2,
        )
        active_values = self._snapshot_hash(response[12], field="active run")
        active_lease_ttl_ms = self._snapshot_integer(
            response[13],
            field="active lease TTL",
            minimum=-2,
        )
        latest_sequence = self._hash_integer(meta_values, "seq", default=0)
        active_run_id = self._hash_text(
            meta_values,
            "active_run",
            default="",
        )
        target_run = self._stored_run_from_values(
            query.identity,
            generation=current_generation,
            values=target_values,
            lease_ttl_ms=target_lease_ttl_ms,
        )
        active_run = None
        if (
            query.include_active_run
            and active_run_id
            and active_run_id != query.identity.run_id
        ):
            active_run = self._stored_run_from_values(
                RunIdentity(
                    namespace=query.identity.namespace,
                    thread_id=query.identity.thread_id,
                    run_id=active_run_id,
                ),
                generation=current_generation,
                values=active_values,
                lease_ttl_ms=active_lease_ttl_ms,
            )
            if active_run is None:
                raise _redis_control._redis_protocol_error(
                    "Redis active run routing has no matching run state"
                )
        matching_message = self._matching_message_from_snapshot(
            query,
            target_values=target_values,
            dedupe_value=response[14],
            message_value=response[15],
        )
        return MessagingStateSnapshot(
            observed_at=observed_at,
            channel=stored_channel,
            stream=StoredMessagingStream(
                channel=query.channel,
                thread_id=query.identity.thread_id,
                generation=current_generation,
                disposition="active",
                latest_sequence=latest_sequence,
                payload_bytes=self._hash_integer(
                    meta_values,
                    "payload_bytes",
                    default=0,
                ),
                next_producer_fence=(
                    self._hash_integer(meta_values, "fence_counter", default=0) + 1
                ),
                active_run_id=active_run_id or None,
                retention_expired=retention_expired,
                message_sequence=latest_sequence,
                control_sequence=self._hash_integer(
                    control_values,
                    "signal_seq",
                    default=0,
                ),
            ),
            target_run=target_run,
            active_run=active_run,
            matching_message=matching_message,
        )

    def _stored_run_from_snapshot(
        self,
        identity: RunIdentity,
        generation: int,
        snapshot: _RunSnapshot,
    ) -> StoredMessagingRun:
        """Convert one atomic run/page snapshot into storage-neutral run state."""

        local_failure = None
        if snapshot.error_class or snapshot.error_message:
            local_failure = self._remote_error(snapshot)
        return StoredMessagingRun(
            identity=identity,
            generation=generation,
            start_sequence=snapshot.start_seq,
            end_sequence=snapshot.end_seq,
            status=snapshot.status,
            settlement_started=snapshot.settling,
            publication_closed=snapshot.publication_closed,
            publication_ready=snapshot.publication_ready,
            cancellable=snapshot.cancellable,
            recoverable=snapshot.recoverable,
            producer_token=(
                None if is_final_run_status(snapshot.status) else snapshot.owner_token
            ),
            producer_fence=snapshot.fence,
            producer_lease_active=(
                not is_final_run_status(snapshot.status) and snapshot.lease_ttl_ms >= 0
            ),
            checkpoint=snapshot.checkpoint,
            failure_class=snapshot.error_class,
            failure_message=snapshot.error_message,
            local_failure=local_failure,
            producer_lease_remaining_seconds=(
                snapshot.lease_ttl_ms / 1000 if snapshot.lease_ttl_ms >= 0 else None
            ),
        )

    def _state_from_run_snapshot(
        self,
        channel: str,
        identity: RunIdentity,
        generation: int,
        snapshot: _RunSnapshot,
    ) -> MessagingStateSnapshot:
        """Return one exact state view from the single Redis run snapshot."""

        stored_limits = (
            snapshot.max_message_payload_bytes,
            snapshot.max_checkpoint_bytes,
            snapshot.max_thread_messages,
            snapshot.max_thread_payload_bytes,
        )
        expected_limits = (
            self._limits.max_message_payload_bytes,
            self._limits.max_checkpoint_bytes,
            self._limits.max_thread_messages,
            self._limits.max_thread_payload_bytes,
        )
        if stored_limits != expected_limits:
            raise _redis_control._redis_protocol_error(
                "Redis channel was opened with different MessagingLimits"
            )
        if snapshot.retention_ms != self._retention_ms:
            raise _redis_control._redis_protocol_error(
                "Redis channel was opened with a different retention policy"
            )
        return MessagingStateSnapshot(
            observed_at=datetime.fromtimestamp(
                snapshot.observed_seconds + snapshot.observed_microseconds / 1_000_000,
                tz=UTC,
            ),
            channel=StoredMessagingChannel(
                channel=channel,
                codec_id=snapshot.codec_id,
                limits=self._limits,
                retention_policy=self._retention_policy,
            ),
            stream=StoredMessagingStream(
                channel=channel,
                thread_id=identity.thread_id,
                generation=generation,
                disposition="active",
                latest_sequence=snapshot.latest_seq,
                payload_bytes=snapshot.payload_bytes,
                next_producer_fence=snapshot.fence_counter + 1,
                active_run_id=snapshot.active_run_id or None,
                retention_expired=False,
                message_sequence=snapshot.latest_seq,
                control_sequence=snapshot.signal_cursor,
            ),
            target_run=self._stored_run_from_snapshot(
                identity,
                generation,
                snapshot,
            ),
            active_run=None,
            matching_message=None,
        )

    def _stored_channel_from_values(
        self,
        channel: str,
        values: Mapping[str, bytes],
    ) -> StoredMessagingChannel | None:
        if not values:
            return None
        if self._hash_text(values, "channel") != channel:
            raise _redis_control._redis_protocol_error(
                "Redis channel identity conflicts with its storage scope"
            )
        expected_limits = {
            "max_message_payload_bytes": self._limits.max_message_payload_bytes,
            "max_checkpoint_bytes": self._limits.max_checkpoint_bytes,
            "max_thread_messages": self._limits.max_thread_messages,
            "max_thread_payload_bytes": self._limits.max_thread_payload_bytes,
            "max_total_bytes": self._limits.max_total_bytes,
            "max_total_records": self._limits.max_total_records,
        }
        for field, expected in expected_limits.items():
            if self._hash_integer(values, field) != expected:
                raise _redis_control._redis_protocol_error(
                    "Redis channel was opened with different MessagingLimits"
                )
        if self._hash_integer(values, "retention_ms") != self._retention_ms:
            raise _redis_control._redis_protocol_error(
                "Redis channel was opened with a different retention policy"
            )
        return StoredMessagingChannel(
            channel=channel,
            codec_id=self._hash_text(values, "codec"),
            limits=self._limits,
            retention_policy=self._retention_policy,
        )

    def _stored_run_from_values(
        self,
        identity: RunIdentity,
        *,
        generation: int,
        values: Mapping[str, bytes],
        lease_ttl_ms: int,
    ) -> StoredMessagingRun | None:
        if not values:
            return None
        if self._hash_text(values, "run") != identity.run_id:
            raise _redis_control._redis_protocol_error(
                "Redis run identity conflicts with its storage key"
            )
        status_text = self._hash_text(values, "status")
        if status_text not in {
            "running",
            "cancel_requested",
            "completed",
            "cancelled",
            "failed",
            "owner_lost",
        }:
            raise _redis_control._redis_protocol_error(
                "Redis Messaging run has an invalid status"
            )
        status = cast(RunStatus, status_text)
        checkpoint = None
        if self._hash_boolean(values, "checkpoint_present", default=False):
            checkpoint = RecoveryCheckpoint(
                position=bytes(values.get("checkpoint_position", b"")),
                last_message_id=(
                    self._hash_text(
                        values,
                        "checkpoint_message_id",
                        default="",
                    )
                    or None
                ),
            )
        failure_class = self._hash_text(values, "error_class", default="")
        failure_message = self._hash_text(values, "error_message", default="")
        local_failure = None
        if failure_class or failure_message:
            local_failure = self._remote_error_from_values(
                values,
                failure_class=failure_class,
                failure_message=failure_message,
            )
        producer_token = self._hash_text(values, "owner_token", default="") or None
        return StoredMessagingRun(
            identity=identity,
            generation=generation,
            start_sequence=self._hash_integer(values, "start_seq", default=0),
            end_sequence=self._hash_integer(values, "end_seq", default=0),
            status=status,
            publication_ready=self._snapshot_boolean(
                values.get("publication_ready", b""), field="publication_ready"
            ),
            publication_closed=self._snapshot_boolean(
                values.get("publication_closed", b""), field="publication_closed"
            ),
            settlement_started=self._hash_boolean(
                values,
                "settling",
                default=False,
            ),
            cancellable=self._hash_boolean(
                values,
                "cancellable",
                default=False,
            ),
            recoverable=self._hash_boolean(
                values,
                "recoverable",
                default=False,
            ),
            producer_token=producer_token,
            producer_fence=self._hash_integer(values, "fence", default=0),
            producer_lease_active=(
                lease_ttl_ms >= 0
                and producer_token is not None
                and not is_final_run_status(status)
            ),
            checkpoint=checkpoint,
            failure_class=failure_class,
            failure_message=failure_message,
            local_failure=local_failure,
            producer_lease_remaining_seconds=(
                lease_ttl_ms / 1000 if lease_ttl_ms >= 0 else None
            ),
        )

    def _remote_error_from_values(
        self,
        values: Mapping[str, bytes],
        *,
        failure_class: str,
        failure_message: str,
    ) -> RuntimeError:
        renew_count = self._hash_integer(values, "lease_renew_count", default=0)
        last_success_seconds = self._hash_integer(
            values,
            "lease_last_success_seconds",
            default=0,
        )
        last_success_microseconds = self._hash_integer(
            values,
            "lease_last_success_microseconds",
            default=0,
        )
        if last_success_microseconds >= 1_000_000:
            raise _redis_control._redis_protocol_error(
                "Redis run state has invalid lease success microseconds"
            )
        error_class = failure_class or "builtins.RuntimeError"
        message = failure_message or "remote producer failed"
        error = RuntimeError(f"{error_class}: {message}")
        error.add_note(
            "Redis lease evidence: "
            f"renew_count={renew_count}, "
            "last_success="
            f"{last_success_seconds}.{last_success_microseconds:06d} UTC"
        )
        return error

    def _matching_message_from_snapshot(
        self,
        query: MessagingStateQuery,
        *,
        target_values: Mapping[str, bytes],
        dedupe_value: _RedisScriptValue,
        message_value: _RedisScriptValue,
    ) -> StoredMessageEvidence | None:
        message_id = query.message_id
        dedupe_values = self._snapshot_hash(
            dedupe_value,
            field="idempotency evidence",
        )
        if message_id is None:
            if dedupe_values or message_value != []:
                raise _redis_control._redis_protocol_error(
                    "Redis state snapshot returned unrequested message evidence"
                )
            return None
        if not dedupe_values:
            if message_value != []:
                raise _redis_control._redis_protocol_error(
                    "Redis state snapshot returned a message without idempotency evidence"
                )
            return None
        sequence = self._hash_integer(dedupe_values, "seq")
        messages = self._snapshot_messages(
            message_value,
            channel=query.channel,
            identity=query.identity,
            after=sequence - 1,
            end_seq=sequence,
        )
        if len(messages) != 1:
            raise _redis_control._redis_protocol_error(
                "Redis idempotency evidence has no exact message"
            )
        envelope = messages[0]
        if envelope.seq != sequence or envelope.message_id != message_id:
            raise _redis_control._redis_protocol_error(
                "Redis idempotency evidence conflicts with its message"
            )
        checkpoint = None
        if self._hash_text(dedupe_values, "checkpoint_present") == "1":
            checkpoint = RecoveryCheckpoint(
                position=bytes(dedupe_values.get("checkpoint_position", b"")),
                last_message_id=self._hash_text(dedupe_values, "checkpoint_message_id")
                or None,
            )
        return StoredMessageEvidence(
            envelope=envelope,
            signature=self._hash_text(dedupe_values, "signature"),
            checkpoint=checkpoint,
        )

    def _validate_active_routing_keys(
        self,
        keys: _RedisKeys,
        *,
        active_run_key: str,
        active_lease_key: str,
    ) -> None:
        if bool(active_run_key) != bool(active_lease_key):
            raise _redis_control._redis_protocol_error(
                "Redis active run routing is incomplete"
            )
        if active_run_key and not active_run_key.startswith(
            f"{keys.generation_base}:run:"
        ):
            raise _redis_control._redis_protocol_error(
                "Redis active run key is outside its generation"
            )
        if active_lease_key and not active_lease_key.startswith(
            f"{keys.generation_base}:lease:"
        ):
            raise _redis_control._redis_protocol_error(
                "Redis active lease key is outside its generation"
            )

    def _snapshot_hash(
        self,
        value: _RedisScriptValue,
        *,
        field: str,
    ) -> dict[str, bytes]:
        if not isinstance(value, list) or len(value) % 2 != 0:
            raise _redis_control._redis_protocol_error(
                f"Redis state snapshot has malformed {field}"
            )
        result: dict[str, bytes] = {}
        for index in range(0, len(value), 2):
            key_bytes = self._snapshot_bytes(
                value[index],
                field=f"{field} name",
            )
            field_value = self._snapshot_bytes(
                value[index + 1],
                field=f"{field} value",
            )
            try:
                key = key_bytes.decode()
            except UnicodeDecodeError as error:
                raise _redis_control._redis_protocol_error(
                    f"Redis state snapshot has invalid {field} name",
                    cause=error,
                ) from error
            if key in result:
                raise _redis_control._redis_protocol_error(
                    f"Redis state snapshot has duplicate {field} fields"
                )
            result[key] = field_value
        return result

    def _snapshot_boolean(
        self,
        value: _RedisScriptValue,
        *,
        field: str,
    ) -> bool:
        text = self._snapshot_text(value, field=field)
        if text not in {"0", "1"}:
            raise _redis_control._redis_protocol_error(
                f"Redis state snapshot has invalid {field}"
            )
        return text == "1"

    def _hash_boolean(
        self,
        values: Mapping[str, bytes],
        field: str,
        *,
        default: bool,
    ) -> bool:
        text = self._hash_text(
            values,
            field,
            default="1" if default else "0",
        )
        if text not in {"0", "1"}:
            raise _redis_control._redis_protocol_error(
                f"Redis Messaging state has invalid {field}"
            )
        return text == "1"

    def _raw_hash(
        self,
        values: Mapping[bytes, bytes],
        *,
        field: str,
    ) -> dict[str, bytes]:
        try:
            return {self._text(key): bytes(value) for key, value in values.items()}
        except (TypeError, UnicodeDecodeError) as error:
            raise _redis_control._redis_protocol_error(
                f"Redis {field} has invalid fields",
                cause=error,
            ) from error

    def _hash_text(
        self,
        values: Mapping[str, bytes],
        field: str,
        *,
        default: str | None = None,
    ) -> str:
        value = values.get(field)
        if value is None:
            if default is None:
                raise _redis_control._redis_protocol_error(
                    f"Redis Messaging state is missing {field}"
                )
            return default
        try:
            return self._text(value)
        except UnicodeDecodeError as error:
            raise _redis_control._redis_protocol_error(
                f"Redis Messaging state has invalid {field}",
                cause=error,
            ) from error

    def _hash_integer(
        self,
        values: Mapping[str, bytes],
        field: str,
        *,
        default: int | None = None,
    ) -> int:
        value = values.get(field)
        if value is None:
            if default is None:
                raise _redis_control._redis_protocol_error(
                    f"Redis Messaging state is missing {field}"
                )
            return default
        try:
            parsed = int(self._text(value))
        except (UnicodeDecodeError, ValueError) as error:
            raise _redis_control._redis_protocol_error(
                f"Redis Messaging state has invalid {field}",
                cause=error,
            ) from error
        if parsed < 0:
            raise _redis_control._redis_protocol_error(
                f"Redis Messaging state has negative {field}"
            )
        return parsed

    def _scope(self, channel: str, identity: RunIdentity) -> _RedisStreamScope:
        return _redis_control._scope(
            self,
            channel,
            identity,
        )

    def _keys(
        self,
        channel: str,
        identity: RunIdentity,
        *,
        generation: int,
    ) -> _RedisKeys:
        return _redis_control._keys(
            self,
            channel,
            identity,
            generation=generation,
        )

    async def _read_control(
        self,
        scope: _RedisStreamScope,
    ) -> _StreamControl | None:
        return await _redis_control._read_control(
            self,
            scope,
        )

    async def _keys_for_handle(self, handle: _BackendRunHandle) -> _RedisKeys:
        return await _redis_control._keys_for_handle(
            self,
            handle,
        )

    async def _is_current_generation(self, keys: _RedisKeys) -> bool:
        return await _redis_control._is_current_generation(
            self,
            keys,
        )

    async def _run_snapshot(
        self,
        keys: _RedisKeys,
        identity: RunIdentity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Read one authoritative run state and optional bounded message page."""

        return await _redis_control._run_snapshot(
            self,
            keys,
            identity,
            after=after,
        )

    async def _settled_run_snapshot(
        self,
        keys: _RedisKeys,
        identity: RunIdentity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Settle one potentially mutating Lua snapshot before caller cancellation."""

        return await _redis_control._settled_run_snapshot(
            self,
            keys,
            identity,
            after=after,
        )

    def _wait_block_ms(self) -> int:
        """Bound the shared XREAD independently of each caller's lease deadline."""

        return _redis_control._wait_block_ms(self)

    @staticmethod
    def _socket_timeout_budget(value: object) -> int | None:
        """Return a positive XREAD budget below the Redis socket timeout."""

        return _redis_control._socket_timeout_budget(
            value,
        )

    @classmethod
    def _snapshot_integer(
        cls,
        value: _RedisScriptValue,
        *,
        field: str,
        minimum: int,
    ) -> int:
        """Decode one canonical integer from a snapshot scalar."""

        return _redis_control._snapshot_integer(
            cls,
            value,
            field=field,
            minimum=minimum,
        )

    @staticmethod
    def _snapshot_text(value: _RedisScriptValue, *, field: str) -> str:
        """Decode one UTF-8 scalar and reject nested or malformed responses."""

        return _redis_control._snapshot_text(
            value,
            field=field,
        )

    @staticmethod
    def _snapshot_bytes(value: _RedisScriptValue, *, field: str) -> bytes:
        """Return one binary scalar and reject nested snapshot structures."""

        return _redis_control._snapshot_bytes(
            value,
            field=field,
        )

    def _snapshot_messages(
        self,
        value: _RedisScriptValue,
        *,
        channel: str,
        identity: RunIdentity,
        after: int | None,
        end_seq: int,
    ) -> tuple[MessageEnvelope, ...]:
        """Decode the exact nested XRANGE representation returned through EVAL."""

        return _redis_journal._snapshot_messages(
            self,
            value,
            channel=channel,
            identity=identity,
            after=after,
            end_seq=end_seq,
        )

    @staticmethod
    def _raise_stream_deleted(
        handle: _BackendRunHandle,
        *,
        generation: int | None = None,
    ) -> Never:
        return _redis_control._raise_stream_deleted(
            handle,
            generation=generation,
        )

    def _decode_entry(
        self,
        channel: str,
        identity: RunIdentity,
        entry: tuple[bytes, Mapping[bytes, bytes]],
    ) -> MessageEnvelope:
        return _redis_journal._decode_entry(
            self,
            channel,
            identity,
            entry,
        )

    async def _eval(
        self,
        script: str,
        keys: Sequence[str],
        arguments: Sequence[str | bytes],
    ) -> list[bytes]:
        return await _redis_control._eval(
            self,
            script,
            keys,
            arguments,
        )

    @staticmethod
    def _message_signature(
        *,
        identity: RunIdentity,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None,
    ) -> str:
        return _redis_journal._message_signature(
            identity=identity,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _digest(value: str) -> str:
        return _redis_journal._digest(
            value,
        )

    @staticmethod
    def _text(value: bytes | str | int) -> str:
        return _redis_journal._text(
            value,
        )

    @staticmethod
    def _bytes(value: bytes | str | int) -> bytes:
        return _redis_journal._bytes(
            value,
        )

    @staticmethod
    def _qualified_name(value: BaseException) -> str:
        return _redis_journal._qualified_name(
            value,
        )

    @staticmethod
    def _remote_error(snapshot: _RunSnapshot) -> RuntimeError:
        return _redis_journal._remote_error(
            snapshot,
        )
