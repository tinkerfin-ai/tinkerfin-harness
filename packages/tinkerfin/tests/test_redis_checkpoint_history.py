"""Complete history and deletion through the borrowed Redis saver contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from typing import Any, Literal, TypedDict, cast
from uuid import UUID, uuid4

import pytest
from deepagents import DeepAgentState
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from redis.asyncio import Redis
from redis.commands.search.aggregation import AggregateRequest, AggregateResult

from tinkerfin._agui_lineage_state import (
    LINEAGE_METADATA_KEY,
    LineageMarker,
    bind_checkpoint_run,
)
from tinkerfin._checkpoint import (
    NamespaceCheckpointer,
    _encode_graph_scope,
    _physical_thread,
)
from tinkerfin.checkpoints import delete_thread
from tinkerfin.errors import TinkerFinLifecycleError
from tinkerfin_contracts import RunIdentity, ThreadIdentity

pytestmark = [pytest.mark.asyncio, pytest.mark.redis_e2e]


@pytest.fixture
async def history_saver(redis_checkpoint_url: str) -> AsyncIterator[AsyncRedisSaver]:
    token = uuid4().hex
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=f"test:history:{token}:checkpoint",
        checkpoint_write_prefix=f"test:history:{token}:write",
    )
    try:
        await saver.asetup()
        yield saver
    finally:
        try:
            try:
                for namespace in ("alpha", "beta"):
                    await delete_thread(
                        saver,
                        thread=ThreadIdentity(namespace=namespace, thread_id=token),
                    )
            finally:
                for index in (saver.checkpoints_index, saver.checkpoint_writes_index):
                    await client.execute_command(
                        "FT.DROPINDEX", index.schema.index.name, "DD"
                    )
        finally:
            await client.aclose()


def _thread(saver: AsyncRedisSaver, namespace: str = "alpha") -> ThreadIdentity:
    return ThreadIdentity(
        namespace=namespace,
        thread_id=saver.checkpoints_index.schema.index.name.split(":")[2],
    )


@pytest.mark.parametrize("cancel_after_snapshot", [False, True])
async def test_message_snapshot_survives_redis_reconnection_and_cancellation(
    history_saver: AsyncRedisSaver,
    redis_checkpoint_url: str,
    cancel_after_snapshot: bool,
) -> None:
    """A persisted message snapshot remains readable after a new saver is opened."""
    identity = _thread(history_saver)
    config: RunnableConfig = {"configurable": {"thread_id": identity.thread_id}}
    entered = asyncio.Event()
    release = asyncio.Event()
    block_next = False

    async def unchanged(state: DeepAgentState) -> dict[str, list[AnyMessage]]:
        del state
        if block_next:
            entered.set()
            await release.wait()
        return {}

    builder = StateGraph(DeepAgentState)
    builder.add_node("unchanged", unchanged)
    builder.add_edge(START, "unchanged")
    builder.add_edge("unchanged", END)
    graph = builder.compile(
        checkpointer=NamespaceCheckpointer(history_saver, identity.namespace)
    )
    for number in range(50):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="message", id=str(number))]},
            config,
            durability="sync",
        )
    expected = 50
    if cancel_after_snapshot:
        block_next = True
        task = asyncio.create_task(
            graph.ainvoke(
                {"messages": [HumanMessage(content="cancel", id="50")]},
                config,
                durability="sync",
            )
        )
        entered_task = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                (task, entered_task), return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                pytest.fail("The run ended before the cancellation boundary")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            entered_task.cancel()
            await asyncio.gather(task, entered_task, return_exceptions=True)
        expected += 1
        block_next = False

    async with Redis.from_url(redis_checkpoint_url, decode_responses=False) as client:
        reopened = AsyncRedisSaver(
            redis_client=client,
            checkpoint_prefix=history_saver.checkpoints_index.schema.index.name,
            checkpoint_write_prefix=history_saver.checkpoint_writes_index.schema.index.name,
        )
        restored = builder.compile(
            checkpointer=NamespaceCheckpointer(reopened, identity.namespace)
        )
        state = await restored.aget_state(config)
        assert [message.id for message in state.values["messages"]] == [
            str(number) for number in range(expected)
        ]
        result = await restored.ainvoke(
            {"messages": [HumanMessage(content="continue", id=str(expected))]},
            config,
            durability="sync",
        )
        assert [message.id for message in result["messages"]] == [
            str(number) for number in range(expected + 1)
        ]


async def _seed_history(saver: AsyncRedisSaver, count: int) -> list[str]:
    """Populate an upstream document fixture without depending on network timing."""

    identity = _thread(saver)
    view = NamespaceCheckpointer(saver, identity.namespace)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = str(UUID(int=1))
    config = await view.aput(
        {"configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}},
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        {},
    )
    await view.aput_writes(config, [("result", "root")], "task")
    physical = _physical_thread(identity, saver)
    raw_document: object = await saver._redis.execute_command(
        "JSON.GET", saver._make_redis_checkpoint_key(physical, "", checkpoint["id"])
    )
    raw_write: object = await saver._redis.execute_command(
        "JSON.GET",
        saver._make_redis_checkpoint_writes_key(
            physical, "", checkpoint["id"], "task", 0
        ),
    )
    assert isinstance(raw_document, dict) and isinstance(raw_write, dict)
    document = cast(dict[str, Any], raw_document)
    write = cast(dict[str, Any], raw_write)
    assert saver._key_registry is not None
    ids = [checkpoint["id"]]
    for start in range(2, count + 1, 128):
        pipeline = saver._redis.pipeline(transaction=False)
        for number in range(start, min(count + 1, start + 128)):
            checkpoint_id = str(UUID(int=number))
            ids.append(checkpoint_id)
            child = deepcopy(document)
            child["checkpoint_ns"] = _encode_graph_scope("child")
            child["checkpoint_id"] = checkpoint_id
            child["checkpoint"]["id"] = checkpoint_id
            child_write = {
                **write,
                "checkpoint_ns": _encode_graph_scope("child"),
                "checkpoint_id": checkpoint_id,
            }
            key = saver._make_redis_checkpoint_key(
                physical, _encode_graph_scope("child"), checkpoint_id
            )
            write_key = saver._make_redis_checkpoint_writes_key(
                physical, _encode_graph_scope("child"), checkpoint_id, "task", 0
            )
            pipeline.json().set(key, "$", child)
            pipeline.json().set(write_key, "$", child_write)
            pipeline.zadd(
                saver._key_registry.make_write_keys_zset_key(
                    physical, _encode_graph_scope("child"), checkpoint_id
                ),
                {write_key: 0},
            )
        await pipeline.execute()
    return ids


async def test_history_and_delete_cover_more_than_ten_thousand_checkpoints(
    history_saver: AsyncRedisSaver,
) -> None:
    saver = history_saver
    identity = _thread(saver)
    view = NamespaceCheckpointer(saver, identity.namespace)
    ids = await _seed_history(saver, 10001)
    config: RunnableConfig = {"configurable": {"thread_id": identity.thread_id}}

    roots = [
        row
        async for row in view.alist(
            {"configurable": {**config["configurable"], "checkpoint_ns": ""}}, limit=1
        )
    ]
    assert [row.checkpoint["id"] for row in roots] == ids[:1]
    rows = [row async for row in view.alist(config)]
    assert [row.checkpoint["id"] for row in rows] == ids[::-1]
    assert all(row.pending_writes == [("task", "result", "root")] for row in rows)
    before: RunnableConfig = {
        "configurable": {**config["configurable"], "checkpoint_id": ids[-2]}
    }
    selected = [row async for row in view.alist(config, before=before, limit=2)]
    assert [row.checkpoint["id"] for row in selected] == ids[-3:-5:-1]

    other = NamespaceCheckpointer(saver, "beta")
    await other.aput(
        {"configurable": {**config["configurable"], "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "input", "step": 0},
        {},
    )
    await view.aput_writes(
        {
            "configurable": {
                **config["configurable"],
                "checkpoint_ns": "orphan",
                "checkpoint_id": "orphan",
            }
        },
        [("result", "orphan")],
        "orphan-task",
    )
    physical = _physical_thread(identity, saver)
    await saver._redis.set(
        saver._make_redis_checkpoint_latest_key(physical, "orphan"), "missing"
    )
    await delete_thread(saver, thread=identity)
    assert [
        key async for key in saver._redis.scan_iter(match=f"*:{physical}:*", count=128)
    ] == []
    assert await other.aget_tuple(config) is not None
    assert await saver._redis.execute_command("PING") is True
    await delete_thread(saver, thread=identity)


async def test_config_metadata_is_available_to_history_filters(
    history_saver: AsyncRedisSaver,
) -> None:
    identity = _thread(history_saver)
    view = NamespaceCheckpointer(history_saver, identity.namespace)
    config: RunnableConfig = {
        "configurable": {
            "thread_id": identity.thread_id,
            "checkpoint_ns": "",
            "run_id": "planning",
        },
        "metadata": {"purpose": "approval"},
    }
    await view.aput(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
    rows = [
        row
        async for row in view.alist(
            config, filter={"run_id": "planning", "purpose": "approval"}
        )
    ]
    assert len(rows) == 1


async def test_history_uses_bounded_queries_without_retained_cursors(
    history_saver: AsyncRedisSaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver = history_saver
    identity = _thread(saver)
    view = NamespaceCheckpointer(saver, identity.namespace)
    ids = await _seed_history(saver, 257)
    config: RunnableConfig = {"configurable": {"thread_id": identity.thread_id}}
    aggregate = saver.checkpoints_index.aggregate
    sizes: list[int] = []

    async def record(query: AggregateRequest) -> AggregateResult:
        result = await aggregate(query)
        sizes.append(len(result.rows))
        assert result.cursor is None or result.cursor.cid == 0
        return result

    monkeypatch.setattr(saver.checkpoints_index, "aggregate", record)
    async with aclosing(view.alist(config)) as history:
        assert (await anext(history)).checkpoint["id"] == ids[-1]
    assert len(sizes) == 1
    assert [row.checkpoint["id"] async for row in view.alist(config)] == ids[::-1]
    assert max(sizes) <= 128
    assert await saver._redis.execute_command("PING") is True


async def test_cancelled_history_closes_the_pending_query(
    history_saver: AsyncRedisSaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver = history_saver
    identity = _thread(saver)
    view = NamespaceCheckpointer(saver, identity.namespace)
    await _seed_history(saver, 2)
    aggregate = saver.checkpoints_index.aggregate
    entered = asyncio.Event()
    exited = asyncio.Event()
    blocked = asyncio.Event()

    async def held(query: AggregateRequest) -> AggregateResult:
        result = await aggregate(query)
        assert result.cursor is None or result.cursor.cid == 0
        entered.set()
        try:
            await blocked.wait()
        finally:
            exited.set()
        return result

    monkeypatch.setattr(saver.checkpoints_index, "aggregate", held)
    async with aclosing(
        view.alist({"configurable": {"thread_id": identity.thread_id}})
    ) as history:
        consumer = asyncio.create_task(anext(history))
        await entered.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert exited.is_set()
    assert await saver._redis.execute_command("PING") is True


@pytest.mark.parametrize("cancelled", [False, True])
async def test_interrupted_deletion_is_repeatable(
    history_saver: AsyncRedisSaver,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    saver = history_saver
    identity = _thread(saver)
    await _seed_history(saver, 257)
    scan = saver._redis.scan_iter
    prefix = f"{saver._checkpoint_prefix}:{_physical_thread(identity, saver)}:*"
    original = [key async for key in scan(match=prefix, count=128)]
    reached = asyncio.Event()
    blocked = asyncio.Event()

    async def interrupted_scan(*, match: str, count: int) -> AsyncIterator[object]:
        seen = 0
        async for key in scan(match=match, count=count):
            yield key
            seen += 1
            if seen == 128:
                reached.set()
                if cancelled:
                    await blocked.wait()
                raise TimeoutError("test Redis scan deadline")

    with monkeypatch.context() as patched:
        patched.setattr(saver._redis, "scan_iter", interrupted_scan)
        if cancelled:
            deletion = asyncio.create_task(delete_thread(saver, thread=identity))
            await reached.wait()
            deletion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deletion
        else:
            with pytest.raises(TinkerFinLifecycleError) as failure:
                await delete_thread(saver, thread=identity)
            assert isinstance(failure.value.cause, TimeoutError)
    remaining = [key async for key in scan(match=prefix, count=128)]
    assert 0 < len(remaining) < len(original)
    await delete_thread(saver, thread=identity)
    assert [key async for key in scan(match=prefix, count=128)] == []
    assert await saver._redis.execute_command("PING") is True


async def test_history_pages_equal_ids_in_distinct_graph_namespaces(
    history_saver: AsyncRedisSaver,
) -> None:
    saver = history_saver
    identity = _thread(saver)
    view = NamespaceCheckpointer(saver, identity.namespace)
    namespaces = [f"图'\"\\\n[{number:04d}]" for number in range(257)]
    checkpoint = empty_checkpoint()
    for namespace in namespaces:
        await view.aput(
            {
                "configurable": {
                    "thread_id": identity.thread_id,
                    "checkpoint_ns": namespace,
                }
            },
            checkpoint,
            {"source": "loop", "step": 1},
            {},
        )
    rows = [
        row
        async for row in view.alist({"configurable": {"thread_id": identity.thread_id}})
    ]
    assert len(rows) == len(namespaces)
    assert {row.config.get("configurable", {})["checkpoint_ns"] for row in rows} == set(
        namespaces
    )


async def test_incomplete_history_fails_instead_of_silently_truncating(
    history_saver: AsyncRedisSaver, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver = history_saver
    identity = _thread(saver)
    await _seed_history(saver, 257)
    aggregate = saver.checkpoints_index.aggregate
    first = True

    async def incomplete(query: AggregateRequest) -> AggregateResult:
        nonlocal first
        result = await aggregate(query)
        if first:
            first = False
            result.rows = result.rows[1:]
        return result

    monkeypatch.setattr(saver.checkpoints_index, "aggregate", incomplete)
    with pytest.raises(TinkerFinLifecycleError, match="incomplete"):
        async for _row in NamespaceCheckpointer(saver, identity.namespace).alist(
            {"configurable": {"thread_id": identity.thread_id}}
        ):
            pass


async def test_delete_escapes_configured_prefixes_and_removes_orphan_auxiliary_keys(
    history_saver: AsyncRedisSaver,
) -> None:
    identity = _thread(history_saver)
    client = history_saver._redis
    prefix = f"test:history:{identity.thread_id}:literal[?*]"
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=prefix,
        checkpoint_write_prefix=f"{prefix}:write",
    )
    physical = _physical_thread(identity, saver)
    adjacent = f"test:history:{identity.thread_id}:literalt:{physical}:keep"
    try:
        await saver.asetup()
        await NamespaceCheckpointer(saver, identity.namespace).aput(
            {"configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}},
            empty_checkpoint(),
            {"source": "input", "step": 0},
            {},
        )
        assert saver._key_registry is not None
        registry = saver._key_registry.make_write_keys_zset_key(
            physical, "orphan", "orphan"
        )
        latest = saver._make_redis_checkpoint_latest_key(physical, "orphan")
        await client.zadd(registry, {"missing": 0})
        await client.set(latest, "missing")
        await client.set(adjacent, "keep")
        await delete_thread(saver, thread=identity)
        assert await client.get(adjacent) == b"keep"
        assert await client.exists(registry, latest) == 0
        assert (
            await NamespaceCheckpointer(saver, identity.namespace).aget_tuple(
                {"configurable": {"thread_id": identity.thread_id}}
            )
            is None
        )
    finally:
        await delete_thread(saver, thread=identity)
        await client.delete(adjacent)
        for index in (saver.checkpoints_index, saver.checkpoint_writes_index):
            await client.execute_command("FT.DROPINDEX", index.schema.index.name, "DD")


@pytest.mark.parametrize(
    "location", ["checkpoint_metadata", "config_metadata", "configurable"]
)
@pytest.mark.parametrize("managed", [False, True])
async def test_metadata_key_normalization_cannot_replace_run_ownership(
    history_saver: AsyncRedisSaver,
    location: Literal["checkpoint_metadata", "config_metadata", "configurable"],
    managed: bool,
) -> None:
    identity = _thread(history_saver)
    config: RunnableConfig = {
        "configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}
    }
    metadata: dict[str, object] = {"source": "input", "step": 0}
    forged = LineageMarker(
        namespace=identity.namespace,
        thread_id=identity.thread_id,
        run_id="forged",
        runtime_profile="deepagents-v2",
        role="native",
    )
    incoming = {LINEAGE_METADATA_KEY + "\0": forged.canonical_json()}
    if location == "checkpoint_metadata":
        metadata.update(incoming)
    elif location == "config_metadata":
        config["metadata"] = incoming
    else:
        config["configurable"].update(incoming)
    if managed:
        config = bind_checkpoint_run(
            config,
            identity=RunIdentity(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                run_id="actual",
            ),
            parent_run_id=None,
            runtime_profile="deepagents-v2",
        )
    view = NamespaceCheckpointer(history_saver, identity.namespace)
    saved = await view.aput(
        config, empty_checkpoint(), cast(CheckpointMetadata, metadata), {}
    )
    row = await view.aget_tuple(saved)
    assert row is not None
    actual = row.metadata.get(LINEAGE_METADATA_KEY)
    if managed:
        assert isinstance(actual, str)
        assert LineageMarker.model_validate_json(actual).run_id == "actual"
    else:
        assert actual is None


@pytest.mark.parametrize("field", ["namespace", "thread_id"])
async def test_redis_preserves_identity_control_characters(
    history_saver: AsyncRedisSaver, field: Literal["namespace", "thread_id"]
) -> None:
    original = _thread(history_saver)
    identity = ThreadIdentity(
        namespace="alpha\0space" if field == "namespace" else original.namespace,
        thread_id=original.thread_id + "\0thread"
        if field == "thread_id"
        else original.thread_id,
    )
    view = NamespaceCheckpointer(history_saver, identity.namespace)
    config: RunnableConfig = {
        "configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}
    }
    try:
        saved = await view.aput(
            config, empty_checkpoint(), {"source": "input", "step": 0}, {}
        )
        row = await view.aget_tuple(saved)
        assert row is not None
        assert row.config.get("configurable", {}).get("thread_id") == identity.thread_id
        assert len([item async for item in view.alist(config)]) == 1
    finally:
        await delete_thread(history_saver, thread=identity)


async def test_redis_preserves_literal_unicode_escape_metadata(
    history_saver: AsyncRedisSaver,
) -> None:
    identity = _thread(history_saver)
    view = NamespaceCheckpointer(history_saver, identity.namespace)
    note = r"literal \u0000x"
    config: RunnableConfig = {
        "configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""},
        "metadata": {"note": note},
    }
    saved = await view.aput(
        config, empty_checkpoint(), {"source": "input", "step": 0}, {}
    )
    row = await view.aget_tuple(saved)
    assert row is not None and row.metadata.get("note") == note
    assert len([item async for item in view.alist(config, filter={"note": note})]) == 1


@pytest.mark.parametrize("changed", ["checkpoint", "write"])
async def test_redis_saver_prefixes_isolate_writes_and_allow_reconnection(
    history_saver: AsyncRedisSaver, changed: Literal["checkpoint", "write"]
) -> None:
    first = history_saver
    identity = _thread(first)
    checkpoint_prefix = first._checkpoint_prefix + (
        ":other" if changed == "checkpoint" else ""
    )
    write_prefix = first._checkpoint_write_prefix + (
        ":other" if changed == "write" else ""
    )
    second = AsyncRedisSaver(
        redis_client=first._redis,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    second_view = NamespaceCheckpointer(second, identity.namespace)
    config: RunnableConfig = {
        "configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}
    }
    checkpoint = empty_checkpoint()
    try:
        await second.asetup()
        for name, saver in (("first", first), ("second", second)):
            view = NamespaceCheckpointer(saver, identity.namespace)
            saved = await view.aput(
                config, checkpoint, {"source": "input", "step": 0}, {}
            )
            await view.aput_writes(saved, [("result", name)], name)
        before = await second_view.aget_tuple(config)
        assert before is not None and before.pending_writes == [
            ("second", "result", "second")
        ]
        await delete_thread(first, thread=identity)
        reconnected = AsyncRedisSaver(
            redis_client=first._redis,
            checkpoint_prefix=checkpoint_prefix,
            checkpoint_write_prefix=write_prefix,
        )
        await reconnected.asetup()
        after = await NamespaceCheckpointer(reconnected, identity.namespace).aget_tuple(
            config
        )
        assert after is not None and after.pending_writes == before.pending_writes
        await delete_thread(reconnected, thread=identity)
        assert await second_view.aget_tuple(config) is None
    finally:
        await delete_thread(second, thread=identity)
        new_index = (
            second.checkpoints_index
            if changed == "checkpoint"
            else second.checkpoint_writes_index
        )
        await first._redis.execute_command(
            "FT.DROPINDEX", new_index.schema.index.name, "DD"
        )


class _CounterState(TypedDict):
    counter: int


async def test_redis_history_preserves_real_subgraph_names_with_nul(
    history_saver: AsyncRedisSaver,
) -> None:
    identity = _thread(history_saver)
    view = NamespaceCheckpointer(history_saver, identity.namespace)

    async def increment(state: _CounterState) -> _CounterState:
        return {"counter": state["counter"] + 1}

    child = StateGraph(_CounterState)
    child.add_node("increment", increment)
    child.add_edge(START, "increment")
    child.add_edge("increment", END)
    parent = StateGraph(_CounterState)
    parent.add_node("child\0scope", child.compile())
    parent.add_edge(START, "child\0scope")
    parent.add_edge("child\0scope", END)
    graph = parent.compile(checkpointer=view)
    config: RunnableConfig = {"configurable": {"thread_id": identity.thread_id}}
    assert await graph.ainvoke({"counter": 0}, config, durability="sync") == {
        "counter": 1
    }
    rows = [item async for item in view.alist(config)]
    assert any(
        str(item.config.get("configurable", {}).get("checkpoint_ns", "")).startswith(
            "child\0scope:"
        )
        for item in rows
    )


async def test_redis_borrowed_resp3_client_reads_and_deletes_history(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    client = Redis.from_url(
        redis_checkpoint_url,
        protocol=3,
        decode_responses=False,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=f"test:resp3:{token}:checkpoint",
        checkpoint_write_prefix=f"test:resp3:{token}:write",
    )
    identity = ThreadIdentity(namespace="alpha", thread_id=token)
    try:
        await saver.asetup()
        view = NamespaceCheckpointer(saver, identity.namespace)
        config: RunnableConfig = {
            "configurable": {"thread_id": token, "checkpoint_ns": ""}
        }
        saved = await view.aput(
            config, empty_checkpoint(), {"source": "input", "step": 0}, {}
        )
        await view.aput_writes(saved, [("result", "resp3")], "task")
        assert await view.aget_tuple(saved) is not None
        rows = [item async for item in view.alist(config)]
        assert len(rows) == 1 and rows[0].pending_writes == [
            ("task", "result", "resp3")
        ]
        await delete_thread(saver, thread=identity)
        assert await view.aget_tuple(config) is None
    finally:
        try:
            await delete_thread(saver, thread=identity)
            for index in (saver.checkpoints_index, saver.checkpoint_writes_index):
                await client.execute_command(
                    "FT.DROPINDEX", index.schema.index.name, "DD"
                )
        finally:
            await client.aclose()


async def test_redis_reserved_checkpoint_id_cannot_silently_return_latest(
    history_saver: AsyncRedisSaver,
) -> None:
    identity = _thread(history_saver)
    view = NamespaceCheckpointer(history_saver, identity.namespace)
    config: RunnableConfig = {
        "configurable": {"thread_id": identity.thread_id, "checkpoint_ns": ""}
    }
    for number in (0, 1):
        checkpoint = empty_checkpoint()
        checkpoint["id"] = str(UUID(int=number))
        await view.aput(config, checkpoint, {"source": "input", "step": 0}, {})
    with pytest.raises(TinkerFinLifecycleError, match="another requested location"):
        await view.aget_tuple(
            {
                "configurable": {
                    **config["configurable"],
                    "checkpoint_id": str(UUID(int=0)),
                }
            }
        )
    with pytest.raises(TinkerFinLifecycleError, match="another indexed location"):
        async for _row in view.alist(config):
            pass
