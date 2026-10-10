"""Current native stream validation contracts."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt

from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeStreamContractError,
    NativeValuesStreamPart,
    validate_native_stream_part,
)


def test_message_and_values_parts_preserve_live_objects_and_namespaces() -> None:
    message = AIMessageChunk(id="message-1", content="visible")
    validated_message = validate_native_stream_part(
        {
            "type": "messages",
            "ns": ("tools:task-1",),
            "data": (message, {"langgraph_node": "model"}),
        }
    )
    validated_values = validate_native_stream_part(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [message], "todos": []},
            "interrupts": (Interrupt(value={"kind": "review"}, id="interrupt-1"),),
        }
    )

    assert isinstance(validated_message, NativeMessageStreamPart)
    assert validated_message.ns == ("tools:task-1",)
    assert validated_message.data.message is message
    assert isinstance(validated_values, NativeValuesStreamPart)
    assert validated_values.interrupts[0].id == "interrupt-1"

    validated_message.ns = ("tools:updated",)
    message.content = "changed upstream"
    assert validated_message.ns == ("tools:updated",)
    assert validated_message.data.message.content == "changed upstream"


@pytest.mark.parametrize(
    "part",
    (
        {"type": "messages", "ns": (), "data": (AIMessageChunk(content="x"),)},
        {"type": "values", "ns": [], "data": {}},
        {"type": "tasks", "ns": (), "data": {"id": "task-1"}},
        {"type": "unknown", "ns": (), "data": {}},
    ),
)
def test_malformed_parts_fail_with_safe_context(part: object) -> None:
    with pytest.raises(NativeStreamContractError) as captured:
        validate_native_stream_part(part)

    assert captured.value.code.value == "native.stream_contract"
    assert "input" not in captured.value.diagnostic_context
