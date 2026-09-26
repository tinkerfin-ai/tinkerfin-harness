"""Private delegated outcomes retain exact checkpoint ownership and write slots."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import DelegationReplayError, RunIdentity
from tinkerfin._agui_lineage_state import bind_checkpoint_run
from tinkerfin._checkpoint import NamespaceCheckpointer
from tinkerfin._delegation_journal import DELEGATION_RECORD_CHANNEL, DelegationJournal


async def _journal(saver: InMemorySaver) -> DelegationJournal:
    checkpoint = empty_checkpoint()
    config = bind_checkpoint_run(
        {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}},
        identity=RunIdentity(namespace="journal", thread_id="thread", run_id="request"),
        parent_run_id=None,
        runtime_profile="deepagents-v2",
    )
    scoped = NamespaceCheckpointer(saver, "journal")
    await scoped.aput(
        config, checkpoint, {"source": "loop", "step": 0, "parents": {}}, {}
    )
    return DelegationJournal(
        scoped,
        config,
        graph_namespace="",
        checkpoint_id=checkpoint["id"],
        parent_task_id="parent",
        tool_call_id="delegate",
    )


async def test_private_outcome_and_native_return_keep_independent_slots() -> None:
    journal = await _journal(InMemorySaver())
    key = journal.attempt_key(0)
    record = await journal.save(key, "outcome", {"decision": "retry"})
    await journal.saver.aput_writes(
        journal.config, [("__return__", "native")], "parent"
    )
    assert await journal.read(key, "outcome") == record

    checkpoint = await journal.saver.aget_tuple(journal.config)
    assert checkpoint is not None and checkpoint.checkpoint["channel_values"] == {}
    assert ("parent", "__return__", "native") in (checkpoint.pending_writes or ())
    assert await journal.save(key, "outcome", {"decision": "retry"}) == record
    with pytest.raises(DelegationReplayError, match="changed recorded"):
        await journal.save(key, "outcome", {"decision": "raise"})
    assert await journal.read(key, "outcome") == record


async def test_recorded_json_types_are_not_collapsed_by_python_equality() -> None:
    journal = await _journal(InMemorySaver())
    await journal.save(journal.request_key, "request", {"argument": False})
    with pytest.raises(DelegationReplayError, match="changed recorded"):
        await journal.save(journal.request_key, "request", {"argument": 0})


class _GatedSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if any(channel == DELEGATION_RECORD_CHANNEL for channel, _value in writes):
            self.entered.set()
            try:
                await self.release.wait()
                await super().aput_writes(config, writes, task_id, task_path)
            finally:
                self.closed.set()
        else:
            await super().aput_writes(config, writes, task_id, task_path)


async def test_cancelled_writer_joins_its_durable_write_before_releasing_owner() -> (
    None
):
    saver = _GatedSaver()
    journal = await _journal(saver)
    key = journal.attempt_key(0)
    writing = asyncio.create_task(journal.save(key, "outcome", {"decision": "retry"}))
    try:
        await saver.entered.wait()
        writing.cancel()
        saver.release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
    finally:
        saver.release.set()
        if not writing.done():
            writing.cancel()
            await asyncio.gather(writing, return_exceptions=True)
    assert saver.closed.is_set()
    record = await journal.read(key, "outcome")
    assert record is not None and record.payload == {"decision": "retry"}


class _CompetingSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.writers = 0
        self.both_saved = asyncio.Event()

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        # Both callers must observe an empty group before either write is visible.
        self.writers += 1
        if self.writers == 2:
            self.both_saved.set()
        await self.both_saved.wait()
        await super().aput_writes(config, writes, task_id, task_path)


async def test_competing_records_are_preserved_and_rejected_without_last_write_wins() -> (
    None
):
    saver = _CompetingSaver()
    journal = await _journal(saver)
    key = journal.attempt_key(0)
    results = await asyncio.gather(
        journal.save(key, "outcome", {"decision": "retry"}),
        journal.save(key, "outcome", {"decision": "raise"}),
        return_exceptions=True,
    )
    assert any(isinstance(result, DelegationReplayError) for result in results)
    with pytest.raises(DelegationReplayError, match="conflicting records"):
        await journal.read(key, "outcome")
    checkpoint = await journal.saver.aget_tuple(journal.config)
    assert checkpoint is not None
    assert len(checkpoint.pending_writes or ()) == 2


async def test_corrupted_record_digest_cannot_authorize_replay() -> None:
    journal = await _journal(InMemorySaver())
    key = journal.attempt_key(0)
    record = await journal.save(key, "outcome", {"decision": "retry"})
    await journal.saver.aput_writes(
        journal.config,
        [(DELEGATION_RECORD_CHANNEL, record.model_dump(mode="json"))],
        f"tinkerfin-delegation:{key}:outcome:invalid-digest",
    )
    with pytest.raises(DelegationReplayError, match="conflicting ownership"):
        await journal.read(key, "outcome")
