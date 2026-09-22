"""Redis control-plane, lease, deletion, and snapshot operations."""

from __future__ import annotations

__all__ = [
    "_eval",
    "_is_current_generation",
    "_keys",
    "_keys_for_handle",
    "_raise_stream_deleted",
    "_read_control",
    "_read_notifications",
    "_reconcile_current_run_status",
    "_run_snapshot",
    "_scope",
    "_settled_run_snapshot",
    "_snapshot_bytes",
    "_snapshot_integer",
    "_snapshot_text",
    "_socket_timeout_budget",
    "_wait_block_ms",
]

import asyncio
import math
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Never, Protocol, TypeAlias, TypeVar, cast
from uuid import uuid4

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity, thread_key
from ._messaging_ledger import BackendRunHandle
from ._redis_scripts import (
    _BEGIN_DELETE_SCRIPT,
    _BEGIN_EXPIRATION_SCRIPT,
    _BEGIN_SETTLEMENT_SCRIPT,
    _CANCEL_SCRIPT,
    _DELETE_BATCH_SCRIPT,
    _FINALIZE_DELETE_SCRIPT,
    _FINISH_SCRIPT,
    _READ_CONTROL_SCRIPT,
    _RENEW_SCRIPT,
    _RUN_SNAPSHOT_SCRIPT,
)
from ._tasks import (
    TaskOutcome,
    capture,
    join_owned_task,
    retain_failure,
    select_failure,
)
from .backend import (
    FinalRunStatus,
    RunStatus,
    is_final_run_status,
)
from .backend_contract import (
    MessagingCleanupReason,
    StreamGenerationPurge,
    StreamGenerationPurgeResult,
)
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    MessagingBackendProtocolError,
    MessagingBackendTimeout,
    MessagingBackendUnavailable,
    RunNotFound,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
    UnexpectedMessagingBackendError,
)
from .models import MessageEnvelope, RecoveryCheckpoint

if TYPE_CHECKING:
    from .redis import RedisBackend


_SNAPSHOT_PAGE_SIZE = 100
_MAX_WAIT_BLOCK_MS = 5_000
_SOCKET_TIMEOUT_SAFETY_RATIO = 0.9

_RedisScriptValue: TypeAlias = bytes | list["_RedisScriptValue"]
_RedisStreamEntry: TypeAlias = tuple[bytes, dict[bytes, bytes]]
_RedisStreamRead: TypeAlias = list[tuple[bytes, list[_RedisStreamEntry]]]
_RedisResultT = TypeVar("_RedisResultT")


async def _redis_call(
    operation: str,
    awaitable: Awaitable[_RedisResultT],
) -> _RedisResultT:
    """Translate transport failures while preserving newly requested cancellation.

    Client-safe errors expose only the logical operation. The concrete Redis failure is
    retained as trusted causal evidence and never serialized into a durable message.
    A driver may consume cancellation while settling a command; its result or failure
    must not let the cancelled caller continue into another read or lifecycle wait.
    Redis AbstractConnection.send_packed_command uses asyncio.wait_for, whose CPython
    3.11 implementation can return a completed write despite caller cancellation.
    """

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    try:
        try:
            result = await awaitable
        except Exception as error:
            if current is not None and current.cancelling() > cancel_count:
                raise asyncio.CancelledError from error
            raise
        else:
            if current is not None and current.cancelling() > cancel_count:
                raise asyncio.CancelledError
            return result
    except RedisTimeoutError as error:
        translated = MessagingBackendTimeout(
            f"Messaging backend {operation} timed out",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except (RedisConnectionError, OSError) as error:
        translated = MessagingBackendUnavailable(
            f"Messaging backend is unavailable for {operation}",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except RedisError as error:
        translated = UnexpectedMessagingBackendError(
            f"Messaging backend {operation} failed",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error


def _redis_protocol_error(
    detail: str,
    *,
    cause: BaseException | None = None,
) -> MessagingBackendProtocolError:
    """Separate a provider-neutral message from trusted Redis diagnostics."""

    return MessagingBackendProtocolError(
        "Messaging backend returned an invalid protocol response",
        diagnostic_context={
            "implementation": "redis",
            "operation": "protocol_validation",
            "detail": detail,
        },
        cause=cause,
    )


class _RedisConnection(Protocol):
    """Describe the exclusively pinned connection's wait budget and disconnect."""

    socket_timeout: float | None

    async def disconnect(self, *, nowait: bool = False) -> None: ...


class _AsyncRedisClient(Protocol):
    """Describe the Redis asyncio surface used after runtime client validation.

    Redis 6 and 7 annotate several asyncio commands as a union of synchronous and
    awaitable results. The actual ``redis.asyncio.Redis`` client always returns the
    awaitable branch; this protocol records that runtime boundary without requiring
    Redis 8-only response aliases.
    """

    connection: _RedisConnection | None

    def get_connection_kwargs(self) -> dict[str, object]: ...

    def client(self) -> _AsyncRedisClient: ...

    async def initialize(self) -> _AsyncRedisClient: ...

    async def aclose(self) -> None: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes,
    ) -> object: ...

    async def hget(self, name: str, key: str) -> bytes | None: ...

    async def get(self, name: str) -> bytes | None: ...

    async def hgetall(self, name: str) -> dict[bytes, bytes]: ...

    async def time(self) -> tuple[int, int]: ...

    async def exists(self, *names: str) -> int: ...

    async def srandmember(
        self,
        name: str,
        number: int | None = None,
    ) -> bytes | list[bytes] | None: ...

    async def xrange(
        self,
        name: str,
        min: str,
        max: str,
        count: int | None = None,
    ) -> list[_RedisStreamEntry]: ...

    async def xread(
        self,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> _RedisStreamRead: ...


_ControlState = Literal[
    "active",
    "deleting",
    "deleted",
    "expiring",
    "expired",
]


@dataclass(frozen=True, slots=True)
class _RedisStreamScope:
    """Name the persistent keys shared by every generation of one stream."""

    channel_meta: str
    control: str
    delete_lease: str
    base: str
    stream_base: str


@dataclass(frozen=True, slots=True)
class _RedisKeys:
    """Name the shared and generation-private keys for one run lookup."""

    channel: str
    identity: RunIdentity
    channel_meta: str
    control: str
    delete_lease: str
    meta: str
    run_key: str
    lease_key: str
    messages: str
    index: str
    tombstone: str
    base: str
    stream_base: str
    generation_base: str
    generation: int


@dataclass(frozen=True, slots=True)
class _StreamControl:
    """Describe the authoritative generation and deletion state."""

    generation: int
    state: _ControlState


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """Hold one generation-fenced run view returned by a single Redis script."""

    status: RunStatus
    end_seq: int
    error_class: str
    error_message: str
    signal_cursor: int
    lease_ttl_ms: int
    lease_renew_count: int
    lease_last_success_seconds: int
    lease_last_success_microseconds: int
    messages: tuple[MessageEnvelope, ...]
    observed_seconds: int
    observed_microseconds: int
    start_seq: int
    settling: bool
    publication_closed: bool
    publication_ready: bool
    cancellable: bool
    recoverable: bool
    owner_token: str
    fence: int
    checkpoint: RecoveryCheckpoint | None
    active_run_id: str
    latest_seq: int
    payload_bytes: int
    fence_counter: int
    codec_id: str
    max_message_payload_bytes: int
    max_checkpoint_bytes: int
    max_thread_messages: int
    max_thread_payload_bytes: int
    retention_ms: int

    @property
    def terminal(self) -> bool:
        """Return whether the snapshot fixes the run's final message boundary."""

        return is_final_run_status(self.status)


async def begin_settlement(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Atomically choose an accepted cancellation or ordinary settlement."""

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        raise BackendOwnershipLost(
            f"Run {handle.identity.run_id!r} has no complete producer ownership "
            "identity"
        )
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    response = await self._eval(
        _BEGIN_SETTLEMENT_SCRIPT,
        [keys.control, keys.run_key, keys.lease_key],
        [str(generation), handle.owner_token, str(handle.fence)],
    )
    code = self._text(response[0])
    if code == "CANCEL_REQUESTED":
        return True
    if code == "SETTLING":
        return False
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        raise BackendOwnershipLost(
            f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
        )
    raise _redis_protocol_error(f"unexpected Redis settlement response: {code}")


async def finish(
    self: RedisBackend,
    handle: BackendRunHandle,
    *,
    status: FinalRunStatus,
    error: BaseException | None = None,
) -> None:
    """Commit one terminal status only for the current generation and fence owner.

    The Lua boundary verifies generation, owner token, and monotonic fence together, so
    a stale producer cannot overwrite a replacement owner's result. Failure class and
    message are operational diagnostics, not user Trace facts.

    Args:
        self: Redis Backend owning the current producer lease.
        handle: Exact generation, owner token, and monotonic fence.
        status: Authoritative terminal producer outcome.
        error: Optional trusted producer failure retained as bounded diagnostics.

    Raises:
        BackendOwnershipLost: The producer no longer owns the exact generation.
        MessagingError: Redis cannot commit or prove the terminal transition.
    """

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        raise BackendOwnershipLost(
            f"Run {handle.identity.run_id!r} has no complete producer ownership "
            "identity"
        )
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    error_class = "" if error is None else self._qualified_name(error)
    error_message = "" if error is None else str(error)
    response = await self._eval(
        _FINISH_SCRIPT,
        [
            keys.control,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            self._expirations_key,
            self._notifications_key,
            self._notification_counter_key,
        ],
        [
            str(generation),
            handle.owner_token,
            str(handle.fence),
            status,
            error_class,
            error_message,
            str(self._retention_ms),
        ],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        raise BackendOwnershipLost(
            f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
        )


async def request_cancel(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Record one idempotent cancellation request for a cancellable active run."""

    keys = await self._keys_for_handle(handle)
    await self._settled_run_snapshot(keys, handle.identity)
    response = await self._eval(
        _CANCEL_SCRIPT,
        [
            keys.control,
            keys.run_key,
            self._notifications_key,
            self._notification_counter_key,
        ],
        [str(keys.generation)],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle, generation=keys.generation)
    if code == "NOT_FOUND":
        raise RunNotFound(identity=handle.identity)
    if code == "UNSUPPORTED":
        raise CancellationUnsupported(identity=handle.identity)
    if code in {"FINAL", "DUPLICATE"}:
        return False
    if code != "REQUESTED":
        raise _redis_protocol_error(f"unexpected Redis cancel response: {code}")
    return True


async def _reconcile_current_run_status(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
) -> RunStatus:
    """Reconcile and return current status through the atomic Redis snapshot."""

    required_identifier("channel", channel)
    required_identity(identity)
    scope = self._scope(channel, identity)
    while True:
        control = await self._read_control(scope)
        if control is not None and control.state == "expired":
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=control.generation,
            )
        if control is None or control.state != "active":
            raise RunNotFound(identity=identity)
        keys = self._keys(
            channel,
            identity,
            generation=control.generation,
        )
        try:
            snapshot = await self._settled_run_snapshot(keys, identity)
        except StreamDeleted:
            # RunIdentity-only lookups follow the current generation. A stale bound
            # handle still receives StreamDeleted through _keys_for_handle().
            continue
        return snapshot.status


async def renew(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Renew a lease only while its complete fencing identity still matches."""

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        return False
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    expected = f"{handle.owner_token}:{handle.fence}"
    response = await self._eval(
        _RENEW_SCRIPT,
        [keys.control, keys.run_key, keys.lease_key],
        [str(generation), expected, str(self._lease_ms)],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        return False
    if code != "RENEWED" or len(response) != 4:
        raise _redis_protocol_error(f"unexpected Redis renew response: {code}")
    self._snapshot_integer(
        response[1],
        field="lease renew count",
        minimum=1,
    )
    self._snapshot_integer(
        response[2],
        field="lease last success seconds",
        minimum=0,
    )
    self._snapshot_integer(
        response[3],
        field="lease last success microseconds",
        minimum=0,
    )
    return True


async def _generation_tombstone_reason(
    self: RedisBackend,
    keys: _RedisKeys,
) -> MessagingCleanupReason | None:
    value = await _redis_call(
        "generation cleanup tombstone lookup",
        self._client.get(keys.tombstone),
    )
    if value is None:
        return None
    reason = self._text(value)
    if reason not in {"deleted", "expired"}:
        raise _redis_protocol_error("Redis generation cleanup tombstone is invalid")
    return cast(MessagingCleanupReason, reason)


async def _claim_generation_cleanup(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
    generation: int,
    reason: MessagingCleanupReason,
    owner_token: str | None = None,
) -> str | None:
    """Acquire or join one exact Redis cleanup lease without deleting records."""

    keys = self._keys(channel, identity, generation=generation)
    resolved_owner_token = (
        f"{self._worker_id}:cleanup:{uuid4().hex}"
        if owner_token is None
        else owner_token
    )
    while True:
        if reason == "deleted":
            active_lease = await _redis_call(
                "active lease lookup",
                self._client.hget(keys.meta, "active_lease"),
            )
            expected_active_lease = (
                "" if active_lease is None else self._text(active_lease)
            )
            active_lease_key = (
                f"{keys.generation_base}:no-active-lease"
                if not expected_active_lease
                else expected_active_lease
            )
            response = await self._eval(
                _BEGIN_DELETE_SCRIPT,
                [
                    keys.control,
                    keys.meta,
                    keys.delete_lease,
                    active_lease_key,
                    self._notifications_key,
                    self._notification_counter_key,
                ],
                [
                    str(generation),
                    resolved_owner_token,
                    str(self._lease_ms),
                    expected_active_lease,
                ],
            )
        else:
            response = await self._eval(
                _BEGIN_EXPIRATION_SCRIPT,
                [keys.control, keys.delete_lease],
                [str(generation), resolved_owner_token, str(self._lease_ms)],
            )
        code = self._text(response[0])
        if code == "OWNED":
            return resolved_owner_token
        if code == "ACTIVE":
            raise StreamDeleteConflict(
                channel=channel,
                identity=identity,
                active_identity=RunIdentity(
                    namespace=identity.namespace,
                    thread_id=identity.thread_id,
                    run_id=self._text(response[1]),
                ),
            )
        if code == "WAIT":
            await asyncio.sleep(self._poll_interval)
            continue
        if code in {"DONE", "RETRY"}:
            return None
        if code == "INVALID_CONTROL_STATE":
            raise _redis_protocol_error(
                f"Redis stream control has invalid state: {self._text(response[1])!r}"
            )
        raise _redis_protocol_error(f"unexpected Redis cleanup claim response: {code}")


async def _begin_generation_cleanup(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
    requested_reason: MessagingCleanupReason,
) -> tuple[
    int | None,
    MessagingCleanupReason | None,
    str | None,
    bool,
]:
    """Seal one current generation and return its exact physical cleanup work."""

    required_identifier("channel", channel)
    required_identity(identity)
    if requested_reason not in {"deleted", "expired"}:
        raise TypeError("requested_reason must be deleted or expired")
    scope = self._scope(channel, identity)
    while True:
        control = await self._read_control(scope)
        if control is None:
            return None, None, None, False
        if control.state in {"deleted", "expired"}:
            return (
                control.generation,
                cast(MessagingCleanupReason, control.state),
                None,
                False,
            )
        if control.state == "active" and requested_reason == "expired":
            return None, None, None, False
        actual_reason: MessagingCleanupReason = (
            "expired" if control.state == "expiring" else "deleted"
        )
        cleanup_token = await _claim_generation_cleanup(
            self,
            channel=channel,
            identity=identity,
            generation=control.generation,
            reason=actual_reason,
        )
        if cleanup_token is not None:
            return control.generation, actual_reason, cleanup_token, True


async def _purge_stream_generation(
    self: RedisBackend,
    purge: StreamGenerationPurge,
) -> StreamGenerationPurgeResult:
    """Remove at most one requested Redis cleanup batch under an exact lease."""

    required_identifier("channel", purge.channel)
    required_identity(purge.identity)
    if isinstance(purge.generation, bool) or not isinstance(purge.generation, int):
        raise TypeError("generation must be an integer")
    if purge.generation < 1:
        raise ValueError("generation must be positive")
    if isinstance(purge.maximum_records, bool) or not isinstance(
        purge.maximum_records,
        int,
    ):
        raise TypeError("maximum_records must be an integer")
    if purge.maximum_records < 1:
        raise ValueError("maximum_records must be positive")
    cleanup_token = purge.cleanup_token
    if not isinstance(cleanup_token, str) or not cleanup_token:
        raise TypeError("cleanup_token must be a non-empty string")
    scope = self._scope(purge.channel, purge.identity)
    keys = self._keys(
        purge.channel,
        purge.identity,
        generation=purge.generation,
    )
    while True:
        control = await self._read_control(scope)
        if control is None or control.generation != purge.generation:
            tombstone = await _generation_tombstone_reason(self, keys)
            if tombstone is not None:
                return StreamGenerationPurgeResult(
                    removed_records=0,
                    complete=True,
                )
            raise _redis_protocol_error(
                "Redis cleanup generation is neither current nor tombstoned"
            )
        if control.state in {"deleted", "expired"}:
            return StreamGenerationPurgeResult(
                removed_records=0,
                complete=True,
            )
        if control.state == "active":
            active_run = await _redis_call(
                "active cleanup conflict lookup",
                self._client.hget(keys.meta, "active_run"),
            )
            raise StreamDeleteConflict(
                channel=purge.channel,
                identity=purge.identity,
                active_identity=RunIdentity(
                    namespace=purge.identity.namespace,
                    thread_id=purge.identity.thread_id,
                    run_id=(
                        purge.identity.run_id
                        if active_run is None
                        else self._text(active_run)
                    ),
                ),
            )
        reason: MessagingCleanupReason = (
            "expired" if control.state == "expiring" else "deleted"
        )
        claimed_token = await _claim_generation_cleanup(
            self,
            channel=purge.channel,
            identity=purge.identity,
            generation=purge.generation,
            reason=reason,
            owner_token=cleanup_token,
        )
        if claimed_token is None:
            continue
        raw_members = await _redis_call(
            "stream index read",
            self._client.srandmember(
                keys.index,
                number=purge.maximum_records,
            ),
        )
        members = tuple(
            self._text(member)
            for member in cast(Sequence[bytes | str], raw_members or ())
        )
        if not members:
            return StreamGenerationPurgeResult(
                removed_records=0,
                complete=True,
            )
        response = await self._eval(
            _DELETE_BATCH_SCRIPT,
            [keys.control, keys.delete_lease, keys.index, *members],
            [
                str(keys.generation),
                claimed_token,
                str(self._lease_ms),
                "expiring" if reason == "expired" else "deleting",
            ],
        )
        code = self._text(response[0])
        if code == "OK" and len(response) == 2:
            remaining = self._snapshot_integer(
                response[1],
                field="cleanup records remaining",
                minimum=0,
            )
            return StreamGenerationPurgeResult(
                removed_records=len(members),
                complete=remaining == 0,
            )
        if code in {"RETRY", "LEASE_LOST"}:
            continue
        raise _redis_protocol_error(f"unexpected Redis delete batch response: {code}")


async def _finish_generation_cleanup(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
    generation: int,
    reason: MessagingCleanupReason,
    cleanup_token: str | None,
) -> None:
    """Finalize one empty Redis generation and retain its exact tombstone."""

    required_identifier("channel", channel)
    required_identity(identity)
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError("generation must be an integer")
    if generation < 1:
        raise ValueError("generation must be positive")
    if reason not in {"deleted", "expired"}:
        raise TypeError("reason must be deleted or expired")
    if not isinstance(cleanup_token, str) or not cleanup_token:
        raise TypeError("cleanup_token must be a non-empty string")
    scope = self._scope(channel, identity)
    keys = self._keys(channel, identity, generation=generation)
    while True:
        control = await self._read_control(scope)
        if control is None or control.generation != generation:
            tombstone = await _generation_tombstone_reason(self, keys)
            if tombstone == reason:
                return
            if tombstone is not None:
                raise _redis_protocol_error(
                    "Redis generation was finalized for a different cleanup reason"
                )
            raise _redis_protocol_error(
                "Redis cleanup generation is neither current nor tombstoned"
            )
        if control.state in {"deleted", "expired"}:
            if control.state != reason:
                raise _redis_protocol_error(
                    "Redis generation was finalized for a different cleanup reason"
                )
            return
        expected_state = "expiring" if reason == "expired" else "deleting"
        if control.state != expected_state:
            raise _redis_protocol_error(
                "Redis generation is not sealed for the requested cleanup"
            )
        claimed_token = await _claim_generation_cleanup(
            self,
            channel=channel,
            identity=identity,
            generation=generation,
            reason=reason,
            owner_token=cleanup_token,
        )
        if claimed_token is None:
            continue
        response = await self._eval(
            _FINALIZE_DELETE_SCRIPT,
            [
                keys.control,
                keys.delete_lease,
                keys.index,
                keys.tombstone,
                self._capacity_key,
                self._expirations_key,
            ],
            [str(generation), claimed_token, expected_state, reason],
        )
        code = self._text(response[0])
        if code == "DONE":
            return
        if code == "MORE":
            raise _redis_protocol_error(
                "Redis generation cleanup was finalized before purge completed"
            )
        if code in {"RETRY", "LEASE_LOST"}:
            continue
        raise _redis_protocol_error(
            f"unexpected Redis delete finalization response: {code}"
        )


async def complete_generation_cleanup(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
    requested_reason: MessagingCleanupReason,
) -> None:
    """Resume a raced cleanup through the same split Backend operations."""

    (
        generation,
        reason,
        cleanup_token,
        cleanup_required,
    ) = await _begin_generation_cleanup(
        self,
        channel=channel,
        identity=identity,
        requested_reason=requested_reason,
    )
    if not cleanup_required:
        return
    if generation is None or reason is None or cleanup_token is None:
        raise _redis_protocol_error(
            "Redis cleanup begin returned incomplete ownership evidence"
        )
    while True:
        progress = await _purge_stream_generation(
            self,
            StreamGenerationPurge(
                channel=channel,
                identity=identity,
                generation=generation,
                maximum_records=64,
                cleanup_token=cleanup_token,
            ),
        )
        if progress.complete:
            break
    await _finish_generation_cleanup(
        self,
        channel=channel,
        identity=identity,
        generation=generation,
        reason=reason,
        cleanup_token=cleanup_token,
    )


def _scope(
    self: RedisBackend, channel: str, identity: RunIdentity
) -> _RedisStreamScope:
    channel_scope = self._digest(channel)
    base = f"{self._namespace}:channel:{channel_scope}"
    stream_digest = self._digest(thread_key(identity))
    stream_base = f"{base}:stream:{stream_digest}"
    return _RedisStreamScope(
        channel_meta=f"{base}:channel",
        control=f"{stream_base}:control",
        delete_lease=f"{stream_base}:delete-lease",
        base=base,
        stream_base=stream_base,
    )


def _keys(
    self: RedisBackend,
    channel: str,
    identity: RunIdentity,
    *,
    generation: int,
) -> _RedisKeys:
    """Derive generation-scoped keys under one Redis Cluster hash slot.

    The prefix selects one shared hash tag for atomic total-capacity accounting.
    Channel, thread, and run digests isolate identities inside that slot.
    """

    scope = self._scope(channel, identity)
    generation_base = f"{scope.stream_base}:generation:{generation}"
    run_digest = self._digest(identity.run_id)
    return _RedisKeys(
        channel=channel,
        identity=identity,
        channel_meta=scope.channel_meta,
        control=scope.control,
        delete_lease=scope.delete_lease,
        meta=f"{generation_base}:meta",
        run_key=f"{generation_base}:run:{run_digest}",
        lease_key=f"{generation_base}:lease:{run_digest}",
        messages=f"{generation_base}:messages",
        index=f"{generation_base}:index",
        tombstone=f"{generation_base}:tombstone",
        base=scope.base,
        stream_base=scope.stream_base,
        generation_base=generation_base,
        generation=generation,
    )


async def _read_control(
    self: RedisBackend,
    scope: _RedisStreamScope,
) -> _StreamControl | None:
    """Read and classify the current generation without physical cleanup."""

    while True:
        raw_response = await _redis_call(
            "stream control read",
            self._client.eval(
                _READ_CONTROL_SCRIPT,
                1,
                scope.control,
            ),
        )
        if not isinstance(raw_response, list):
            raise _redis_protocol_error(
                "Redis stream control returned a non-list response"
            )
        response = cast(list[_RedisScriptValue], raw_response)
        if not response:
            raise _redis_protocol_error("Redis stream control returned no fields")
        code = self._snapshot_text(response[0], field="control response code")
        if code == "NONE":
            return None
        if code != "OK" or len(response) != 3:
            raise _redis_protocol_error(f"unexpected Redis control response: {code}")
        try:
            generation = int(
                self._snapshot_text(response[1], field="control generation")
            )
        except ValueError as error:
            raise _redis_protocol_error(
                "Redis stream control has invalid generation",
                cause=error,
            ) from error
        if generation < 1:
            raise _redis_protocol_error("Redis stream control has invalid generation")
        state = self._snapshot_text(response[2], field="control state")
        if state not in {"active", "deleting", "deleted", "expiring", "expired"}:
            raise _redis_protocol_error(
                f"Redis stream control has invalid state: {state!r}"
            )
        return _StreamControl(
            generation=generation,
            state=cast(_ControlState, state),
        )


async def _keys_for_handle(self: RedisBackend, handle: BackendRunHandle) -> _RedisKeys:
    generation = handle.generation
    if generation is None:
        scope = self._scope(handle.channel, handle.identity)
        control = await self._read_control(scope)
        if control is None:
            raise RunNotFound(identity=handle.identity)
        if control.state != "active":
            await _raise_generation_unavailable(
                self,
                handle,
                generation=control.generation,
            )
        generation = control.generation
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    if handle.generation is not None:
        reason = await _redis_call(
            "generation tombstone lookup",
            self._client.get(keys.tombstone),
        )
        if reason is not None:
            if self._text(reason) == "expired":
                raise StreamExpired(
                    channel=handle.channel,
                    identity=handle.identity,
                    generation=generation,
                )
            self._raise_stream_deleted(handle, generation=generation)
    return keys


async def _raise_generation_unavailable(
    self: RedisBackend,
    handle: BackendRunHandle,
    *,
    generation: int,
) -> Never:
    """Distinguish retention expiry from explicit deletion for a stale handle."""

    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    reason = await _redis_call(
        "generation tombstone lookup",
        self._client.get(keys.tombstone),
    )
    if reason is not None and self._text(reason) == "expired":
        raise StreamExpired(
            channel=handle.channel,
            identity=handle.identity,
            generation=generation,
        )
    self._raise_stream_deleted(handle, generation=generation)


async def _is_current_generation(self: RedisBackend, keys: _RedisKeys) -> bool:
    control = await self._read_control(
        _RedisStreamScope(
            channel_meta=keys.channel_meta,
            control=keys.control,
            delete_lease=keys.delete_lease,
            base=keys.base,
            stream_base=keys.stream_base,
        )
    )
    return (
        control is not None
        and control.state == "active"
        and control.generation == keys.generation
    )


async def _run_snapshot(
    self: RedisBackend,
    keys: _RedisKeys,
    identity: RunIdentity,
    *,
    after: int | None = None,
) -> _RunSnapshot:
    """Read one authoritative run state and optional bounded message page."""

    raw_response = await _redis_call(
        "run snapshot",
        self._client.eval(
            _RUN_SNAPSHOT_SCRIPT,
            9,
            keys.control,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            keys.messages,
            keys.channel_meta,
            self._expirations_key,
            self._notifications_key,
            self._notification_counter_key,
            str(keys.generation),
            "__none__" if after is None else str(after),
            str(self._retention_ms),
        ),
    )
    if not isinstance(raw_response, list):
        raise _redis_protocol_error("Redis run snapshot returned a non-list response")
    response = cast(list[_RedisScriptValue], raw_response)
    if not response:
        raise _redis_protocol_error("Redis run snapshot returned an empty response")
    code = self._snapshot_text(response[0], field="response code")
    if code == "STREAM_DELETED":
        raise StreamDeleted(
            channel=keys.channel,
            identity=keys.identity,
            generation=keys.generation,
        )
    if code == "NOT_FOUND":
        raise RunNotFound(identity=identity)
    if code == "INVALID_STATUS":
        status = (
            self._snapshot_text(response[1], field="invalid status")
            if len(response) > 1
            else ""
        )
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid status: {status!r}"
        )
    if code == "INVALID_BOUNDARY":
        raise _redis_protocol_error(
            "Redis run snapshot has an invalid message boundary"
        )
    if code != "OK" or len(response) != 34:
        raise _redis_protocol_error(f"unexpected Redis run snapshot response: {code}")

    status_text = self._snapshot_text(response[1], field="status")
    if status_text not in {
        "running",
        "cancel_requested",
        "completed",
        "cancelled",
        "failed",
        "owner_lost",
    }:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid status: {status_text!r}"
        )
    end_seq = self._snapshot_integer(
        response[2],
        field="end_seq",
        minimum=0,
    )
    error_class = self._snapshot_text(response[3], field="error_class")
    error_message = self._snapshot_text(response[4], field="error_message")
    signal_cursor = self._snapshot_integer(
        response[5],
        field="signal cursor",
        minimum=0,
    )
    lease_ttl_ms = self._snapshot_integer(
        response[6],
        field="lease TTL",
        minimum=-2,
    )
    lease_renew_count = self._snapshot_integer(
        response[7],
        field="lease renew count",
        minimum=0,
    )
    lease_last_success_seconds = self._snapshot_integer(
        response[8],
        field="lease last success seconds",
        minimum=0,
    )
    lease_last_success_microseconds = self._snapshot_integer(
        response[9],
        field="lease last success microseconds",
        minimum=0,
    )
    messages = self._snapshot_messages(
        response[10],
        channel=keys.channel,
        identity=keys.identity,
        after=after,
        end_seq=end_seq,
    )
    observed_seconds = self._snapshot_integer(
        response[11],
        field="observed seconds",
        minimum=0,
    )
    observed_microseconds = self._snapshot_integer(
        response[12],
        field="observed microseconds",
        minimum=0,
    )
    start_seq = self._snapshot_integer(
        response[13],
        field="start_seq",
        minimum=0,
    )
    settling = _snapshot_boolean(self, response[14], field="settling")
    cancellable = _snapshot_boolean(self, response[15], field="cancellable")
    recoverable = _snapshot_boolean(self, response[16], field="recoverable")
    owner_token = self._snapshot_text(response[17], field="owner token")
    fence = self._snapshot_integer(response[18], field="fence", minimum=0)
    checkpoint_present = _snapshot_boolean(
        self,
        response[19],
        field="checkpoint presence",
    )
    checkpoint = None
    checkpoint_position = self._snapshot_bytes(
        response[20],
        field="checkpoint position",
    )
    checkpoint_message_id = self._snapshot_text(
        response[21],
        field="checkpoint message ID",
    )
    if checkpoint_present:
        checkpoint = RecoveryCheckpoint(
            position=checkpoint_position,
            last_message_id=checkpoint_message_id or None,
        )
    active_run_id = self._snapshot_text(response[22], field="active run ID")
    latest_seq = self._snapshot_integer(
        response[23],
        field="latest sequence",
        minimum=0,
    )
    payload_bytes = self._snapshot_integer(
        response[24],
        field="payload bytes",
        minimum=0,
    )
    fence_counter = self._snapshot_integer(
        response[25],
        field="fence counter",
        minimum=0,
    )
    codec_id = self._snapshot_text(response[26], field="codec ID")
    max_message_payload_bytes = self._snapshot_integer(
        response[27],
        field="maximum message payload bytes",
        minimum=1,
    )
    max_checkpoint_bytes = self._snapshot_integer(
        response[28],
        field="maximum checkpoint bytes",
        minimum=1,
    )
    max_thread_messages = self._snapshot_integer(
        response[29],
        field="maximum thread messages",
        minimum=1,
    )
    max_thread_payload_bytes = self._snapshot_integer(
        response[30],
        field="maximum thread payload bytes",
        minimum=1,
    )
    retention_ms = self._snapshot_integer(
        response[31],
        field="retention milliseconds",
        minimum=0,
    )
    return _RunSnapshot(
        status=cast(RunStatus, status_text),
        end_seq=end_seq,
        error_class=error_class,
        error_message=error_message,
        signal_cursor=signal_cursor,
        lease_ttl_ms=lease_ttl_ms,
        lease_renew_count=lease_renew_count,
        lease_last_success_seconds=lease_last_success_seconds,
        lease_last_success_microseconds=lease_last_success_microseconds,
        messages=messages,
        observed_seconds=observed_seconds,
        observed_microseconds=observed_microseconds,
        start_seq=start_seq,
        settling=settling,
        publication_ready=_snapshot_boolean(
            self, response[33], field="publication_ready"
        ),
        publication_closed=_snapshot_boolean(
            self, response[32], field="publication_closed"
        ),
        cancellable=cancellable,
        recoverable=recoverable,
        owner_token=owner_token,
        fence=fence,
        checkpoint=checkpoint,
        active_run_id=active_run_id,
        latest_seq=latest_seq,
        payload_bytes=payload_bytes,
        fence_counter=fence_counter,
        codec_id=codec_id,
        max_message_payload_bytes=max_message_payload_bytes,
        max_checkpoint_bytes=max_checkpoint_bytes,
        max_thread_messages=max_thread_messages,
        max_thread_payload_bytes=max_thread_payload_bytes,
        retention_ms=retention_ms,
    )


async def _settled_run_snapshot(
    self: RedisBackend,
    keys: _RedisKeys,
    identity: RunIdentity,
    *,
    after: int | None = None,
) -> _RunSnapshot:
    """Settle one potentially mutating Lua snapshot before caller cancellation."""

    snapshot_task = asyncio.create_task(
        self._run_snapshot(keys, identity, after=after),
        name=f"tinkerfin-messaging-redis-snapshot:{identity.run_id}",
    )
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not snapshot_task.done():
        try:
            await asyncio.shield(snapshot_task)
        except asyncio.CancelledError as cancellation:
            next_cancel_count = current.cancelling() if current is not None else 0
            if next_cancel_count > cancel_count:
                if caller_cancellation is None:
                    caller_cancellation = cancellation
                cancel_count = next_cancel_count
                continue
            if snapshot_task.done():
                break
            raise
        except BaseException:
            if snapshot_task.done():
                break
            raise

    snapshot_error: BaseException | None = None
    snapshot: _RunSnapshot | None = None
    try:
        snapshot = snapshot_task.result()
    except BaseException as error:  # noqa: BLE001 - preserve Redis outcome
        snapshot_error = error
    if caller_cancellation is not None:
        if snapshot_error is not None:
            caller_cancellation.add_note(
                "Redis run snapshot also failed during cancellation: "
                f"{type(snapshot_error).__name__}: {snapshot_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if snapshot_error is not None:
        raise snapshot_error.with_traceback(snapshot_error.__traceback__)
    assert snapshot is not None
    return snapshot


async def _read_notifications(
    self: RedisBackend,
    *,
    after: int,
) -> _RedisStreamRead:
    """Read one bounded metadata batch with cancellation-safe connection ownership."""

    blocking_client = self._client.client()
    closing = False

    # The task owns the pinned client from initialization through close. HTTP response
    # cancellation can abandon a nested async-generator await before its outer finally
    # resumes; keeping cleanup inside the loop-owned task prevents that transport race
    # from leaking the connection. The caller still cancels and joins the task normally.
    async def read() -> _RedisStreamRead:
        nonlocal closing
        read_error: BaseException | None = None
        try:
            await _redis_call(
                "blocking client initialization",
                blocking_client.initialize(),
            )
            connection = blocking_client.connection
            assert connection is not None
            socket_timeout = connection.socket_timeout
            # This pinned connection is exclusive until aclose returns it to the
            # borrowed pool. Its socket timeout bounds the whole XREAD, including
            # retries, so Redis cannot consume cancellation in CPython 3.11 wait_for.
            connection.socket_timeout = None
            try:
                async with asyncio.timeout(socket_timeout):
                    return await _redis_call(
                        "stream wait",
                        blocking_client.xread(
                            {self._notifications_key: f"{after}-0"},
                            count=128,
                            block=self._wait_block_ms(),
                        ),
                    )
            except TimeoutError as error:
                raise MessagingBackendTimeout(
                    "Messaging backend stream wait timed out",
                    diagnostic_context={
                        "implementation": "redis",
                        "operation": "stream wait",
                    },
                    cause=error,
                ) from error
            finally:
                connection.socket_timeout = socket_timeout
        except BaseException as error:
            read_error = error
            raise
        finally:
            closing = True
            try:
                await _redis_call("blocking client close", blocking_client.aclose())
            except BaseException as error:
                if read_error is None:
                    raise
                current = asyncio.current_task()
                if (
                    isinstance(read_error, asyncio.CancelledError)
                    and current is not None
                    and current.cancelling()
                ):
                    # Only this reader's owner can stop this pinned read. Its stop
                    # request has completed; an independent close failure still
                    # needs to reach that owner as an ordinary or control failure.
                    failure = error
                    retain_failure(failure, read_error)
                else:
                    failure = select_failure(read_error, error)
                failure.add_note(
                    f"Redis blocking client close also failed: {type(error).__name__}: {error}"
                )
                raise failure

    async def captured_read() -> TaskOutcome[_RedisStreamRead]:
        return await capture(read())

    read_task = asyncio.create_task(
        captured_read(),
        name="tinkerfin-messaging-redis-xread",
    )
    owner_task = asyncio.current_task()

    def cancel_when_owner_finishes(_task: asyncio.Task[object]) -> None:
        if not closing and not read_task.done() and read_task.cancelling() == 0:
            read_task.cancel()

    def read_finished(task: asyncio.Task[TaskOutcome[_RedisStreamRead]]) -> None:
        if owner_task is not None:
            owner_task.remove_done_callback(cancel_when_owner_finishes)
        if not task.cancelled():
            task.exception()

    if owner_task is not None:
        owner_task.add_done_callback(cancel_when_owner_finishes)
    read_task.add_done_callback(read_finished)
    caller_error: BaseException | None = None
    try:
        outcome = await asyncio.shield(read_task)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome.value
    except BaseException as error:
        caller_error = error
        raise
    finally:
        if not read_task.done() and not closing and not read_task.cancelling():
            read_task.cancel()
        try:
            await join_owned_task(read_task)
        except BaseException as error:  # noqa: BLE001 - retain the captured control outcome
            outcome = None if read_task.cancelled() else read_task.result()
            if isinstance(caller_error, asyncio.CancelledError) and isinstance(
                outcome, BaseException
            ):
                # This boundary only forwards the shared reader's own stop. Keep
                # the completed pinned operation's failure, including failed close.
                caller_error = outcome
            else:
                caller_error = (
                    error
                    if caller_error is None
                    else select_failure(caller_error, error)
                )
            raise caller_error


def _wait_block_ms(self: RedisBackend) -> int:
    """Bound the shared XREAD independently of each caller's lease deadline."""

    candidates = [_MAX_WAIT_BLOCK_MS]
    if self._socket_timeout_budget_ms is not None:
        candidates.append(self._socket_timeout_budget_ms)
    return max(1, min(candidates))


def _socket_timeout_budget(value: object) -> int | None:
    """Return a positive XREAD budget below the Redis socket timeout."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    return max(
        1,
        math.floor(timeout * 1000 * _SOCKET_TIMEOUT_SAFETY_RATIO),
    )


def _snapshot_integer(
    cls: type[RedisBackend],
    value: _RedisScriptValue,
    *,
    field: str,
    minimum: int,
) -> int:
    """Decode one canonical integer from a snapshot scalar."""

    text = cls._snapshot_text(value, field=field)
    try:
        parsed = int(text)
    except ValueError as error:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid {field}",
            cause=error,
        ) from error
    if str(parsed) != text or parsed < minimum:
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    return parsed


def _snapshot_boolean(
    self: RedisBackend,
    value: _RedisScriptValue,
    *,
    field: str,
) -> bool:
    """Decode one canonical Redis boolean from a snapshot scalar."""

    text = self._snapshot_text(value, field=field)
    if text not in {"0", "1"}:
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    return text == "1"


def _snapshot_text(value: _RedisScriptValue, *, field: str) -> str:
    """Decode one UTF-8 scalar and reject nested or malformed responses."""

    if not isinstance(value, bytes):
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    try:
        return value.decode()
    except UnicodeDecodeError as error:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid {field}",
            cause=error,
        ) from error


def _snapshot_bytes(value: _RedisScriptValue, *, field: str) -> bytes:
    """Return one binary scalar and reject nested snapshot structures."""

    if not isinstance(value, bytes):
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    return value


def _raise_stream_deleted(
    handle: BackendRunHandle,
    *,
    generation: int | None = None,
) -> Never:
    raise StreamDeleted(
        channel=handle.channel,
        identity=handle.identity,
        generation=(handle.generation if generation is None else generation),
    )


async def _eval(
    self: RedisBackend,
    script: str,
    keys: Sequence[str],
    arguments: Sequence[str | bytes],
) -> list[bytes]:
    response = await _redis_call(
        "script evaluation",
        self._client.eval(
            script,
            len(keys),
            *keys,
            *arguments,
        ),
    )
    if not isinstance(response, list):
        raise _redis_protocol_error("Redis script returned a non-list response")
    return cast(list[bytes], response)
