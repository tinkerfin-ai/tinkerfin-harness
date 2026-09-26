from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Annotated, TypedDict

import pytest
from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import (
    BaseEvent,
    RawEvent,
    StateSnapshotEvent,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from tinkerfin_agui_adapter import (
    AgUiStreamContractError,
    AttachmentMessagesSnapshotEvent,
    AttachmentToolCallResultEvent,
    DeepAgentAgUiAdapter,
    RunIdentity,
)
from tinkerfin_agui_adapter.ids import ScopedIdCodec


class _State(TypedDict):
    value: int


class _InterruptChildState(_State, total=False):
    messages: Annotated[list[AnyMessage], add_messages]


def _identity(*, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)


def _increment(state: _State) -> dict[str, int]:
    return {"value": state["value"] + 1}


def _ordinary_parent_graph() -> CompiledStateGraph:
    child_builder = StateGraph(_State)
    child_builder.add_node("child_node", _increment)
    child_builder.add_edge(START, "child_node")
    child_builder.add_edge("child_node", END)
    child = child_builder.compile()

    parent_builder = StateGraph(_State)
    parent_builder.add_node("execute_step", child)
    parent_builder.add_edge(START, "execute_step")
    parent_builder.add_edge("execute_step", END)
    return parent_builder.compile()


def _nested_parent_graph() -> CompiledStateGraph:
    leaf_builder = StateGraph(_State)
    leaf_builder.add_node("leaf", _increment)
    leaf_builder.add_edge(START, "leaf")
    leaf_builder.add_edge("leaf", END)
    leaf = leaf_builder.compile()

    child_builder = StateGraph(_State)
    child_builder.add_node("middle", leaf)
    child_builder.add_edge(START, "middle")
    child_builder.add_edge("middle", END)
    child = child_builder.compile()

    parent_builder = StateGraph(_State)
    parent_builder.add_node("outer", child)
    parent_builder.add_edge(START, "outer")
    parent_builder.add_edge("outer", END)
    return parent_builder.compile()


def _interrupting_child_graph() -> CompiledStateGraph:
    def propose(state: _InterruptChildState) -> dict[str, object]:
        del state
        return {
            "messages": [
                AIMessage(
                    id="proposal-message",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "a.txt"},
                            "id": "native-call",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def review(state: _InterruptChildState) -> dict[str, object]:
        del state
        interrupt(
            {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"path": "a.txt"},
                        "description": "Approve write",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve"],
                    }
                ],
            }
        )
        return {}

    child_builder = StateGraph(_InterruptChildState)
    child_builder.add_node("propose", propose)
    child_builder.add_node("review", review)
    child_builder.add_edge(START, "propose")
    child_builder.add_edge("propose", "review")
    child_builder.add_edge("review", END)
    child = child_builder.compile()

    parent_builder = StateGraph(_State)
    parent_builder.add_node("execute_step", child)
    parent_builder.add_edge(START, "execute_step")
    parent_builder.add_edge("execute_step", END)
    return parent_builder.compile(checkpointer=InMemorySaver())


async def _parts(graph: CompiledStateGraph) -> AsyncIterator[object]:
    async for part in graph.astream(
        {"value": 1},
        stream_mode=("messages", "tasks", "values"),
        subgraphs=True,
        version="v2",
    ):
        yield part


@pytest.mark.asyncio
async def test_ordinary_compiled_subgraph_uses_graph_scope_not_subagent_identity() -> (
    None
):
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    events: list[BaseEvent] = []

    async for part in _parts(_ordinary_parent_graph()):
        events.extend(adapter.process(part))
    events.extend(adapter.finish())

    child_values = [
        event
        for event in events
        if isinstance(event, RawEvent)
        and event.source == "langgraph.values"
        and isinstance(event.raw_event, Mapping)
        and event.raw_event.get("ns")
    ]
    assert len(child_values) == 2
    for event in child_values:
        provenance = event.event["provenance"]
        assert provenance["kind"] == "compiled_subgraph"
        assert provenance["graphNamespace"][0].startswith("execute_step:")
        assert "agentType" not in provenance

    task_events = [
        event
        for event in events
        if isinstance(event, RawEvent)
        and event.source is not None
        and "tasks" in event.source
    ]
    assert task_events
    assert {event.source for event in task_events} == {"langgraph.tasks"}


@pytest.mark.asyncio
async def test_nested_compiled_subgraphs_keep_complete_task_provenance() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    events: list[BaseEvent] = []

    async for part in _parts(_nested_parent_graph()):
        events.extend(adapter.process(part))
    events.extend(adapter.finish())

    task_events = [
        event
        for event in events
        if isinstance(event, RawEvent) and event.source == "langgraph.tasks"
    ]
    nested = [
        event
        for event in task_events
        if len(event.event["provenance"]["graphNamespace"]) == 2
    ]
    phases: list[object] = []
    for event in nested:
        raw_event = event.raw_event
        assert isinstance(raw_event, Mapping)
        phases.append(raw_event["phase"])
        provenance = event.event["provenance"]
        assert provenance["kind"] == "compiled_subgraph"
        assert provenance["nodeName"] == "middle"
        assert provenance["graphNamespace"][0].startswith("outer:")
        assert provenance["graphNamespace"][1].startswith("middle:")
        assert "agentType" not in provenance
    assert phases == ["start", "result"]


@pytest.mark.asyncio
async def test_repeated_child_calls_share_the_proven_parent_task() -> None:
    """Sequential child invocations retain their complete physical scopes."""

    child_builder = StateGraph(_State)
    child_builder.add_node("increment", _increment)
    child_builder.add_edge(START, "increment")
    child_builder.add_edge("increment", END)
    child = child_builder.compile()

    async def twice(state: _State, config: RunnableConfig) -> _State:
        first = await child.ainvoke(state, config)
        first_value = first["value"]
        assert isinstance(first_value, int)
        second = await child.ainvoke({"value": first_value}, config)
        second_value = second["value"]
        assert isinstance(second_value, int)
        return {"value": second_value}

    parent_builder = StateGraph(_State)
    parent_builder.add_node("twice", twice)
    parent_builder.add_edge(START, "twice")
    parent_builder.add_edge("twice", END)
    graph = parent_builder.compile()
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    events: list[BaseEvent] = []
    namespaces: set[tuple[str, ...]] = set()
    async for part in _parts(graph):
        assert isinstance(part, dict)
        namespaces.add(part["ns"])
        events.extend(adapter.process(part))
    events.extend(adapter.finish())

    children = sorted(namespace for namespace in namespaces if namespace)
    assert len(children) == 2
    assert children[1] == (*children[0], "1")
    values = [
        event
        for event in events
        if isinstance(event, RawEvent) and event.source == "langgraph.values"
    ]
    actual_namespaces: set[tuple[str, ...]] = set()
    for event in values:
        raw = event.raw_event
        assert isinstance(raw, Mapping)
        namespace = raw.get("ns")
        assert isinstance(namespace, list)
        assert all(isinstance(part, str) for part in namespace)
        actual_namespaces.add(tuple(namespace))
    assert actual_namespaces == set(children)
    assert {event.event["provenance"]["graphTaskId"] for event in values} == {
        children[0][0].partition(":")[2]
    }
    assert all(
        event.event["provenance"]["parentGraphNamespace"] == [] for event in values
    )


def test_unregistered_compiled_subgraph_part_is_rejected() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    with pytest.raises(
        AgUiStreamContractError, match="before its native task-start correlation"
    ):
        adapter.process(
            {
                "type": "values",
                "ns": ("missing:task-id",),
                "data": {"value": 1},
                "interrupts": (),
            }
        )


def test_ordinary_task_start_replay_and_conflict_are_deterministic() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    start = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "task-id",
            "name": "ordinary_child",
            "input": {"value": 1},
            "triggers": ("branch:to:ordinary_child",),
        },
    }

    first = adapter.process(start)
    replay = adapter.process(start)

    assert [event.model_dump() for event in replay] == [
        event.model_dump() for event in first
    ]
    with pytest.raises(ValueError, match="conflicting task start"):
        adapter.process(
            {
                **start,
                "data": {
                    **start["data"],
                    "input": {"value": 2},
                },
            }
        )

    child = adapter.process(
        {
            "type": "values",
            "ns": ("ordinary_child:task-id",),
            "data": {"value": 1},
            "interrupts": (),
        }
    )[0]
    assert isinstance(child, RawEvent)
    assert child.event["provenance"]["kind"] == "compiled_subgraph"


@pytest.mark.asyncio
async def test_child_interrupt_waits_for_root_and_merges_scoped_messages() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    events: list[BaseEvent] = []
    graph = _interrupting_child_graph()
    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}

    async for part in graph.astream(
        {"value": 1},
        config,
        stream_mode=("messages", "tasks", "values"),
        subgraphs=True,
        version="v2",
    ):
        events.extend(adapter.process(part))
    events.extend(adapter.finish())

    snapshots = [
        event
        for event in events
        if isinstance(event, StateSnapshotEvent | AttachmentMessagesSnapshotEvent)
    ]
    assert [type(event) for event in snapshots[-2:]] == [
        StateSnapshotEvent,
        AttachmentMessagesSnapshotEvent,
    ]
    state_snapshot = snapshots[-2]
    assert isinstance(state_snapshot, StateSnapshotEvent)
    assert state_snapshot.snapshot == {"value": 1}
    message_snapshot = snapshots[-1]
    assert isinstance(message_snapshot, AttachmentMessagesSnapshotEvent)
    assert len(message_snapshot.messages) == 1
    proposal = message_snapshot.messages[0]
    assert isinstance(proposal, AgUiAssistantMessage)
    assert proposal.id.startswith("tf:message:")
    assert proposal.tool_calls is not None
    assert proposal.tool_calls[0].id.startswith("tf:tool:")
    assert adapter.main_outcome().type == "interrupt"
    assert len(adapter.main_outcome().interrupts) == 1
    assert (
        adapter.main_outcome().interrupts[0].tool_call_id == proposal.tool_calls[0].id
    )


def _ordinary_task_start(*, node: str, task_id: str) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": task_id,
            "name": node,
            "input": {"value": 1},
            "triggers": (f"branch:to:{node}",),
        },
    }


def _generic_interrupt_part(
    *,
    namespace: tuple[str, ...],
    interrupt_id: str,
    value: object,
) -> dict[str, object]:
    return {
        "type": "values",
        "ns": namespace,
        "data": {"messages": [], "child_state": True},
        "interrupts": ({"id": interrupt_id, "value": value},),
    }


def test_child_interrupt_requires_identical_root_propagation() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="child", task_id="task-1"))
    child = _generic_interrupt_part(
        namespace=("child:task-1",),
        interrupt_id="interrupt-1",
        value={"kind": "pause", "revision": 1},
    )
    adapter.process(child)

    with pytest.raises(
        ValueError,
        match="conflicting root propagation for child interrupt",
    ):
        adapter.process(
            {
                **child,
                "ns": (),
                "data": {"messages": [], "root_state": True},
                "interrupts": (
                    {
                        "id": "interrupt-1",
                        "value": {"kind": "pause", "revision": 2},
                    },
                ),
            }
        )

    with pytest.raises(ValueError, match="not propagated by root values"):
        adapter.finish()


def test_same_interrupt_id_in_unrelated_child_scopes_is_rejected() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="first", task_id="task-1"))
    adapter.process(_ordinary_task_start(node="second", task_id="task-2"))
    value = {"kind": "pause"}

    adapter.process(
        _generic_interrupt_part(
            namespace=("first:task-1",),
            interrupt_id="shared-interrupt",
            value=value,
        )
    )
    with pytest.raises(
        ValueError,
        match="unrelated child namespaces",
    ):
        adapter.process(
            _generic_interrupt_part(
                namespace=("second:task-2",),
                interrupt_id="shared-interrupt",
                value=value,
            )
        )


def test_stream_completion_rejects_a_missing_child_interrupt() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="child", task_id="task-1"))
    adapter.process(
        _generic_interrupt_part(
            namespace=("child:task-1",),
            interrupt_id="interrupt-1",
            value={"kind": "pause"},
        )
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"root_state": True},
            "interrupts": (),
        }
    )
    with pytest.raises(ValueError, match="not propagated by root values"):
        adapter.finish()


def test_exact_child_and_root_interrupt_replays_are_idempotent() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="child", task_id="task-1"))
    child = _generic_interrupt_part(
        namespace=("child:task-1",),
        interrupt_id="interrupt-1",
        value={"kind": "pause"},
    )
    root = {
        **child,
        "ns": (),
        "data": {"messages": [], "root_state": True},
    }

    adapter.process(child)
    adapter.process(child)
    first = adapter.process(root)
    replay = adapter.process(root)

    assert [type(event) for event in first[-2:]] == [
        StateSnapshotEvent,
        AttachmentMessagesSnapshotEvent,
    ]
    assert [type(event) for event in replay] == [
        StateSnapshotEvent,
        AttachmentMessagesSnapshotEvent,
    ]
    assert len(adapter.main_outcome().interrupts) == 1
    assert adapter.finish() == []


def test_child_interrupt_replay_rejects_a_changed_message_snapshot_atomically() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    namespace = ("child:task-1",)
    adapter.process(_ordinary_task_start(node="child", task_id="task-1"))

    def child_part(tool_call_id: str) -> dict[str, object]:
        part = _generic_interrupt_part(
            namespace=namespace,
            interrupt_id="interrupt-1",
            value={
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"path": "a.txt"},
                        "description": "Approve write",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve"],
                    }
                ],
            },
        )
        part["data"] = {
            "messages": [
                AIMessage(
                    id="proposal-message",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "a.txt"},
                            "id": tool_call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "child_state": True,
        }
        return part

    first = child_part("native-call-first")
    adapter.process(first)

    with pytest.raises(ValueError, match="conflicting child message snapshot"):
        adapter.process(child_part("native-call-replayed"))

    root_events = adapter.process(
        {
            **first,
            "ns": (),
            "data": {"messages": [], "root_state": True},
        }
    )
    snapshot = next(
        event
        for event in root_events
        if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    proposal = snapshot.messages[0]
    assert isinstance(proposal, AgUiAssistantMessage)
    assert proposal.tool_calls is not None
    expected_tool_id = ScopedIdCodec().encode(
        "tool",
        namespace,
        "native-call-first",
    )
    assert proposal.tool_calls[0].id == expected_tool_id
    assert adapter.main_outcome().interrupts[0].tool_call_id == expected_tool_id


def test_multiple_child_snapshots_merge_in_scope_registration_order() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="first", task_id="task-1"))
    adapter.process(_ordinary_task_start(node="second", task_id="task-2"))

    def child_part(
        namespace: tuple[str, ...],
        interrupt_id: str,
        label: str,
    ) -> dict[str, object]:
        proposal = AIMessage(
            id="shared-message-id",
            content=label,
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": f"{label}.txt"},
                    "id": "shared-call-id",
                    "type": "tool_call",
                }
            ],
        )
        return {
            "type": "values",
            "ns": namespace,
            "data": {"messages": [proposal], "child_state": label},
            "interrupts": (
                {
                    "id": interrupt_id,
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"path": f"{label}.txt"},
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve"],
                            }
                        ],
                    },
                },
            ),
        }

    first = child_part(("first:task-1",), "interrupt-1", "first")
    second = child_part(("second:task-2",), "interrupt-2", "second")
    first_interrupts = first["interrupts"]
    second_interrupts = second["interrupts"]
    assert isinstance(first_interrupts, tuple)
    assert isinstance(second_interrupts, tuple)
    adapter.process(first)
    adapter.process(second)
    root_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [HumanMessage(id="root-message", content="root")],
                "root_state": True,
            },
            "interrupts": (
                second_interrupts[0],
                first_interrupts[0],
            ),
        }
    )

    state = next(
        event for event in root_events if isinstance(event, StateSnapshotEvent)
    )
    assert state.snapshot == {"root_state": True}
    snapshot = next(
        event
        for event in root_events
        if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    assert [message.content for message in snapshot.messages] == [
        "root",
        "first",
        "second",
    ]
    assert len({message.id for message in snapshot.messages}) == 3
    assert [interrupt.id for interrupt in adapter.main_outcome().interrupts] == [
        "interrupt-2",
        "interrupt-1",
    ]


def test_scoped_prior_tool_result_does_not_replay_proposal_lifecycle() -> None:
    namespace = ("child:task-1",)
    scoped_tool_id = ScopedIdCodec().encode("tool", namespace, "native-call")
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(run_id="run-resumed"),
        prior_tool_call_ids=frozenset({scoped_tool_id}),
    )
    adapter.process(_ordinary_task_start(node="child", task_id="task-1"))

    events = adapter.process(
        {
            "type": "messages",
            "ns": namespace,
            "data": (
                ToolMessage(
                    id="result-message",
                    name="write_file",
                    tool_call_id="native-call",
                    content="approved",
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )

    assert [type(event) for event in events] == [AttachmentToolCallResultEvent]
    result = events[0]
    assert isinstance(result, AttachmentToolCallResultEvent)
    assert result.tool_call_id == scoped_tool_id


def test_nested_child_interrupt_propagates_through_ancestor_and_root_once() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="outer", task_id="outer-task"))
    outer_namespace = ("outer:outer-task",)
    adapter.process(
        {
            "type": "tasks",
            "ns": outer_namespace,
            "data": {
                "id": "inner-task",
                "name": "inner",
                "input": {"value": 1},
                "triggers": ("branch:to:inner",),
            },
        }
    )
    inner_namespace = (*outer_namespace, "inner:inner-task")
    value = {"kind": "nested-pause"}

    adapter.process(
        _generic_interrupt_part(
            namespace=inner_namespace,
            interrupt_id="nested-interrupt",
            value=value,
        )
    )
    adapter.process(
        _generic_interrupt_part(
            namespace=outer_namespace,
            interrupt_id="nested-interrupt",
            value=value,
        )
    )
    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [], "root_state": True},
            "interrupts": ({"id": "nested-interrupt", "value": value},),
        }
    )

    outcome = adapter.main_outcome()
    assert len(outcome.interrupts) == 1
    metadata = outcome.interrupts[0].metadata
    assert isinstance(metadata, Mapping)
    source = metadata.get("source")
    assert isinstance(source, Mapping)
    assert source["graphNamespace"] == list(inner_namespace)
    assert adapter.finish() == []


@pytest.mark.parametrize("shared_scope", [False, True])
def test_parallel_interrupt_batches_accumulate_until_stream_completion(
    shared_scope: bool,
) -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_ordinary_task_start(node="first", task_id="task-1"))
    if not shared_scope:
        adapter.process(_ordinary_task_start(node="second", task_id="task-2"))
    first = _generic_interrupt_part(
        namespace=("first:task-1",), interrupt_id="first", value={"kind": "first"}
    )
    second = _generic_interrupt_part(
        namespace=("first:task-1",) if shared_scope else ("second:task-2",),
        interrupt_id="second",
        value={"kind": "second"},
    )
    adapter.process(first)
    adapter.process(second)
    adapter.process({**first, "ns": (), "data": {"messages": []}})
    adapter.process({**second, "ns": (), "data": {"messages": []}})
    adapter.finish()
    assert [pending.id for pending in adapter.main_outcome().interrupts] == [
        "first",
        "second",
    ]
