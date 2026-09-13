"""Shared immutable model behavior for tracing contracts."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel


class TraceModel(BaseModel, frozen=True):
    """Provide strict immutable validation for Trace values."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class TimedTraceModel(TraceModel, frozen=True):
    """Attach comparable source timestamps to one semantic fact."""

    occurred_at: datetime
    monotonic_ns: int

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: datetime) -> datetime:
        """Require an aware UTC timestamp without rewriting evidence."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("occurred_at must be an aware UTC timestamp")
        return value

    @field_validator("monotonic_ns")
    @classmethod
    def monotonic_ns_is_non_negative(cls, value: int) -> int:
        """Reject values that cannot represent a monotonic clock reading."""

        if value < 0:
            raise ValueError("monotonic_ns must be non-negative")
        return value


__all__ = ["TimedTraceModel", "TraceModel"]
