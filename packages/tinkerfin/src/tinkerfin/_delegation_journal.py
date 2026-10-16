"""Immutable delegated execution records attached to their original checkpoint.

Records use independent, content-addressed pending-write owners. LangGraph's
ordinary write indexes are local to an owner: using a real task ID would collide
with RETURN, and relying on overwrite behavior would differ between MemorySaver
and RedisSaver. These owners never match a scheduled task or the null writer, so
the scheduler cannot apply the records to Agent state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from pydantic import BaseModel, ConfigDict, JsonValue

from ._agui_lineage_state import (
    LINEAGE_CONFIG_KEY,
    LINEAGE_METADATA_KEY,
    RESUME_CONFIG_KEY,
    LineageMarker,
    ResumeIntent,
)
from ._tasks import run_async_owned
from .errors import DelegationReplayError

DELEGATION_RECORD_CHANNEL = "_tinkerfin_delegation_record"
_OWNER_PREFIX = "tinkerfin-delegation:"
RecordKind = Literal["request", "started", "effective", "outcome", "backoff_done"]


def canonical_json(value: object) -> str:
    """Encode finite JSON without depending on a saver's byte representation."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise DelegationReplayError(
            "Delegation parameters must be finite JSON", cause=error
        ) from error


def content_digest(value: object) -> str:
    """Identify the exact current JSON contract without schema versions."""
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


class DelegationRecord(BaseModel):
    """Validate one immutable private checkpoint write and its managed owners."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: LineageMarker
    writer: LineageMarker
    intent_digest: str | None
    graph_namespace: str
    checkpoint_id: str
    parent_task_id: str
    tool_call_id: str
    attempt_key: str
    kind: RecordKind
    payload: JsonValue

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class DelegationJournal:
    """Save attempts under a borrowed saver while the owning Run remains active.

    The exact parent checkpoint is retained across resume; completed runs never
    move or copy these writes to a newer head. Writes are append-only. A different
    proposed value occupies another digest owner and is rejected on the mandatory
    read-back, including on savers whose ordinary slots use upsert.
    """

    def __init__(
        self,
        saver: BaseCheckpointSaver[Any],
        config: RunnableConfig,
        *,
        graph_namespace: str,
        checkpoint_id: str,
        parent_task_id: str,
        tool_call_id: str,
    ) -> None:
        self.saver = saver
        options = config.get("configurable", {})
        writer = options.get(LINEAGE_CONFIG_KEY)
        intent = options.get(RESUME_CONFIG_KEY)
        if not isinstance(writer, LineageMarker):
            raise DelegationReplayError(
                "Delegation requires managed checkpoint ownership"
            )
        if intent is not None and (
            not isinstance(intent, ResumeIntent)
            or intent.thread != writer.thread
            or intent.run_id != writer.run_id
            or intent.parent_run_id != writer.parent_run_id
            or intent.runtime_profile != writer.runtime_profile
            or intent.role != writer.role
        ):
            raise DelegationReplayError("Delegation has conflicting approval ownership")
        self.writer = writer
        self.intent_digest = intent.digest if isinstance(intent, ResumeIntent) else None
        self.config: RunnableConfig = {
            "configurable": {
                "thread_id": writer.thread_id,
                "checkpoint_ns": graph_namespace,
                "checkpoint_id": checkpoint_id,
                LINEAGE_CONFIG_KEY: writer,
            }
        }
        self.graph_namespace = graph_namespace
        self.checkpoint_id = checkpoint_id
        self.parent_task_id = parent_task_id
        self.tool_call_id = tool_call_id
        self.request_key = content_digest(
            [
                writer.thread.model_dump(mode="json"),
                graph_namespace,
                checkpoint_id,
                parent_task_id,
                tool_call_id,
            ]
        )

        # AG-UI validates the selected branch, original anchors and intent before
        # entering the Graph. Native Command callers have no ResumeIntent; their
        # actual ExecutionInfo supplies these coordinates. A later writer does not
        # replace the source checkpoint's original run ownership.

    def attempt_key(self, index: int) -> str:
        return content_digest([self.request_key, index])

    async def _checkpoint(self) -> CheckpointTuple:
        checkpoint = await self.saver.aget_tuple(self.config)
        if checkpoint is None:
            raise DelegationReplayError("Delegation source checkpoint is unavailable")
        options = checkpoint.config.get("configurable", {})
        if (
            options.get("thread_id") != self.writer.thread_id
            or options.get("checkpoint_ns", "") != self.graph_namespace
            or checkpoint.checkpoint["id"] != self.checkpoint_id
        ):
            raise DelegationReplayError("Delegation source checkpoint changed identity")
        return checkpoint

    def _source(self, checkpoint: CheckpointTuple) -> LineageMarker:
        value = checkpoint.metadata.get(LINEAGE_METADATA_KEY)
        if not isinstance(value, str):
            raise DelegationReplayError("Delegation source has no managed lineage")
        source = LineageMarker.model_validate_json(value)
        if (
            source.thread != self.writer.thread
            or source.runtime_profile != self.writer.runtime_profile
            or source.role != self.writer.role
        ):
            raise DelegationReplayError("Delegation checkpoint has conflicting lineage")
        return source

    def _read(
        self, checkpoint: CheckpointTuple, attempt_key: str, kind: RecordKind
    ) -> DelegationRecord | None:
        prefix = f"{_OWNER_PREFIX}{attempt_key}:{kind}:"
        source = self._source(checkpoint)
        records: list[DelegationRecord] = []
        for owner, channel, value in checkpoint.pending_writes or ():
            if not owner.startswith(prefix):
                continue
            try:
                record = DelegationRecord.model_validate(value)
            except ValueError as error:
                raise DelegationReplayError(
                    "Delegation journal contains an invalid record", cause=error
                ) from error
            if (
                channel != DELEGATION_RECORD_CHANNEL
                or owner != prefix + record.digest
                or record.source != source
                or record.writer.thread != source.thread
                or record.writer.runtime_profile != source.runtime_profile
                or record.writer.role != source.role
                or record.graph_namespace != self.graph_namespace
                or record.checkpoint_id != self.checkpoint_id
                or record.parent_task_id != self.parent_task_id
                or record.tool_call_id != self.tool_call_id
                or record.attempt_key != attempt_key
                or record.kind != kind
            ):
                raise DelegationReplayError(
                    "Delegation journal has conflicting ownership"
                )
            records.append(record)
        if len(records) > 1:
            raise DelegationReplayError(
                "Delegation journal contains conflicting records"
            )
        return records[0] if records else None

    async def read(self, attempt_key: str, kind: RecordKind) -> DelegationRecord | None:
        return self._read(await self._checkpoint(), attempt_key, kind)

    async def save(
        self, attempt_key: str, kind: RecordKind, payload: JsonValue
    ) -> DelegationRecord:
        """Await an immutable write and verified read before dependent execution.

        The existing owned-operation boundary joins saver I/O through cancellation
        and retains failures before the Run releases its coordinator and resources.
        This does not manufacture a CAS or a second cross-process lock.

        Args:
            attempt_key: Stable request or attempt identity at the original checkpoint.
            kind: The independently committed stage of that request or attempt.
            payload: Finite JSON that must match an already committed record exactly.

        Returns:
            The immutable record verified by reading the exact checkpoint back.

        Raises:
            DelegationReplayError: Ownership, payload, or durable read-back conflicts.
            CancelledError: Caller cancellation, after the pending save has been joined.
        """

        async def commit() -> DelegationRecord:
            checkpoint = await self._checkpoint()
            previous = self._read(checkpoint, attempt_key, kind)
            if previous is not None:
                if content_digest(previous.payload) != content_digest(payload):
                    raise DelegationReplayError(
                        "Delegation replay changed recorded inputs or outcome"
                    )
                return previous
            record = DelegationRecord(
                source=self._source(checkpoint),
                writer=self.writer,
                intent_digest=self.intent_digest,
                graph_namespace=self.graph_namespace,
                checkpoint_id=self.checkpoint_id,
                parent_task_id=self.parent_task_id,
                tool_call_id=self.tool_call_id,
                attempt_key=attempt_key,
                kind=kind,
                payload=payload,
            )
            owner = f"{_OWNER_PREFIX}{attempt_key}:{kind}:{record.digest}"
            await self.saver.aput_writes(
                self.config,
                [(DELEGATION_RECORD_CHANNEL, record.model_dump(mode="json"))],
                owner,
            )
            verified = self._read(await self._checkpoint(), attempt_key, kind)
            if verified != record:
                raise DelegationReplayError("Delegation outcome was not durably saved")
            return record

        return await run_async_owned(commit, task_name="tinkerfin-delegation-save")


def json_payload(value: object) -> JsonValue:
    """Detach a finite JSON boundary value before checkpoint submission."""
    return cast(JsonValue, json.loads(canonical_json(value)))
