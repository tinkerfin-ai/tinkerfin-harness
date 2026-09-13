"""Runtime checkpoints, compiled subagents, and current namespace ownership."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest
from ag_ui.core import RunFinishedEvent, RunFinishedInterruptOutcome
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableBinding, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from tinkerfin import AgUiResumeRequest, TinkerFin
from tinkerfin._agui_lineage_state import (
    LINEAGE_CONFIG_KEY,
    LINEAGE_METADATA_KEY,
    RESUME_METADATA_KEY,
    LineageMarker,
)
from tinkerfin._checkpoint import NamespaceCheckpointer
from tinkerfin.deep_agent import create_graph
from tinkerfin_native_stream import NativeRuntimeInterrupt


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


def _approve(terminal: object) -> AgUiResumeRequest:
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": item.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
                for item in terminal.outcome.interrupts
            ]
        }
    )


@pytest.mark.parametrize("native_scalar", [False, True])
async def test_subagent_approval_rounds_keep_original_retry_and_current_review_separate(
    native_scalar: bool,
) -> None:
    actions: list[str] = []

    @tool
    async def first() -> str:
        """Perform the first approved action."""
        actions.append("first")
        return "first done"

    @tool
    async def second() -> str:
        """Perform the second approved action."""
        actions.append("second")
        return "second done"

    child_responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": {}, "id": f"{name}-call"}],
        )
        for name in ("first", "second")
    ]
    child_responses.append(AIMessage(content="child done"))
    child = _Model(responses=child_responses)
    root = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Do both", "subagent_type": "worker"},
                        "id": "parent-call",
                    }
                ],
            ),
            AIMessage(content="parent done"),
        ]
    )
    saver = InMemorySaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("business")
        .build(
            model=root,
            subagents=[
                {
                    "name": "worker",
                    "description": "Do both steps",
                    "system_prompt": "Use both tools",
                    "tools": [first, second],
                    "interrupt_on": {"first": True, "second": True},
                    "model": child,
                }
            ],
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="A",
        messages=[{"id": "user", "role": "user", "content": "Do both"}],
    )
    first_events = [event async for event in initial]
    assert initial.error is None
    first_request = _approve(first_events[-1])
    if native_scalar:
        native = runtime.open_run(
            thread_id="thread",
            run_id="C",
            input=Command(resume={"decisions": [{"type": "approve"}]}),
        )
        parts = [part async for part in native]
        assert native.error is None
        assert actions == ["first"]
        pending = [
            NativeRuntimeInterrupt.model_validate(value)
            for part in parts
            if part.get("type") == "values" and part.get("ns") == ()
            for value in cast(tuple[object, ...], part.get("interrupts", ()))
        ]
        request = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": pending[-1].id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                ]
            }
        )
        released: list[bool] = []

        async def not_saved() -> None:
            released.append(True)

        unsafe = runtime.open_agui_run(
            thread_id="thread",
            run_id="B",
            resume=request,
            on_resume_not_saved=not_saved,
        )
        [event async for event in unsafe]
        assert unsafe.error is not None
        assert "global decision" in str(unsafe.error)
        assert actions == ["first"]
        assert released == [True]
        return
    first_resume = runtime.open_agui_run(
        thread_id="thread", run_id="B", resume=first_request
    )
    second_events = [event async for event in first_resume]
    assert first_resume.error is None
    second_request = _approve(second_events[-1])
    assert actions == ["first"]
    assert (
        first_request.entries[0].interrupt_id != second_request.entries[0].interrupt_id
    )
    retry = runtime.open_agui_run(thread_id="thread", run_id="B", resume=first_request)
    retried_events = [event async for event in retry]
    assert retry.error is None
    assert _approve(retried_events[-1]) == second_request
    assert actions == ["first"]
    last = runtime.open_agui_run(thread_id="thread", run_id="C", resume=second_request)
    last_events = [event async for event in last]
    assert last.error is None
    assert isinstance(last_events[-1], RunFinishedEvent)
    assert last_events[-1].outcome is not None
    assert last_events[-1].outcome.type == "success"
    assert actions == ["first", "second"]


async def test_unrelated_checkpoint_history_does_not_prevent_subagent_approval() -> (
    None
):
    actions: list[str] = []

    @tool
    async def perform() -> str:
        """Perform the approved action."""
        actions.append("performed")
        return "done"

    saver = InMemorySaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("business")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {
                                    "description": "Perform one action",
                                    "subagent_type": "worker",
                                },
                                "id": "root-tool",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Perform one action",
                    "system_prompt": "Use the tool",
                    "tools": [perform],
                    "interrupt_on": {"perform": True},
                    "model": _Model(
                        responses=[
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"name": "perform", "args": {}, "id": "child-tool"}
                                ],
                            ),
                            AIMessage(content="done"),
                        ]
                    ),
                }
            ],
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="A",
        messages=[{"id": "user", "role": "user", "content": "Perform"}],
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    request = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": item.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
                for item in terminal.outcome.interrupts
            ]
        }
    )
    view = NamespaceCheckpointer(saver, "business")
    root_config: RunnableConfig = {
        "configurable": {"thread_id": "thread", "checkpoint_ns": ""}
    }
    before = await view.aget_tuple(root_config)
    assert before is not None
    marker = LineageMarker.model_validate_json(
        cast(str, before.metadata.get(LINEAGE_METADATA_KEY))
    )
    archived: RunnableConfig = {
        "configurable": {
            "thread_id": "thread",
            "checkpoint_ns": "archived:finished-task",
            LINEAGE_CONFIG_KEY: marker,
        }
    }
    for step in range(4097):
        await view.aput(
            archived, empty_checkpoint(), {"source": "update", "step": step}, {}
        )
    after = await view.aget_tuple(root_config)
    assert after is not None
    assert after.checkpoint == before.checkpoint
    assert after.pending_writes == before.pending_writes
    resumed = runtime.open_agui_run(thread_id="thread", run_id="B", resume=request)
    events = [event async for event in resumed]
    assert resumed.error is None
    assert isinstance(events[-1], RunFinishedEvent)
    assert actions == ["performed"]


async def test_public_runtime_and_direct_graph_share_only_their_namespaced_thread() -> (
    None
):
    saver = InMemorySaver()
    for namespace in ("alpha", "用户\0空间"):
        runtime = (
            TinkerFin(checkpointer=saver)
            .with_namespace(namespace)
            .build(model=_Model(responses=[AIMessage(content=namespace)]))
        )
        await runtime.ainvoke(
            thread_id="thread\0😀",
            run_id="run\0😀",
            input={"messages": [HumanMessage(content=namespace)]},
        )
        view = NamespaceCheckpointer(saver, namespace)
        saved = await view.aget_tuple({"configurable": {"thread_id": "thread\0😀"}})
        assert saved is not None
        marker = LineageMarker.model_validate_json(
            cast(str, saved.metadata.get(LINEAGE_METADATA_KEY))
        )
        assert marker.namespace == namespace
        assert marker.run_id == "run\0😀"
        graph = await create_graph(runtime)
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="next")]},
            {"configurable": {"thread_id": "thread\0😀"}},
        )
        messages = result["messages"]
        assert isinstance(messages, list)
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == namespace
        direct = await view.aget_tuple({"configurable": {"thread_id": "thread\0😀"}})
        assert direct is not None
        assert LINEAGE_METADATA_KEY not in direct.metadata
        assert RESUME_METADATA_KEY not in direct.metadata


class _ChildState(MessagesState, total=False):
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]


async def test_parallel_compiled_outputs_cannot_replace_checkpoint_ownership() -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    builder = StateGraph(_ChildState)

    async def finish(state: _ChildState, runtime: Runtime) -> dict[str, object]:
        del state
        assert runtime.store is not None
        await runtime.store.aput(("files",), "child", {"value": "scoped"})
        return {
            "messages": [AIMessage(content="child done")],
            "_tinkerfin_lineage": {"namespace": "foreign", "runId": "foreign"},
            "_tinkerfin_resume": {"runId": "foreign"},
        }

    builder.add_node("finish", finish)
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    child = builder.compile().with_config(
        {
            "tags": ["worker"],
            "metadata": {LINEAGE_METADATA_KEY: "forged", RESUME_METADATA_KEY: "forged"},
            "configurable": {"business_option": "retained"},
        }
    )
    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Inspect", "subagent_type": name},
                        "id": name,
                    }
                    for name in ("reader", "writer")
                ],
            ),
            AIMessage(content="root done"),
        ]
    )
    runtime = (
        TinkerFin(checkpointer=saver, store=store)
        .with_namespace("scope")
        .build(
            model=model,
            subagents=[
                {
                    "name": name,
                    "description": "Inspect the workspace",
                    "runnable": child,
                }
                for name in ("reader", "writer")
            ],
        )
    )
    await runtime.ainvoke(thread_id="thread", run_id="root", input={"messages": []})
    rows = [
        row
        async for row in NamespaceCheckpointer(saver, "scope").alist(
            {"configurable": {"thread_id": "thread"}}
        )
    ]
    assert any(row.config.get("configurable", {}).get("checkpoint_ns") for row in rows)
    for row in rows:
        marker = LineageMarker.model_validate_json(
            cast(str, row.metadata.get(LINEAGE_METADATA_KEY))
        )
        assert (marker.namespace, marker.run_id) == ("scope", "root")
        assert RESUME_METADATA_KEY not in row.metadata
    assert await store.aget(("files",), "child") is None
    assert await store.aget(("c2NvcGU", "files"), "child") is not None


@pytest.mark.parametrize(
    "mode",
    [
        "saver",
        "store",
        "disabled",
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "checkpoint_map",
        "__pregel_checkpointer",
        "__pregel_runtime",
        "binding",
        "dynamic",
    ],
)
def test_compiled_subagent_resource_overrides_fail_at_build(mode: str) -> None:
    builder = StateGraph(MessagesState)
    builder.add_edge(START, END)
    child = builder.compile(
        checkpointer=InMemorySaver()
        if mode == "saver"
        else False
        if mode == "disabled"
        else None,
        store=InMemoryStore() if mode == "store" else None,
    )
    if mode == "binding":
        child = RunnableBinding(
            bound=child, config={"configurable": {"thread_id": "same"}}
        )
    elif mode == "dynamic":
        child = RunnableBinding(bound=child, config_factories=[lambda config: config])
    elif mode not in {"saver", "store", "disabled"}:
        child = child.with_config({"configurable": {mode: None}})
    with pytest.raises(ValueError, match="subagent"):
        TinkerFin().with_namespace("scope").build(
            model=_Model(responses=[AIMessage(content="unused")]),
            subagents=[{"name": "child", "description": "Child", "runnable": child}],
        )


async def test_public_checkpoint_deletion_is_scoped_without_building_a_model() -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    from tinkerfin.checkpoints import delete_thread
    from tinkerfin_contracts import ThreadIdentity

    saver = InMemorySaver()
    for namespace in ("alpha", "beta"):
        await NamespaceCheckpointer(saver, namespace).aput(
            {"configurable": {"thread_id": "shared", "checkpoint_ns": ""}},
            empty_checkpoint(),
            {"source": "input"},
            {},
        )
    await delete_thread(
        saver, thread=ThreadIdentity(namespace="alpha", thread_id="shared")
    )
    await delete_thread(
        saver, thread=ThreadIdentity(namespace="alpha", thread_id="shared")
    )
    config: RunnableConfig = {"configurable": {"thread_id": "shared"}}
    assert await NamespaceCheckpointer(saver, "alpha").aget_tuple(config) is None
    assert await NamespaceCheckpointer(saver, "beta").aget_tuple(config) is not None


async def test_native_resume_without_a_new_checkpoint_cannot_stage_an_agui_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.types import Command, Interrupt, interrupt

    from tinkerfin import AgUiResumeRequest
    from tinkerfin._agui_lineage_state import RESUME_WRITE_OWNER

    entered: list[str] = []
    completed: list[object] = []

    def review(name: str) -> dict[str, object]:
        return {
            "action_requests": [{"name": name, "args": {}}],
            "review_configs": [{"action_name": name, "allowed_decisions": ["approve"]}],
        }

    from tinkerfin._agent_spec import AgentSpec

    def build(spec: AgentSpec[None], **kwargs: object) -> object:
        saver = spec.checkpointer
        assert isinstance(saver, BaseCheckpointSaver)
        builder = StateGraph(MessagesState)

        async def ask(state: MessagesState) -> dict[str, object]:
            del state
            entered.append("entered")
            interrupt(review("first"))
            completed.append(interrupt(review("second")))
            return {}

        builder.add_node("ask", ask)
        builder.add_edge(START, "ask")
        builder.add_edge("ask", END)
        return builder.compile(checkpointer=saver)

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", build)

    raw = InMemorySaver()
    view = NamespaceCheckpointer(raw, "scope")
    runtime = (
        TinkerFin(checkpointer=raw)
        .with_namespace("scope")
        .build(model="provider:model")
    )
    first = runtime.open_run(
        thread_id="thread",
        run_id="first",
        input={
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": name, "args": {}, "id": name}
                        for name in ("first", "second")
                    ],
                ),
            ]
        },
        durability="sync",
    )
    parts = [part async for part in first]
    pending = next(
        part["interrupts"] for part in reversed(parts) if part.get("interrupts")
    )
    assert isinstance(pending, (tuple, list))
    assert isinstance(pending[0], Interrupt)
    interrupt_id = pending[0].id
    second = runtime.open_run(
        thread_id="thread",
        run_id="native-resume",
        input=Command(resume={interrupt_id: {"decisions": [{"type": "approve"}]}}),
        durability="sync",
    )
    [part async for part in second]
    before = len(entered)
    request = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ]
        }
    )
    for _attempt in range(2):
        stream = runtime.open_agui_run(
            thread_id="thread", run_id="agui-resume", resume=request
        )
        events = [event async for event in stream]
        assert events[-1].type.value == "RUN_ERROR"
        assert stream.error is not None
        saved = await view.aget_tuple({"configurable": {"thread_id": "thread"}})
        assert saved is not None
        assert not any(
            owner == RESUME_WRITE_OWNER for owner, _, _ in saved.pending_writes or ()
        )
    assert len(entered) == before
    assert completed == []
