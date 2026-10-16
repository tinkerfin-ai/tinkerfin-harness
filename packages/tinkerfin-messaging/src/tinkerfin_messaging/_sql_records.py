"""Validate SQL identities and decode bounded Messaging records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeGuard

from sqlalchemy import Table, and_
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql.elements import ColumnElement

from tinkerfin_contracts import RunIdentity

from ._identity import stored_request_digest, thread_key
from ._messaging_transition import messaging_message_signature
from .backend import is_active_run_status, is_final_run_status
from .backend_contract import StoredMessageEvidence, StoredMessagingRun
from .errors import MessagingBackendProtocolError
from .models import MessageEnvelope, RecoveryCheckpoint


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True, slots=True)
class Scope:
    """Keep full identities alongside bounded SQL indexes for collision checks."""

    channel: str
    identity: RunIdentity

    @property
    def channel_id(self) -> bytes:
        return digest(self.channel)

    @property
    def thread_id(self) -> bytes:
        return digest(thread_key(self.identity))

    def where(self, table: Table, generation: int | None = None) -> ColumnElement[bool]:
        terms = [table.c.channel_id == self.channel_id]
        if "thread_id" in table.c:
            terms.append(table.c.thread_id == self.thread_id)
        if generation is not None:
            terms.append(table.c.generation == generation)
        return and_(*terms)

    def keys(self, generation: int | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
        }
        if generation is not None:
            result["generation"] = generation
        return result


def text_value(value: object) -> str:
    if not isinstance(value, str):
        raise MessagingBackendProtocolError("Messaging SQL text has an invalid type")
    return value


def encode_text(value: str) -> str:
    # PostgreSQL TEXT cannot hold NUL. ASCII JSON strings preserve every allowed
    # identifier and arbitrary diagnostic text without tightening the public API.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def decode_text(value: object) -> str:
    try:
        decoded: object = json.loads(text_value(value))
    except ValueError as error:
        raise MessagingBackendProtocolError(
            "Messaging SQL encoded text is invalid", cause=error
        ) from error
    return text_value(decoded)


def optional_decoded_text(value: object) -> str | None:
    return None if value is None else decode_text(value)


def binary(value: object) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if not isinstance(value, bytes):
        raise MessagingBackendProtocolError("Messaging SQL bytes have an invalid type")
    return value


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MessagingBackendProtocolError(
            "Messaging SQL counter has an invalid value"
        )
    return value


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MessagingBackendProtocolError("Messaging SQL flag has an invalid type")
    return value


def utc_time(value: object) -> datetime:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("Invalid timestamp type")
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise MessagingBackendProtocolError(
            "Messaging SQL timestamp is invalid", cause=error
        ) from error


def checkpoint(row: RowMapping) -> RecoveryCheckpoint | None:
    position, message_id = row["checkpoint_position"], row["checkpoint_message_id"]
    if position is None:
        if message_id is not None:
            raise MessagingBackendProtocolError(
                "Messaging SQL checkpoint position is missing"
            )
        return None
    try:
        return RecoveryCheckpoint(
            position=binary(position), last_message_id=optional_decoded_text(message_id)
        )
    except ValueError as error:
        raise MessagingBackendProtocolError(
            "Messaging SQL checkpoint is invalid", cause=error
        ) from error


def checkpoint_values(value: RecoveryCheckpoint | None) -> dict[str, object]:
    return {
        "checkpoint_position": None if value is None else value.position,
        "checkpoint_message_id": None
        if value is None or value.last_message_id is None
        else encode_text(value.last_message_id),
    }


def stored_identity(value: object, scope: Scope) -> RunIdentity:
    try:
        identity = RunIdentity.model_validate_json(text_value(value))
    except ValueError as error:
        raise MessagingBackendProtocolError(
            "Messaging SQL run identity is invalid", cause=error
        ) from error
    if identity.thread != scope.identity.thread:
        raise MessagingBackendProtocolError(
            "Messaging SQL run identity escapes its thread"
        )
    return identity


def stored_run(row: RowMapping, scope: Scope, now: datetime) -> StoredMessagingRun:
    identity = stored_identity(row["identity"], scope)
    if binary(row["run_id"]) != digest(identity.run_id):
        raise MessagingBackendProtocolError(
            "Messaging SQL run identity does not match its index"
        )
    status = row["status"]
    if not is_active_run_status(status) and not is_final_run_status(status):
        raise MessagingBackendProtocolError("Messaging SQL producer status is invalid")
    deadline = row["lease_deadline"]
    remaining = (
        None
        if deadline is None
        else max(0.0, (utc_time(deadline) - now).total_seconds())
    )
    token = optional_decoded_text(row["producer_token"])
    return StoredMessagingRun(
        identity=identity,
        request_digest=stored_request_digest(row["request_digest"]),
        generation=integer(row["generation"]),
        start_sequence=integer(row["start_sequence"]),
        end_sequence=integer(row["end_sequence"]),
        status=status,
        settlement_started=boolean(row["settlement_started"]),
        cancellable=boolean(row["cancellable"]),
        recoverable=boolean(row["recoverable"]),
        producer_token=token,
        producer_fence=integer(row["producer_fence"]),
        producer_lease_active=token is not None
        and remaining is not None
        and remaining > 0,
        producer_lease_remaining_seconds=remaining,
        publication_closed=boolean(row["publication_closed"]),
        publication_ready=boolean(row["publication_ready"]),
        checkpoint=checkpoint(row),
        failure_class=decode_text(row["failure_class"]),
        failure_message=decode_text(row["failure_message"]),
    )


def message_evidence(
    row: RowMapping, scope: Scope, codec: str
) -> StoredMessageEvidence:
    identity = stored_identity(row["identity"], scope)
    message_id = decode_text(row["message_id"])
    if (
        binary(row["message_id_hash"]) != digest(message_id)
        or decode_text(row["codec"]) != codec
    ):
        raise MessagingBackendProtocolError(
            "Messaging SQL message index or codec is inconsistent"
        )
    saved_checkpoint = checkpoint(row)
    payload = binary(row["payload"])
    signature = text_value(row["signature"])
    if signature != messaging_message_signature(
        identity=identity, codec_id=codec, payload=payload, checkpoint=saved_checkpoint
    ):
        raise MessagingBackendProtocolError(
            "Messaging SQL message evidence is inconsistent"
        )
    try:
        envelope = MessageEnvelope(
            channel=scope.channel,
            identity=identity,
            seq=integer(row["sequence"]),
            message_id=message_id,
            codec=codec,
            payload=payload,
            created_at=utc_time(row["created_at"]),
        )
    except ValueError as error:
        raise MessagingBackendProtocolError(
            "Messaging SQL message is invalid", cause=error
        ) from error
    return StoredMessageEvidence(
        envelope=envelope, signature=signature, checkpoint=saved_checkpoint
    )


def live_disposition(
    value: str,
) -> TypeGuard[Literal["active", "deleting", "expiring"]]:
    return value in {"active", "deleting", "expiring"}
