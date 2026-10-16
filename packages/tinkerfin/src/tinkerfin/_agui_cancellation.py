"""Non-consuming abandonment evidence retained on the interrupted checkpoint."""

from __future__ import annotations

import hashlib
from typing import cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import Field, field_validator

from tinkerfin_contracts import RunIdentity

from ._agui_lineage import (
    AgUiLineageResolution,
    _checkpoint_id,
    _checkpoint_lineage,
    _CheckpointSaver,
)
from ._agui_lineage_state import LineageMarker
from ._tasks import run_async_owned
from .agui_resume import AgUiResumeBinding, AgUiResumeRequest
from .errors import TinkerFinLifecycleError

_CHANNEL = "_tinkerfin_abandonment"


class CancellationConflict(TinkerFinLifecycleError):
    """Reject reuse of a cancellation identity without opening another writer."""


class CancellationFact(RunIdentity):
    """Identify a validated cancellation without consuming its pending interrupt."""

    source: LineageMarker
    parent_run_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    checkpoint_ns: str
    public_interrupt_ids: tuple[str, ...] = Field(min_length=1)
    native_interrupt_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("public_interrupt_ids", "native_interrupt_ids", mode="before")
    @classmethod
    def normalize_ids(cls, value: object) -> object:
        """Accept the JSON array representation while preserving canonical order."""

        return tuple(cast(list[object], value)) if isinstance(value, list) else value

    @field_validator("public_interrupt_ids", "native_interrupt_ids")
    @classmethod
    def canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous or damaged batch identities instead of repairing them."""

        if tuple(sorted(set(value))) != value or any(
            not item or item != item.strip() for item in value
        ):
            raise ValueError("cancellation IDs must be canonical and unique")
        return value

    def binding(self) -> AgUiResumeBinding:
        """Recreate only the non-executing operation from saver-readable evidence."""

        return AgUiResumeBinding(
            mode="abandon", native_interrupt_ids=self.native_interrupt_ids
        )

    def validate_request(
        self, request: AgUiResumeRequest | None, parent_run_id: str | None
    ) -> None:
        """Require the same complete cancellation and resolved source on retries."""

        if (
            request is None
            or any(
                entry.status != "cancelled" or entry.payload is not None
                for entry in request.entries
            )
            or tuple(sorted(entry.interrupt_id for entry in request.entries))
            != self.public_interrupt_ids
            or parent_run_id not in (None, self.parent_run_id)
        ):
            raise CancellationConflict("runId belongs to a different cancellation")


def _owner(identity: RunIdentity) -> str:
    return "tinkerfin-abandon:" + hashlib.sha256(identity.run_id.encode()).hexdigest()


async def find_cancellation(
    saver: object | None, *, identity: RunIdentity, runtime_profile: str
) -> CancellationFact | None:
    """Read immutable cancellation evidence independently of the current Graph head."""

    if saver is None:
        return None
    if not isinstance(saver, BaseCheckpointSaver):
        raise TinkerFinLifecycleError("cancellation requires a concrete checkpointer")
    checkpointer = cast(_CheckpointSaver, saver)
    found: CancellationFact | None = None
    async for checkpoint in checkpointer.alist(
        {"configurable": {"thread_id": identity.thread_id}}
    ):
        for task_id, channel, value in checkpoint.pending_writes or ():
            if task_id != _owner(identity):
                continue
            if channel != _CHANNEL:
                raise CancellationConflict("cancellation owner has unrelated writes")
            fact = CancellationFact.model_validate(value)
            source = _checkpoint_lineage(checkpoint)
            if (
                fact.namespace != identity.namespace
                or fact.thread_id != identity.thread_id
                or fact.run_id != identity.run_id
                or fact.source != source
                or fact.source.thread != identity.thread
                or fact.source.runtime_profile != runtime_profile
                or fact.checkpoint_id != _checkpoint_id(checkpoint)
                or fact.checkpoint_ns
                != checkpoint.config.get("configurable", {}).get("checkpoint_ns", "")
                or (found is not None and found != fact)
            ):
                raise CancellationConflict("cancellation checkpoint evidence conflicts")
            found = fact
    return found


async def save_cancellation(
    saver: _CheckpointSaver,
    resolution: AgUiLineageResolution,
    *,
    identity: RunIdentity,
    request: AgUiResumeRequest,
    binding: AgUiResumeBinding,
    runtime_profile: str,
) -> None:
    """Commit one private non-task write and verify it before observing cancellation.

    The existing Run owner admits this operation. The saver is borrowed, and its
    write settles even if the caller is cancelled. No Graph checkpoint, native
    decision, or consuming resume-intent slot is created.
    """

    checkpoint = await saver.aget_tuple(resolution.config)
    source = None if checkpoint is None else _checkpoint_lineage(checkpoint)
    if checkpoint is None or source is None or resolution.parent_run_id is None:
        raise TinkerFinLifecycleError("cancellation source checkpoint is unavailable")
    fact = CancellationFact(
        **identity.model_dump(),
        source=source,
        parent_run_id=resolution.parent_run_id,
        checkpoint_id=_checkpoint_id(checkpoint),
        checkpoint_ns=checkpoint.config.get("configurable", {}).get(
            "checkpoint_ns", ""
        ),
        public_interrupt_ids=tuple(
            sorted(entry.interrupt_id for entry in request.entries)
        ),
        native_interrupt_ids=binding.native_interrupt_ids,
    )
    previous = await find_cancellation(
        saver, identity=identity, runtime_profile=runtime_profile
    )
    if previous is not None and previous != fact:
        raise CancellationConflict("runId owns another cancellation")
    if previous is None:
        await run_async_owned(
            lambda: saver.aput_writes(
                checkpoint.config,
                ((_CHANNEL, fact.model_dump(mode="json")),),
                _owner(identity),
            ),
            task_name="tinkerfin-abandonment-save",
        )
    if (
        await find_cancellation(
            saver, identity=identity, runtime_profile=runtime_profile
        )
        != fact
    ):
        raise CancellationConflict("cancellation write could not be verified")
