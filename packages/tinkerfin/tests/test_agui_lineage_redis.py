"""Real RedisSaver contracts for AG-UI lineage and resume indexing."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from ag_ui.core import RunFinishedEvent, RunFinishedInterruptOutcome
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from tinkerfin import AgUiResumeCheckpoint, AgUiResumeRequest, TinkerFin
from tinkerfin._agui_lineage_state import (
    LINEAGE_METADATA_KEY,
    RESUME_METADATA_KEY,
    LineageMarker,
)
from tinkerfin._checkpoint import NamespaceCheckpointer


class _ReviewModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _RedisResumeState(TypedDict, total=False):
    result: str


def _lineage_marker(checkpoint: CheckpointTuple) -> LineageMarker:
    """Read saver-owned canonical JSON independently of Graph state."""

    value = checkpoint.metadata.get(LINEAGE_METADATA_KEY)
    assert isinstance(value, str)
    return LineageMarker.model_validate_json(value)


async def _cleanup_saver(
    saver: AsyncRedisSaver,
    client: Redis,
    *,
    thread_ids: tuple[str, ...],
    checkpoint_prefix: str,
    write_prefix: str,
) -> None:
    """Delete test threads, auxiliary keys, and the two disposable indexes."""

    try:
        for thread_id in thread_ids:
            await saver.adelete_thread(thread_id)
    finally:
        auxiliary_keys: list[bytes] = []
        for thread_id in thread_ids:
            auxiliary_keys.extend(
                [
                    key
                    async for key in client.scan_iter(
                        match=f"write_keys_zset:{thread_id}:*",
                        count=100,
                    )
                ]
            )
            auxiliary_keys.extend(
                [
                    key
                    async for key in client.scan_iter(
                        match=f"{checkpoint_prefix}_latest:{thread_id}:*",
                        count=100,
                    )
                ]
            )
        if auxiliary_keys:
            await client.unlink(*auxiliary_keys)
        for index_name in (checkpoint_prefix, write_prefix):
            try:
                await client.execute_command("FT.DROPINDEX", index_name, "DD")
            except ResponseError:
                pass
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_scoped_checkpoints_resume_once(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    thread_id = f"tinkerfin:agui:{token}:thread:with:colons"
    checkpoint_prefix = f"tinkerfin:test:agui-lineage:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:agui-lineage:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    executions: list[str] = []

    @tool
    async def first_action() -> str:
        """Record the approved action."""
        executions.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record the second action only when approved."""
        executions.append("second")
        return "second"

    try:
        await saver.asetup()
        runtime = (
            TinkerFin(checkpointer=saver)
            .with_namespace("test")
            .build(
                model=_ReviewModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": name,
                                    "args": {},
                                    "id": name,
                                    "type": "tool_call",
                                }
                                for name in ("first_action", "second_action")
                            ],
                        ),
                        AIMessage(content="done"),
                    ]
                ),
                tools=[first_action, second_action],
                interrupt_on={"first_action": True, "second_action": True},
            )
        )
        parent = runtime.open_agui_run(
            thread_id=thread_id,
            run_id="run-parent",
            input={"messages": [HumanMessage(content="Run both actions")]},
        )
        parent_events = [event async for event in parent]
        assert parent.error is None
        terminal = parent_events[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        assert len(terminal.outcome.interrupts) == 2
        request = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": pending.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    if index == 0
                    else {"interruptId": pending.id, "status": "cancelled"}
                    for index, pending in enumerate(terminal.outcome.interrupts)
                ]
            }
        )
        checkpoints: list[AgUiResumeCheckpoint] = []

        async def fail_after_staging(value: AgUiResumeCheckpoint) -> None:
            checkpoints.append(value)
            raise RuntimeError("host settlement unavailable")

        failed_stream = runtime.open_agui_run(
            thread_id=thread_id,
            run_id="run-resume",
            resume=request,
            on_resume_saved=fail_after_staging,
        )
        failed_events = [event async for event in failed_stream]

        assert failed_events[-1].type.value == "RUN_ERROR"
        assert isinstance(failed_stream.error, RuntimeError)
        assert executions == []
        assert len(checkpoints) == 1
        staged = await NamespaceCheckpointer(saver, "test").aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        )
        assert staged is not None
        assert any(
            channel == RESUME_METADATA_KEY
            for _task_id, channel, _value in staged.pending_writes or ()
        )

        async def checkpointed(value: AgUiResumeCheckpoint) -> None:
            checkpoints.append(value)

        resumed_stream = runtime.open_agui_run(
            thread_id=thread_id,
            run_id="run-resume",
            resume=request,
            on_resume_saved=checkpointed,
        )
        resumed_events = [event async for event in resumed_stream]

        assert resumed_events[-1].type.value == "RUN_FINISHED"
        assert resumed_stream.error is None
        assert executions == ["first"]
        assert len(checkpoints) == 2
        assert checkpoints[0] == checkpoints[1]
        indexed = [
            checkpoint
            async for checkpoint in NamespaceCheckpointer(saver, "test").alist(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "",
                    }
                },
                filter={
                    LINEAGE_METADATA_KEY: LineageMarker(
                        namespace="test",
                        thread_id=thread_id,
                        run_id="run-resume",
                        parent_run_id="run-parent",
                        runtime_profile="deepagents-v2",
                        role="native",
                    ).canonical_json()
                },
            )
        ]
        assert indexed
        indexed_run_ids = tuple(
            _lineage_marker(checkpoint).run_id for checkpoint in indexed
        )
        assert set(indexed_run_ids) == {"run-resume"}, indexed_run_ids
    finally:
        await NamespaceCheckpointer(saver, "test").adelete_thread(thread_id)
        await _cleanup_saver(
            saver,
            client,
            thread_ids=(thread_id,),
            checkpoint_prefix=checkpoint_prefix,
            write_prefix=write_prefix,
        )


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_saver_preserves_colon_thread_subgraph_state(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    thread_id = f"tenant:{token}:conversation:subgraph:thread"
    checkpoint_prefix = f"tinkerfin:test:subgraph:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:subgraph:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )

    async def reviewed(state: _RedisResumeState) -> dict[str, object]:
        del state
        interrupt({"question": "continue?"})
        return {"result": "done"}

    child = StateGraph(_RedisResumeState)
    child.add_node("reviewed", reviewed)
    child.add_edge(START, "reviewed")
    child.add_edge("reviewed", END)
    parent = StateGraph(_RedisResumeState)
    parent.add_node("child", child.compile())
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    graph = parent.compile(checkpointer=saver)
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "run_id": "nested-run",
        }
    }
    try:
        await saver.asetup()
        async for _part in graph.astream(
            {},
            config,
            stream_mode=["tasks", "values"],
            version="v2",
            subgraphs=True,
        ):
            pass
        rows = [
            checkpoint
            async for checkpoint in saver.alist(
                {"configurable": {"thread_id": thread_id}}
            )
        ]
        namespaces = {
            checkpoint.config.get("configurable", {}).get("checkpoint_ns", "")
            for checkpoint in rows
        }
        assert "" in namespaces
        assert any(namespace for namespace in namespaces)
        assert any(checkpoint.pending_writes for checkpoint in rows)
        assert all(
            isinstance(checkpoint.checkpoint.get("pending_sends"), list)
            for checkpoint in rows
        )
        filtered = [
            checkpoint
            async for checkpoint in saver.alist(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "",
                        "run_id": "nested-run",
                    }
                }
            )
        ]
        assert filtered

        await saver.adelete_thread(thread_id)
        assert [
            checkpoint
            async for checkpoint in saver.alist(
                {"configurable": {"thread_id": thread_id}}
            )
        ] == []
    finally:
        await _cleanup_saver(
            saver,
            client,
            thread_ids=(thread_id,),
            checkpoint_prefix=checkpoint_prefix,
            write_prefix=write_prefix,
        )
