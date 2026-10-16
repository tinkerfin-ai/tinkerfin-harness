"""Deterministic identities and canonical command digests for Automation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TypeAlias
from uuid import UUID, uuid5

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity

from .schedules import (
    CronSchedule,
    IntervalSchedule,
    OnceSchedule,
    ScheduleSpec,
)

_AUTOMATION_NAMESPACE = UUID("154841d6-4a23-50da-8f53-0f3609cab079")
_CanonicalValue: TypeAlias = (
    JsonValue | datetime | ScheduleSpec | Mapping[str, JsonValue]
)


def _json_value(value: _CanonicalValue) -> JsonValue:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity datetime values must be timezone-aware")
        return value.isoformat()
    if isinstance(value, (OnceSchedule, IntervalSchedule, CronSchedule)):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def canonical_digest(values: Mapping[str, _CanonicalValue]) -> str:
    """Return a stable SHA-256 digest for bounded command input."""

    encoded = json.dumps(
        {key: _json_value(value) for key, value in values.items()},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def occurrence_key(parts: Sequence[str]) -> str:
    """Return one unambiguous deterministic occurrence key."""

    encoded = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def execution_identity(
    *,
    namespace: str,
    execution_namespace: str,
    owner_id: str,
    task_id: str | None,
    occurrence: str,
    attempt: int,
) -> tuple[str, RunIdentity]:
    """Derive the execution ID and Runtime identity for one business attempt."""

    material = json.dumps(
        [namespace, execution_namespace, owner_id, task_id, occurrence, attempt],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    execution_id = str(uuid5(_AUTOMATION_NAMESPACE, f"execution:{material}"))
    identity = RunIdentity(
        namespace=execution_namespace,
        thread_id=str(uuid5(_AUTOMATION_NAMESPACE, f"thread:{material}")),
        run_id=str(uuid5(_AUTOMATION_NAMESPACE, f"run:{material}")),
    )
    return execution_id, identity


__all__ = ["canonical_digest", "execution_identity", "occurrence_key"]
