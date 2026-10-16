"""Saver-owned evidence for the exact tool batch awaiting approval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from langchain_core.messages import ToolCall
from langgraph.checkpoint.base import CheckpointTuple
from pydantic import BaseModel, ConfigDict, Field

from .errors import TinkerFinLifecycleError

TOOL_REVIEW_CHANNEL = "_tinkerfin_tool_review"


class PendingToolReview(BaseModel):
    """Prove which middleware task produced one pending tool review."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    graph_namespace: str = Field(
        description="Complete LangGraph scope, including invocation slots"
    )
    run_id: str | None = Field(default=None, min_length=1)
    task_id: str = Field(min_length=1)
    interrupt_id: str = Field(min_length=1)
    batch_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def tool_review_owner(task_id: str) -> str:
    """Keep evidence out of real task writes and null-task resume intent slots."""

    return f"tinkerfin-tool-review:{task_id}"


def pending_tool_review(
    checkpoint: CheckpointTuple, *, task_id: str
) -> PendingToolReview | None:
    """Read only the dedicated saver owner; ordinary state cannot grant support."""

    values = [
        value
        for owner, channel, value in checkpoint.pending_writes or ()
        if owner == tool_review_owner(task_id) and channel == TOOL_REVIEW_CHANNEL
    ]
    if not values:
        return None
    if len(values) != 1:
        raise TinkerFinLifecycleError("pending tool review has conflicting evidence")
    return PendingToolReview.model_validate(values[0])


def tool_review_digest(
    request: Mapping[str, object],
    *,
    tool_calls: Sequence[ToolCall],
    reviewed_ids: Sequence[str],
) -> str:
    """Bind decisions to the review policy, exact selected IDs, and full model batch."""

    serialized = json.dumps(
        {"request": request, "tools": tool_calls, "reviewed_ids": reviewed_ids},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
