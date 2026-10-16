"""Public values stored and replayed by messaging backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity

ReplayT = TypeVar("ReplayT")


class MessageEnvelope(BaseModel):
    """One immutable payload committed to an ordered channel stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(
        min_length=1,
        max_length=1024,
        description="Logical channel whose codec interprets the payload.",
    )
    identity: RunIdentity = Field(
        description="Shared thread and semantic run identity for this payload."
    )
    seq: int = Field(
        ge=1,
        description="One-based monotonic sequence within the channel stream.",
    )
    message_id: str = Field(
        min_length=1,
        max_length=1024,
        description="Stable idempotency identifier within the channel stream.",
    )
    codec: str = Field(
        min_length=1,
        max_length=1024,
        description="Persisted codec identifier required for decoding.",
    )
    payload: bytes = Field(
        description="Protocol-neutral encoded payload committed by the backend.",
    )
    created_at: datetime = Field(
        description="Aware UTC timestamp allocated on the first successful commit.",
    )

    @field_validator("channel", "message_id", "codec")
    @classmethod
    def _identifier_is_canonical(cls, value: str) -> str:
        return required_identifier("identifier", value)

    @field_validator("identity")
    @classmethod
    def _identity_is_bounded(cls, value: RunIdentity) -> RunIdentity:
        return required_identity(value)

    @field_validator("created_at")
    @classmethod
    def _created_at_is_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("created_at must be an aware UTC timestamp")
        return value


class RecoveryCheckpoint(BaseModel):
    """Opaque source position atomically committed with a recoverable message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position: bytes = Field(
        description="Opaque position interpreted only by the source factory.",
    )
    last_message_id: str | None = Field(
        default=None,
        max_length=1024,
        description="Last stable message ID included in this checkpoint.",
    )

    @field_validator("last_message_id")
    @classmethod
    def _last_message_id_is_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return required_identifier("last_message_id", value)


@dataclass(frozen=True, slots=True)
class DecodedMessage(Generic[ReplayT]):
    """Pair one durable envelope with its codec-decoded value."""

    envelope: MessageEnvelope
    data: ReplayT


@dataclass(frozen=True, slots=True)
class RecoverableMessage(Generic[ReplayT]):
    """Carry one stable message ID and the checkpoint committed after it."""

    message_id: str
    data: ReplayT
    checkpoint: RecoveryCheckpoint

    def __post_init__(self) -> None:
        """Require the checkpoint to name this exact recoverable message."""

        required_identifier("message_id", self.message_id)
        if self.checkpoint.last_message_id != self.message_id:
            raise ValueError("checkpoint.last_message_id must match message_id")
