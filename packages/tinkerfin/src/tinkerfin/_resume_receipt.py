"""Attribute durable JSON decisions without changing Native resume value support."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import cast

from langgraph.checkpoint.base import CheckpointTuple
from pydantic import BaseModel, ConfigDict, Field

from tinkerfin_contracts import ThreadIdentity

from ._agui_lineage_state import LineageMarker
from .errors import TinkerFinLifecycleError

RESUME_RECEIPT_CHANNEL = "_tinkerfin_resume_receipt"
_RECEIPT_OWNER_PREFIX = "tinkerfin-resume-receipt:"


class ResumeReceipt(BaseModel):
    """Reserve one task prefix for its original managed invocation.

    A later invocation can rewrite a previously consumed prefix while continuing
    the task. Its writer does not become the owner of those earlier decisions.
    This record precedes the native write; both must agree before consumption is
    reported. Unfinished reservations can be retried only by their original owner.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    thread: ThreadIdentity
    graph_namespace: str
    checkpoint_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    prefix_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner: LineageMarker | None
    intent_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def receipt_owner(task_id: str, index: int) -> str:
    """Use one ordinary saver slot per native task prefix, outside real task writes."""

    return f"{_RECEIPT_OWNER_PREFIX}{task_id}:{index}"


def _is_json(value: object) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_json(item) for item in cast(list[object], value))
    if type(value) is dict:
        return all(
            type(key) is str and _is_json(item)
            for key, item in cast(dict[object, object], value).items()
        )
    return False


def resume_prefix_digest(values: Sequence[object]) -> str | None:
    """Hash exact finite JSON prefixes; leave opaque Native values unverifiable.

    SerializerProtocol promises round trips, not deterministic bytes. Native
    datetime, bytes, sets, and custom serializer values remain supported by the
    borrowed saver, but cannot authorize a JSON approval's consumption.
    """

    try:
        prefix = list(values)
        if not _is_json(prefix):
            return None
        encoded = json.dumps(
            prefix,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (ValueError, RecursionError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def task_resume_values(checkpoint: CheckpointTuple, task_id: str) -> list[object]:
    """Read the one native task list using LangGraph's scalar normalization."""

    found = [
        value
        for owner, channel, value in checkpoint.pending_writes or ()
        if owner == task_id and channel == "__resume__"
    ]
    if len(found) > 1:
        raise TinkerFinLifecycleError("task has conflicting native resume writes")
    if not found:
        return []
    return cast(list[object], found[0]) if isinstance(found[0], list) else [found[0]]


def resume_receipts(
    checkpoint: CheckpointTuple, task_id: str, *, thread: ThreadIdentity
) -> dict[int, ResumeReceipt]:
    """Validate only the dedicated owner slots for this exact native task."""

    prefix = f"{_RECEIPT_OWNER_PREFIX}{task_id}:"
    found: dict[int, ResumeReceipt] = {}
    for owner, channel, value in checkpoint.pending_writes or ():
        if not owner.startswith(prefix):
            continue
        suffix = owner.removeprefix(prefix)
        if (
            not suffix.isdecimal()
            or str(int(suffix)) != suffix
            or channel != RESUME_RECEIPT_CHANNEL
            or int(suffix) in found
        ):
            raise TinkerFinLifecycleError("task has invalid resume ownership records")
        receipt = ResumeReceipt.model_validate(value)
        options = checkpoint.config.get("configurable", {})
        if (
            receipt.thread != thread
            or receipt.graph_namespace != options.get("checkpoint_ns", "")
            or receipt.checkpoint_id != checkpoint.checkpoint["id"]
            or receipt.task_id != task_id
            or receipt.index != int(suffix)
            or (receipt.owner is not None and receipt.owner.thread != thread)
        ):
            raise TinkerFinLifecycleError(
                "resume ownership belongs to another task source"
            )
        found[int(suffix)] = receipt
    return found
