"""Redis journal publication, replay, and envelope decoding operations."""

from __future__ import annotations

__all__ = [
    "_bytes",
    "_decode_entry",
    "_digest",
    "_message_signature",
    "_qualified_name",
    "_remote_error",
    "_snapshot_messages",
    "_text",
]

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity, thread_key
from ._messaging_ledger import (
    BackendRunHandle,
    PreparedRun,
    validate_append_input,
)
from ._redis_control import (
    _SNAPSHOT_PAGE_SIZE,
    _redis_protocol_error,
    _RedisScriptValue,
    _RunSnapshot,
    complete_generation_cleanup,
)
from ._redis_scripts import _APPEND_SCRIPT, _PREPARE_SCRIPT
from .errors import (
    BackendOwnershipLost,
    CodecMismatch,
    InvalidCursor,
    MessageIdConflict,
    MessagingQuotaExceeded,
    PublicationRejected,
    RunAlreadyActive,
    StreamDeleted,
    StreamExpired,
)
from .models import MessageEnvelope, RecoveryCheckpoint

if TYPE_CHECKING:
    from .redis import RedisBackend


async def prepare(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
    codec: str,
    after: int | None,
    cancellable: bool,
    recoverable: bool,
) -> PreparedRun:
    """Atomically start, recover, or attach within one thread generation.

    The Lua contract validates codec, limits, cursor, active-run exclusion, recovery
    capability, and lease acquisition together. A delete/recreate race restarts the
    lookup instead of binding the caller to a stale generation.

    Args:
        self: Redis Backend owning the current channel namespace and worker lease.
        channel: Canonical logical channel name.
        identity: Exact thread and semantic Run identity.
        codec: Persisted codec identity required by every generation.
        after: Optional exclusive replay cursor.
        cancellable: Whether the new owner exposes remote cancellation.
        recoverable: Whether the source can restart from a committed checkpoint.

    Returns:
        Owner or attachment preparation bound to one current generation.

    Raises:
        MessagingError: Redis state rejects or cannot prove the requested preparation.
        TypeError: ``after`` has the wrong type.
    """

    required_identifier("channel", channel)
    required_identity(identity)
    required_identifier("codec", codec)
    if after is not None and (isinstance(after, bool) or not isinstance(after, int)):
        raise TypeError("after must be an integer or None")
    scope = self._scope(channel, identity)
    owner_token = f"{self._worker_id}:{uuid4().hex}"
    while True:
        control = await self._read_control(scope)
        if control is None:
            generation = 1
        elif control.state == "deleting":
            raise StreamDeleted(
                channel=channel,
                identity=identity,
                generation=control.generation,
            )
        elif control.state == "expiring":
            await complete_generation_cleanup(
                self,
                channel=channel,
                identity=identity,
                requested_reason="expired",
            )
            continue
        elif control.state in {"deleted", "expired"}:
            if control.state == "expired" and after not in {None, 0}:
                raise StreamExpired(
                    channel=channel,
                    identity=identity,
                    generation=control.generation,
                )
            generation = control.generation + 1
        else:
            generation = control.generation
        keys = self._keys(channel, identity, generation=generation)
        response = await self._eval(
            _PREPARE_SCRIPT,
            [
                keys.channel_meta,
                keys.control,
                keys.meta,
                keys.run_key,
                keys.lease_key,
                keys.index,
                self._capacity_key,
                self._expirations_key,
                self._notifications_key,
                self._notification_counter_key,
            ],
            [
                str(generation),
                identity.run_id,
                codec,
                "__tail__" if after is None else str(after),
                "1" if cancellable else "0",
                "1" if recoverable else "0",
                owner_token,
                str(self._lease_ms),
                channel,
                thread_key(identity),
                str(self._limits.max_message_payload_bytes),
                str(self._limits.max_checkpoint_bytes),
                str(self._limits.max_thread_messages),
                str(self._limits.max_thread_payload_bytes),
                str(self._retention_ms),
                str(self._limits.max_total_bytes),
                str(self._limits.max_total_records),
            ],
        )
        code = self._text(response[0])
        if code == "GENERATION_CHANGED":
            continue
        if code == "STREAM_EXPIRING":
            await complete_generation_cleanup(
                self,
                channel=channel,
                identity=identity,
                requested_reason="expired",
            )
            continue
        if code == "STREAM_DELETED":
            raise StreamDeleted(
                channel=channel,
                identity=identity,
                generation=generation,
            )
        if code == "INVALID_CONTROL_STATE":
            raise _redis_protocol_error(
                f"Redis stream control has invalid state: {self._text(response[1])!r}"
            )
        if code == "QUOTA_EXCEEDED":
            raise MessagingQuotaExceeded(
                resource=self._text(response[1]), limit=int(self._text(response[2]))
            )
        if code == "LIMITS_MISMATCH":
            raise _redis_protocol_error(
                "Redis channel was opened with different MessagingLimits"
            )
        if code == "RETENTION_MISMATCH":
            raise _redis_protocol_error(
                "Redis channel was opened with a different retention policy"
            )
        break
    if code == "INVALID_CURSOR":
        raise InvalidCursor(
            after=int(self._text(response[1])),
            latest=int(self._text(response[2])),
        )
    if code == "CODEC_MISMATCH":
        raise CodecMismatch(
            expected=self._text(response[1]),
            actual=codec,
        )
    if code == "RUN_ACTIVE":
        raise RunAlreadyActive(
            active_identity=RunIdentity(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                run_id=self._text(response[1]),
            ),
            requested_identity=identity,
        )
    cursor = int(self._text(response[1]))
    if code in {"START", "RECOVER"}:
        fence = int(self._text(response[2]))
        checkpoint: RecoveryCheckpoint | None = None
        if code == "RECOVER" and self._text(response[3]) == "1":
            last_id = self._text(response[5]) or None
            checkpoint = RecoveryCheckpoint(
                position=self._bytes(response[4]),
                last_message_id=last_id,
            )
        return PreparedRun(
            handle=BackendRunHandle(
                channel=channel,
                identity=identity,
                owner_token=owner_token,
                fence=fence,
                generation=generation,
            ),
            after=cursor,
            is_owner=True,
            checkpoint=checkpoint,
            recovered=code == "RECOVER",
        )
    if code != "ATTACH":
        raise _redis_protocol_error(f"unexpected Redis prepare response: {code}")
    return PreparedRun(
        handle=BackendRunHandle(
            channel=channel,
            identity=identity,
            owner_token=None,
            fence=None,
            generation=generation,
        ),
        after=cursor,
        is_owner=False,
    )


async def append(
    self: RedisBackend,
    handle: BackendRunHandle,
    *,
    message_id: str,
    codec: str,
    payload: bytes,
    checkpoint: RecoveryCheckpoint | None = None,
    external: bool = False,
    closes_publication: bool = False,
    opens_publication: bool = True,
) -> MessageEnvelope:
    """Append one fenced, quota-checked, idempotent message and checkpoint.

    Message-ID deduplication compares a digest of identity, codec, payload, and optional
    checkpoint before allocating sequence or quota. The current generation, owner token,
    and fence must still match in the same Lua transaction.

    Args:
        self: Redis Backend owning the producer fence.
        handle: Exact owned generation, token, and fence.
        message_id: Stable idempotency key within the stream.
        codec: Codec identity already bound during preparation.
        payload: Finite encoded message bytes.
        checkpoint: Optional source position committed atomically with the message.
        external: Publish as an observer without producer ownership or checkpoint changes.
        closes_publication: Seal subsequent external messages at this source commit.
        opens_publication: Permit publication after the required source start.

    Returns:
        The committed immutable envelope, including its allocated sequence.

    Raises:
        MessagingError: Ownership, idempotency, codec, quota, or Redis evidence fails.
    """

    validate_append_input(
        handle,
        message_id=message_id,
        codec=codec,
        payload=payload,
        checkpoint=checkpoint,
        limits=self._limits,
    )
    generation = handle.generation
    if external and (checkpoint is not None or closes_publication):
        raise ValueError(
            "External publication cannot change source recovery or lifecycle"
        )
    if generation is None or (
        not external and (handle.owner_token is None or handle.fence is None)
    ):
        raise BackendOwnershipLost(
            f"Run {handle.identity.run_id!r} has no complete producer ownership "
            "identity"
        )
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    dedupe = f"{keys.generation_base}:message:{self._digest(message_id)}"
    signature = self._message_signature(
        identity=handle.identity,
        codec=codec,
        payload=payload,
        checkpoint=checkpoint,
    )
    response = await self._eval(
        _APPEND_SCRIPT,
        [
            keys.control,
            keys.channel_meta,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            keys.messages,
            dedupe,
            keys.index,
            self._capacity_key,
            self._notifications_key,
            self._notification_counter_key,
        ],
        [
            str(generation),
            handle.owner_token or "",
            "" if handle.fence is None else str(handle.fence),
            message_id,
            handle.identity.run_id,
            codec,
            payload,
            signature,
            "1" if checkpoint is not None else "0",
            b"" if checkpoint is None else checkpoint.position,
            ""
            if checkpoint is None or checkpoint.last_message_id is None
            else checkpoint.last_message_id,
            str(self._limits.max_thread_messages),
            str(self._limits.max_thread_payload_bytes),
            str(self._limits.max_total_bytes),
            str(self._limits.max_total_records),
            "1" if external else "0",
            "1" if closes_publication else "0",
            "1" if opens_publication and not external else "0",
        ],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "PUBLICATION_REJECTED":
        raise PublicationRejected(
            identity=handle.identity, reason=self._text(response[1])
        )
    if code == "OWNERSHIP_LOST":
        raise BackendOwnershipLost(
            f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
        )
    if code == "CODEC_MISMATCH":
        raise CodecMismatch(
            expected=self._text(response[1]),
            actual=codec,
        )
    if code == "MESSAGE_CONFLICT":
        raise MessageIdConflict(
            identity=handle.identity,
            message_id=message_id,
        )
    if code == "LIMITS_MISMATCH":
        raise _redis_protocol_error(
            "Redis scope was opened with different total capacity limits"
        )
    if code == "QUOTA_EXCEEDED":
        raise MessagingQuotaExceeded(
            resource=self._text(response[1]),
            limit=int(self._text(response[2])),
        )
    if code not in {"APPENDED", "IDEMPOTENT"}:
        raise _redis_protocol_error(f"unexpected Redis append response: {code}")
    seq = int(self._text(response[1]))
    created_at = datetime.fromtimestamp(
        int(self._text(response[2])) + int(self._text(response[3])) / 1_000_000,
        tz=UTC,
    )
    return MessageEnvelope(
        channel=handle.channel,
        identity=handle.identity,
        seq=seq,
        message_id=message_id,
        codec=codec,
        payload=bytes(payload),
        created_at=created_at,
    )


def _snapshot_messages(
    self: RedisBackend,
    value: _RedisScriptValue,
    *,
    channel: str,
    identity: RunIdentity,
    after: int | None,
    end_seq: int,
) -> tuple[MessageEnvelope, ...]:
    """Decode the exact nested XRANGE representation returned through EVAL."""

    if not isinstance(value, list):
        raise _redis_protocol_error("Redis run snapshot has an invalid message page")
    if after is None and value:
        raise _redis_protocol_error(
            "Redis run snapshot returned an unexpected message page"
        )
    if len(value) > _SNAPSHOT_PAGE_SIZE:
        raise _redis_protocol_error(
            "Redis run snapshot exceeded its message page limit"
        )

    messages: list[MessageEnvelope] = []
    previous_seq = after
    expected_fields = {
        b"message_id",
        b"run",
        b"codec",
        b"payload",
        b"created_seconds",
        b"created_microseconds",
    }
    for raw_entry in value:
        if not isinstance(raw_entry, list) or len(raw_entry) != 2:
            raise _redis_protocol_error(
                "Redis run snapshot has a malformed message entry"
            )
        identifier = self._snapshot_bytes(
            raw_entry[0],
            field="message identifier",
        )
        raw_fields = raw_entry[1]
        if not isinstance(raw_fields, list) or len(raw_fields) % 2 != 0:
            raise _redis_protocol_error(
                "Redis run snapshot has malformed message fields"
            )
        fields: dict[bytes, bytes] = {}
        for index in range(0, len(raw_fields), 2):
            key = self._snapshot_bytes(
                raw_fields[index],
                field="message field name",
            )
            field_value = self._snapshot_bytes(
                raw_fields[index + 1],
                field="message field value",
            )
            if key in fields:
                raise _redis_protocol_error(
                    "Redis run snapshot has duplicate message fields"
                )
            fields[key] = field_value
        if set(fields) != expected_fields:
            raise _redis_protocol_error(
                "Redis run snapshot has incomplete message fields"
            )
        try:
            message = self._decode_entry(
                channel,
                identity,
                (identifier, fields),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
            raise _redis_protocol_error(
                "Redis run snapshot has a malformed message entry",
                cause=error,
            ) from error
        if message.seq > end_seq or (
            previous_seq is not None and message.seq <= previous_seq
        ):
            raise _redis_protocol_error(
                "Redis run snapshot has an invalid message order"
            )
        previous_seq = message.seq
        messages.append(message)
    return tuple(messages)


def _decode_entry(
    self: RedisBackend,
    channel: str,
    identity: RunIdentity,
    entry: tuple[bytes, Mapping[bytes, bytes]],
) -> MessageEnvelope:
    identifier, raw_fields = entry
    fields = {self._text(key): value for key, value in raw_fields.items()}
    seq = int(self._text(identifier).split("-", maxsplit=1)[0])
    return MessageEnvelope(
        channel=channel,
        identity=RunIdentity(
            namespace=identity.namespace,
            thread_id=identity.thread_id,
            run_id=self._text(fields["run"]),
        ),
        seq=seq,
        message_id=self._text(fields["message_id"]),
        codec=self._text(fields["codec"]),
        payload=self._bytes(fields["payload"]),
        created_at=datetime.fromtimestamp(
            int(self._text(fields["created_seconds"]))
            + int(self._text(fields["created_microseconds"])) / 1_000_000,
            tz=UTC,
        ),
    )


def _message_signature(
    *,
    identity: RunIdentity,
    codec: str,
    payload: bytes,
    checkpoint: RecoveryCheckpoint | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"tinkerfin-messaging:redis-message\0")
    values = [identity.run_id.encode(), codec.encode(), payload]
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value: bytes | str | int) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _bytes(value: bytes | str | int) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode()


def _qualified_name(value: BaseException) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _remote_error(snapshot: _RunSnapshot) -> RuntimeError:
    error_class = snapshot.error_class or "builtins.RuntimeError"
    message = snapshot.error_message or "remote producer failed"
    error = RuntimeError(f"{error_class}: {message}")
    error.add_note(
        "Redis lease evidence: "
        f"renew_count={snapshot.lease_renew_count}, "
        "last_success="
        f"{snapshot.lease_last_success_seconds}."
        f"{snapshot.lease_last_success_microseconds:06d} UTC"
    )
    return error
