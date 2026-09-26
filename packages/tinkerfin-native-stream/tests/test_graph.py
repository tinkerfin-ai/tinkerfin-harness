"""Physical graph origins require task evidence and preserve owned privacy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.func import task
from langgraph.graph import END, START, StateGraph

from tinkerfin_contracts import GraphOrigin, GraphTaskReference
from tinkerfin_native_stream import (
    NativeExtraStreamPart,
    NativeGraphScopeRegistry,
    NativeStreamContractError,
    NativeStreamMetadata,
    NativeStreamPart,
    NativeTasksStreamPart,
    NativeTaskStartPayload,
    validate_native_stream_part,
)


def _start(
    task_id: str,
    *,
    node: str = "child",
    namespace: tuple[str, ...] = (),
    metadata: dict[str, object] | None = None,
) -> NativeTasksStreamPart:
    return NativeTasksStreamPart(
        type="tasks",
        ns=namespace,
        data=NativeTaskStartPayload(
            id=task_id,
            name=node,
            input={},
            triggers=("__pregel_push",),
            metadata=None
            if metadata is None
            else NativeStreamMetadata.model_validate(metadata),
        ),
    )


def test_repeat_counter_preserves_the_exact_opening_task() -> None:
    registry = NativeGraphScopeRegistry()
    registry.accept(_start("parent"))
    base = registry.resolve(("child:parent",))
    assert base.parent_task == GraphTaskReference(
        graph_namespace=(), task_id="parent", node_name="child"
    )
    assert registry.resolve(("child:parent", "1")) == base
    assert registry.resolve(("child:parent", "25")) == base
    assert base.subagent_request is None
    for namespace in (
        ("unknown:parent", "1"),
        ("child:parent", "0"),
        ("child:parent", "01"),
        ("child:parent", "１"),
        ("child:parent", "1", "2"),
    ):
        with pytest.raises(NativeStreamContractError):
            registry.resolve(namespace)


def test_functional_task_metadata_cannot_claim_an_unrelated_parent() -> None:
    registry = NativeGraphScopeRegistry()
    registry.accept(_start("left"))
    registry.accept(_start("right"))
    with pytest.raises(ValueError, match="proven caller path"):
        registry.accept(
            _start(
                "operation",
                node="work",
                metadata={"langgraph_checkpoint_ns": "child:right|work:operation"},
            )
        )
    with pytest.raises(ValueError, match="conflicts with its identity"):
        registry.accept(
            _start(
                "operation",
                node="work",
                metadata={"langgraph_checkpoint_ns": "child:right|work:another"},
            )
        )


class _State(TypedDict):
    value: int


async def test_raw_functional_tasks_without_scope_evidence_fail_closed() -> None:
    def increment(state: _State) -> _State:
        return {"value": state["value"] + 1}

    child_builder = StateGraph(_State)
    child_builder.add_node("increment", increment)
    child_builder.add_edge(START, "increment")
    child_builder.add_edge("increment", END)
    child = child_builder.compile()

    @task(name="operation")
    async def operation(state: _State) -> _State:
        output = await child.ainvoke(state)
        value = output["value"]
        assert isinstance(value, int)
        return {"value": value}

    async def parent(state: _State) -> _State:
        return await operation(await operation(state))

    builder = StateGraph(_State)
    builder.add_node("parent", parent)
    builder.add_edge(START, "parent")
    builder.add_edge("parent", END)
    parts = [
        validate_native_stream_part(raw)
        async for raw in builder.compile().astream(
            {"value": 1},
            version="v2",
            stream_mode=["tasks", "values"],
            subgraphs=True,
        )
    ]
    openings = [
        part
        for part in parts
        if isinstance(part, NativeTasksStreamPart)
        and isinstance(part.data, NativeTaskStartPayload)
        and part.data.name == "operation"
    ]
    assert len(openings) == 2
    assert all(
        part.ns == ()
        and isinstance(part.data, NativeTaskStartPayload)
        and part.data.metadata is None
        for part in openings
    )
    registry = NativeGraphScopeRegistry()
    with pytest.raises(NativeStreamContractError, match="before its native task-start"):
        for part in parts:
            registry.accept(part)


def test_private_attempt_filter_requires_exact_owned_task_and_return() -> None:
    registry = NativeGraphScopeRegistry()
    request = registry.register_delegation(
        parent_namespace=(),
        graph_task_id="parent",
        tool_call_id="delegate",
        agent_name="worker",
        description="Work",
    )
    registry.register_retry(request, node_name="delegation_attempt")
    scope = ("tools:parent", "delegation_attempt:attempt")
    registry.register_execution(
        scope,
        parent_task=GraphTaskReference(
            graph_namespace=(),
            task_id="attempt",
            node_name="delegation_attempt",
        ),
        request=request,
    )
    reference = {"attempt_key": "private-key", "record_digest": "private-digest"}
    registry.record_internal_result((), "attempt", reference)
    assert registry.public_part(_start("attempt", node="delegation_attempt")) is None

    updates = validate_native_stream_part(
        {
            "type": "updates",
            "ns": (),
            "data": {"delegation_attempt": reference, "business": reference},
        }
    )
    public = registry.public_part(updates)
    assert public is not None and public.data == {"business": reference}
    business = validate_native_stream_part(
        {
            "type": "updates",
            "ns": (),
            "data": {
                "delegation_attempt": {
                    "attempt_key": "business",
                    "record_digest": "data",
                }
            },
        }
    )
    assert registry.public_part(business) is business
    state = validate_native_stream_part({"type": "values", "ns": (), "data": reference})
    assert registry.public_part(state) is state
    for mode in ("debug", "checkpoints"):
        checkpoint = {
            "tasks": [
                {"id": "attempt", "result": reference},
                {"id": "public", "result": reference},
            ],
            "values": {"business": reference},
        }
        part = validate_native_stream_part(
            {
                "type": mode,
                "ns": (),
                "data": {"type": "checkpoint", "payload": checkpoint}
                if mode == "debug"
                else checkpoint,
            }
        )
        filtered = registry.public_part(part)
        assert isinstance(filtered, NativeExtraStreamPart)
        assert isinstance(filtered.data, Mapping)
        data = filtered.data["payload"] if mode == "debug" else filtered.data
        assert data == {
            "tasks": [{"id": "public", "result": reference}],
            "values": {"business": reference},
        }


def test_tool_proposal_identity_is_scoped_and_replayable() -> None:
    registry = NativeGraphScopeRegistry()
    original = AIMessage(
        id="original",
        content="",
        tool_calls=[
            {
                "id": "call",
                "name": "search",
                "args": {},
            }
        ],
    )
    baseline = validate_native_stream_part(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [original]},
        }
    )
    registry.accept(baseline)
    registry.accept(baseline)
    registry.validate_model_output(
        (), message_ids=("original",), tool_call_ids=("call",)
    )
    with pytest.raises(NativeStreamContractError, match="unique IDs"):
        registry.validate_model_output(
            (), message_ids=("new",), tool_call_ids=("call",)
        )
    conflicting = validate_native_stream_part(
        {
            "type": "messages",
            "ns": (),
            "data": (original.model_copy(update={"id": "new"}), {}),
        }
    )
    with pytest.raises(NativeStreamContractError, match="unique IDs"):
        registry.accept(conflicting)
    registry.accept(_start("parent"))
    registry.accept(conflicting.model_copy(update={"ns": ("child:parent",)}))


def test_recorded_origin_cannot_assign_an_unrelated_scope_to_a_delegate() -> None:
    registry = NativeGraphScopeRegistry()
    request = registry.register_delegation(
        parent_namespace=(),
        graph_task_id="parent",
        tool_call_id="delegate",
        agent_name="worker",
        description="Work",
    )
    registry.accept(_start("other"))
    with pytest.raises(ValueError, match="proven delegation owner"):
        registry.adopt_origin(
            ("child:other", "operation:attempt"),
            GraphOrigin(
                parent_task=GraphTaskReference(
                    graph_namespace=(), task_id="attempt", node_name="operation"
                ),
                subagent_request=request,
            ),
        )
    assert registry.resolve(("child:other",)).subagent_request is None


@pytest.mark.parametrize(
    "parent", [("unrelated:bad",), ("tools:parent", "operation:attempt")]
)
def test_recorded_attempt_requires_a_proven_proper_ancestor(
    parent: tuple[str, ...],
) -> None:
    registry = NativeGraphScopeRegistry()
    request = registry.register_delegation(
        parent_namespace=(),
        graph_task_id="parent",
        tool_call_id="call",
        agent_name="worker",
        description="Work",
    )
    namespace = ("tools:parent", "operation:attempt")
    with pytest.raises(ValueError, match="proven ancestor"):
        registry.adopt_origin(
            namespace,
            GraphOrigin(
                parent_task=GraphTaskReference(
                    graph_namespace=parent, task_id="attempt", node_name="operation"
                ),
                subagent_request=request,
            ),
        )
    with pytest.raises(NativeStreamContractError):
        registry.resolve(namespace)
    assert registry.resolve(("tools:parent",)).subagent_request == request


def test_first_recorded_execution_confirms_effective_arguments_once() -> None:
    registry = NativeGraphScopeRegistry()
    request = registry.register_delegation(
        parent_namespace=(),
        graph_task_id="parent",
        tool_call_id="call",
        agent_name="worker",
        description="Proposed work",
    )
    namespace = ("tools:parent",)
    effective = request.model_copy(update={"description": "Approved effective work"})
    origin = GraphOrigin(
        parent_task=GraphTaskReference(
            graph_namespace=(), task_id="parent", node_name="tools"
        ),
        subagent_request=effective,
    )
    registry.adopt_origin(namespace, origin)
    registry.adopt_origin(namespace, origin)
    changed = origin.model_copy(
        update={
            "subagent_request": effective.model_copy(
                update={"description": "Unproved later work"}
            )
        }
    )
    with pytest.raises(ValueError, match="after its execution was observed"):
        registry.adopt_origin(namespace, changed)
    assert registry.resolve(namespace).subagent_request == effective


@pytest.mark.parametrize(
    "data",
    [
        {
            "state": {"messages": []},
            "messages": [],
            "messagesPresent": False,
            "interrupts": [],
        },
        {
            "state": {},
            "messages": [],
            "messagesPresent": False,
            "interrupts": [{"id": "unexpected", "value": {}}],
        },
        {
            "state": {},
            "messages": [],
            "messagesPresent": False,
            "interrupts": [],
            "unknown": 1,
        },
    ],
)
def test_recorded_state_rejects_conflicting_or_unknown_fields(
    data: dict[str, object],
) -> None:
    part = NativeStreamPart.model_validate({"type": "values", "ns": [], "data": data})
    with pytest.raises(NativeStreamContractError):
        part.to_frame()
