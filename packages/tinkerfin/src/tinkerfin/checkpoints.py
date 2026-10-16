"""Administer persisted conversations without creating an agent or opening a model."""

from __future__ import annotations

from typing import TypeVar

from langgraph.checkpoint.base import BaseCheckpointSaver

from tinkerfin_contracts import ThreadIdentity

from ._checkpoint import NamespaceCheckpointer
from .errors import TinkerFinError, TinkerFinLifecycleError

V = TypeVar("V", int, float, str)


async def delete_thread(
    checkpointer: BaseCheckpointSaver[V], *, thread: ThreadIdentity
) -> None:
    """Delete a conversation's checkpoints and pending writes in its namespace.

    Stop the conversation's runs and prevent new runs before calling this function.
    The caller owns the borrowed saver's connection and resource lifetime. Other
    namespaces and application records are unaffected. Cancellation propagates;
    deletion atomicity follows the saver's contract and an interrupted delete may
    need to be repeated.

    Args:
        checkpointer: Open saver supplied to the Runtime when it was built.
        thread: Complete namespace and logical thread identity to remove.

    Raises:
        TypeError: The saver or thread has the wrong type.
        TinkerFinLifecycleError: The saver cannot complete the deletion.
    """

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TypeError("checkpointer must implement BaseCheckpointSaver")
    if not isinstance(thread, ThreadIdentity):
        raise TypeError("thread must be a ThreadIdentity")
    try:
        await NamespaceCheckpointer(checkpointer, thread.namespace).adelete_thread(
            thread.thread_id
        )
    except TinkerFinError:
        raise
    except Exception as error:
        raise TinkerFinLifecycleError(
            "checkpoint deletion failed", cause=error
        ) from error


__all__ = ["delete_thread"]
