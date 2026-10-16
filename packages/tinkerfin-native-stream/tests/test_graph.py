"""Physical graph origins require task evidence and preserve owned privacy."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from tinkerfin_contracts import GraphOrigin, GraphTaskReference
from tinkerfin_native_stream import (
    NativeGraphScopeRegistry,
    NativeStreamContractError,
    NativeStreamMetadata,
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
