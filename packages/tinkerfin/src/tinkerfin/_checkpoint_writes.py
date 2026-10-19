"""Typed checkpoint ownership and selected writes without loading Graph state."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.base import CheckpointMetadata, CheckpointTuple, PendingWrite

from .errors import TinkerFinLifecycleError


@dataclass(frozen=True, slots=True)
class CheckpointWriteView:
    """Retain a real checkpoint anchor and the writes selected by its caller.

    This view contains no channel values, pending sends, or graph state. It cannot
    be passed to LangGraph as a CheckpointTuple or used to resume execution.
    """

    thread_id: str
    graph_namespace: str
    checkpoint_id: str
    metadata: CheckpointMetadata
    pending_writes: tuple[PendingWrite, ...]


def checkpoint_write_view(
    saved: CheckpointTuple, owner_prefix: str | None = None
) -> CheckpointWriteView:
    """Select writes from a standard saver's complete native checkpoint result."""

    options = saved.config.get("configurable", {})
    thread = options.get("thread_id")
    namespace = options.get("checkpoint_ns", "")
    checkpoint = options.get("checkpoint_id")
    if (
        not isinstance(thread, str)
        or not isinstance(namespace, str)
        or not isinstance(checkpoint, str)
        or checkpoint != saved.checkpoint["id"]
    ):
        raise TinkerFinLifecycleError("checkpoint writes have invalid coordinates")
    return CheckpointWriteView(
        thread,
        namespace,
        checkpoint,
        saved.metadata,
        tuple(
            write
            for write in saved.pending_writes or ()
            if owner_prefix is None or write[0].startswith(owner_prefix)
        ),
    )


__all__ = ["CheckpointWriteView", "checkpoint_write_view"]
