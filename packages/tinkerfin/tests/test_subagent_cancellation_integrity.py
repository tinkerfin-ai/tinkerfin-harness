"""Locked subagent failure, cancellation, and Adapter integrity contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, cast

import pytest
from ag_ui.core import (
    BaseEvent,
    RawEvent,
    RunErrorEvent,
    RunFinishedEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, MessagesState, StateGraph

from tinkerfin import AgUiRunStream, RunIdentity, TinkerFin


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    """Keep deterministic responses while accepting the runtime Tool binding."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _ToolBindingFakeModel:
        del tools, kwargs
        return self


class _AsyncStateNode(Protocol):
    def __call__(
        self,
        state: MessagesState,
    ) -> Awaitable[dict[str, object]]: ...


def _compiled_child(
    name: str,
    node: _AsyncStateNode,
) -> Any:
    """Compile one test subagent at the real LangGraph Runnable boundary."""

    builder = StateGraph(MessagesState)
    builder.add_node(name, node)
    builder.add_edge(START, name)
    builder.add_edge(name, END)
    return builder.compile(name=name)


def _root_model(tool_calls: list[dict[str, object]]) -> _ToolBindingFakeModel:
    return _ToolBindingFakeModel(
        responses=[
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content="root done"),
        ]
    )


def _task_call(
    *,
    call_id: str = "parent-task-call",
    subagent_type: str = "researcher",
    extra_args: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": "task",
        "args": {
            "description": "Complete the delegated work",
            "subagent_type": subagent_type,
            **dict(extra_args or {}),
        },
        "id": call_id,
        "type": "tool_call",
    }


async def _run_default_agui(
    *,
    thread_id: str,
    model: _ToolBindingFakeModel,
    subagents: list[dict[str, object]],
) -> tuple[
    list[BaseEvent],
    AgUiRunStream,
    list[Mapping[str, object]],
    list[str],
]:
    """Capture every native repr before the public Adapter consumes its part."""

    native_parts: list[Mapping[str, object]] = []
    native_reprs: list[str] = []

    async def observe(part: Mapping[str, object]) -> None:
        native_reprs.append(repr(part))
        native_parts.append(part)

    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[],
            subagents=cast(Any, subagents),
        )
    )
    identity = RunIdentity(
        namespace="test", thread_id=thread_id, run_id=f"run-{thread_id}"
    )
    runtime = definition
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        mode="default",
        on_native_part=observe,
        input={"messages": [HumanMessage(content="Delegate the work")]},
        config={"configurable": {"thread_id": thread_id}},
    )
    events = [event async for event in stream]
    return events, stream, native_parts, native_reprs


def _task_start_inputs(
    parts: Sequence[Mapping[str, object]],
) -> list[object]:
    return [
        data["input"]
        for part in parts
        if part.get("type") == "tasks"
        and isinstance((data := part.get("data")), Mapping)
        and data.get("name") == "tools"
        and "input" in data
    ]


def _task_error_types(events: Sequence[BaseEvent]) -> list[str]:
    error_types: list[str] = []
    for event in events:
        if not isinstance(event, RawEvent) or event.source != "langgraph.tasks":
            continue
        if not isinstance(event.raw_event, Mapping):
            continue
        if event.raw_event.get("phase") != "result":
            continue
        data = event.event.get("data")
        if not isinstance(data, Mapping):
            continue
        error = data.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("type"), str):
            error_types.append(cast(str, error["type"]))
    return error_types


@pytest.mark.asyncio
async def test_extra_task_arguments_do_not_cancel_a_valid_subagent_run() -> None:
    """Native pre-validation Tool args must not invalidate effective provenance."""

    async def complete(state: MessagesState) -> dict[str, object]:
        del state
        return {"messages": [AIMessage(content="child done")]}

    events, stream, native_parts, native_reprs = await _run_default_agui(
        thread_id="task-extra-arguments",
        model=_root_model(
            [
                _task_call(
                    extra_args={
                        "prompt": "Duplicate model-generated instructions",
                    }
                )
            ]
        ),
        subagents=[
            {
                "name": "researcher",
                "description": "Research deterministically",
                "runnable": _compiled_child("complete_child", complete),
            }
        ],
    )

    assert len(native_reprs) == len(native_parts)
    task_inputs = _task_start_inputs(native_parts)
    assert len(task_inputs) == 1
    assert "'prompt': 'Duplicate model-generated instructions'" in repr(task_inputs[0])
    assert stream.error is None
    assert [
        event.type.value
        for event in events
        if isinstance(event, RunFinishedEvent | RunErrorEvent)
    ] == ["RUN_FINISHED"]
    terminal = next(event for event in events if isinstance(event, RunFinishedEvent))
    assert terminal.outcome is not None
    assert terminal.outcome.type == "success"

    task_start = next(
        event
        for event in events
        if isinstance(event, ToolCallStartEvent) and event.tool_call_name == "task"
    )
    assert (
        sum(
            isinstance(event, ToolCallEndEvent)
            and event.tool_call_id == task_start.tool_call_id
            for event in events
        )
        == 1
    )
    assert (
        sum(
            isinstance(event, ToolCallResultEvent)
            and event.tool_call_id == task_start.tool_call_id
            for event in events
        )
        == 1
    )
    descriptors: list[object] = []
    for event in events:
        if not isinstance(event, RawEvent) or event.source != "langgraph.tasks":
            continue
        provenance = event.event.get("provenance")
        if not isinstance(provenance, Mapping):
            continue
        raw_descriptors = provenance.get("subagents")
        if isinstance(raw_descriptors, list):
            descriptors.extend(raw_descriptors)
    assert len(descriptors) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("ordinary", RuntimeError),
        ("node_cancel", NodeCancelledError),
    ],
)
async def test_subagent_failures_keep_their_distinct_runtime_cause(
    failure_kind: str,
    expected_error: type[Exception],
) -> None:
    """A node cancellation is a failure, while ordinary exceptions stay intact."""

    async def fail(state: MessagesState) -> dict[str, object]:
        del state
        if failure_kind == "node_cancel":
            raise asyncio.CancelledError("child cancelled itself")
        raise RuntimeError("child failed")

    events, stream, native_parts, native_reprs = await _run_default_agui(
        thread_id=f"subagent-{failure_kind}",
        model=_root_model([_task_call()]),
        subagents=[
            {
                "name": "researcher",
                "description": "Fail deterministically",
                "runnable": _compiled_child("fail_child", fail),
            }
        ],
    )

    assert len(native_reprs) == len(native_parts)
    assert isinstance(stream.error, expected_error)
    if isinstance(stream.error, NodeCancelledError):
        assert isinstance(stream.error.__cause__, asyncio.CancelledError)
    terminals = [
        event for event in events if isinstance(event, RunFinishedEvent | RunErrorEvent)
    ]
    assert len(terminals) == 1
    assert isinstance(terminals[0], RunErrorEvent)
    assert terminals[0].code == "runtime_error"
    assert expected_error.__name__ in _task_error_types(events)
    task_start = next(
        event
        for event in events
        if isinstance(event, ToolCallStartEvent) and event.tool_call_name == "task"
    )
    assert (
        sum(
            isinstance(event, ToolCallEndEvent)
            and event.tool_call_id == task_start.tool_call_id
            for event in events
        )
        == 1
    )
    assert not any(
        isinstance(event, ToolCallResultEvent)
        and event.tool_call_id == task_start.tool_call_id
        for event in events
    )


@pytest.mark.asyncio
async def test_external_abort_stays_cancelled_and_cleans_the_subagent() -> None:
    """Runtime cancellation must not be converted into an ordinary run failure."""

    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def block(state: MessagesState) -> dict[str, object]:
        del state
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return {"messages": [AIMessage(content="unreachable")]}

    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=_root_model([_task_call()]),
            tools=[],
            subagents=cast(
                Any,
                [
                    {
                        "name": "researcher",
                        "description": "Block until cancelled",
                        "runnable": _compiled_child("block_child", block),
                    }
                ],
            ),
        )
    )
    identity = RunIdentity(
        namespace="test", thread_id="external-abort", run_id="run-external-abort"
    )
    runtime = definition
    stream = runtime.open_agui_run(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        mode="default",
        input={"messages": [HumanMessage(content="Delegate the work")]},
        config={"configurable": {"thread_id": "external-abort"}},
    )
    delivered: list[BaseEvent] = []

    async def consume() -> None:
        async for event in stream:
            delivered.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=5)
    tail = await stream.abort()
    outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert cleaned.is_set()
    assert stream.error is None
    assert not any(isinstance(event, RunErrorEvent) for event in delivered)
    assert len(tail) == 1
    assert isinstance(tail[0], RunErrorEvent)
    assert tail[0].code == "cancelled"


@pytest.mark.asyncio
async def test_parallel_sibling_cancellation_does_not_replace_the_first_failure() -> (
    None
):
    """Pregel sibling teardown must preserve the ordinary failure that caused it."""

    blocker_started = asyncio.Event()
    blocker_cleaned = asyncio.Event()

    async def block(state: MessagesState) -> dict[str, object]:
        del state
        blocker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            blocker_cleaned.set()
        return {"messages": [AIMessage(content="unreachable")]}

    async def fail(state: MessagesState) -> dict[str, object]:
        del state
        await blocker_started.wait()
        raise RuntimeError("first sibling failure")

    graph = create_deep_agent(
        model=_root_model(
            [
                _task_call(call_id="task-fail", subagent_type="failing"),
                _task_call(call_id="task-block", subagent_type="blocking"),
            ]
        ),
        tools=[],
        subagents=cast(
            Any,
            [
                {
                    "name": "failing",
                    "description": "Fail after the sibling starts",
                    "runnable": _compiled_child("fail_child", fail),
                },
                {
                    "name": "blocking",
                    "description": "Block until sibling teardown",
                    "runnable": _compiled_child("block_child", block),
                },
            ],
        ),
        checkpointer=InMemorySaver(),
    )
    native_parts: list[Mapping[str, object]] = []
    native_reprs: list[str] = []

    with pytest.raises(RuntimeError, match="first sibling failure"):
        async for part in graph.astream(
            {"messages": [HumanMessage(content="Delegate both tasks")]},
            config={"configurable": {"thread_id": "parallel-siblings"}},
            stream_mode=["messages", "tasks", "values"],
            version="v2",
            subgraphs=True,
        ):
            native_reprs.append(repr(part))
            native_parts.append(cast(Mapping[str, object], part))

    assert len(native_reprs) == len(native_parts)
    assert blocker_cleaned.is_set()
    errors = [
        data.get("error")
        for part in native_parts
        if part.get("type") == "tasks"
        and isinstance((data := part.get("data")), Mapping)
        and "result" in data
    ]
    assert any(
        isinstance(error, RuntimeError) and str(error) == "first sibling failure"
        for error in errors
    )
