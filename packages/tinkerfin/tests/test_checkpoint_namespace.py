"""Async checkpoint scope, durable identity, and inherited saver contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    CheckpointMetadata,
    CheckpointTuple,
    empty_checkpoint,
)
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin._checkpoint import NamespaceCheckpointer


def _config(thread: str = "thread", graph_namespace: str = "") -> RunnableConfig:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": graph_namespace}}


@pytest.mark.parametrize("namespace", ["company-a", "用户\0空间", "😀" * 128])
async def test_checkpoint_threads_are_isolated_and_history_is_logical(
    namespace: str,
) -> None:
    shared = InMemorySaver()
    alpha = NamespaceCheckpointer(shared, namespace)
    beta = NamespaceCheckpointer(shared, "other")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"answer": "alpha"}
    checkpoint["channel_versions"] = {"answer": "1"}
    first = await alpha.aput(
        _config(), checkpoint, {"source": "input", "step": -1}, {"answer": "1"}
    )
    await alpha.aput_writes(first, [("pending", "alpha")], "task")
    second_checkpoint = empty_checkpoint()
    second = await alpha.aput(
        first, second_checkpoint, {"source": "loop", "step": 0}, {}
    )
    foreign = empty_checkpoint()
    foreign["channel_values"] = {"answer": "beta"}
    foreign["channel_versions"] = {"answer": "1"}
    await beta.aput(
        _config(), foreign, {"source": "input", "step": -1}, {"answer": "1"}
    )
    found = await alpha.aget_tuple(second)
    assert found is not None
    assert found.config.get("configurable", {})["thread_id"] == "thread"
    assert found.parent_config is not None
    assert found.parent_config.get("configurable", {})["thread_id"] == "thread"
    rows = [row async for row in alpha.alist(_config(), before=second)]
    assert [row.checkpoint["id"] for row in rows] == [checkpoint["id"]]
    assert rows[0].pending_writes == [("task", "pending", "alpha")]
    assert (await beta.aget(_config())) == foreign
    await alpha.adelete_thread("thread")
    assert await alpha.aget(_config()) is None
    assert await beta.aget(_config()) is not None


async def test_history_filters_graph_scope_before_pagination_and_closes_cursor() -> (
    None
):
    class BroadHistorySaver(InMemorySaver):
        async def alist(
            self,
            config: RunnableConfig | None,
            *,
            filter: dict[str, Any] | None = None,
            before: RunnableConfig | None = None,
            limit: int | None = None,
        ) -> AsyncIterator[CheckpointTuple]:
            assert config is not None
            del filter, before, limit
            # This models an upstream saver whose index only selects the thread.
            config = {
                "configurable": {
                    "thread_id": config.get("configurable", {})["thread_id"]
                }
            }
            async for row in super().alist(config):
                yield row

    view = NamespaceCheckpointer(BroadHistorySaver(), "scope")
    first = await view.aput(_config(), empty_checkpoint(), {"step": 1}, {})
    await view.aput(
        _config(graph_namespace="child:task"), empty_checkpoint(), {"step": 2}, {}
    )
    latest = await view.aput(first, empty_checkpoint(), {"step": 3}, {})
    rows = [
        row
        async for row in view.alist(
            _config(), before=latest, filter={"step": 1}, limit=1
        )
    ]
    assert [row.config for row in rows] == [first]
    assert [row async for row in view.alist(_config(), limit=0)] == []
    with pytest.raises(ValueError, match="thread_id"):
        [row async for row in view.alist(None)]


async def test_direct_checkpoint_writes_cannot_forge_managed_metadata() -> None:
    from tinkerfin._agui_lineage_state import LINEAGE_METADATA_KEY, RESUME_METADATA_KEY

    view = NamespaceCheckpointer(InMemorySaver(), "scope")
    injected: dict[str, Any] = {
        LINEAGE_METADATA_KEY: "forged",
        RESUME_METADATA_KEY: "forged",
    }
    saved_config = await view.aput(
        {
            "configurable": {"thread_id": "thread", "checkpoint_ns": "", **injected},
            "metadata": injected,
        },
        empty_checkpoint(),
        cast(CheckpointMetadata, injected),
        {},
    )
    saved = await view.aget_tuple(saved_config)
    assert saved is not None
    assert LINEAGE_METADATA_KEY not in saved.metadata
    assert RESUME_METADATA_KEY not in saved.metadata
