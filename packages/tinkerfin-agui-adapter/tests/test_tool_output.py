"""Tool progress inherits the live cloud call's identity and lifecycle."""

from __future__ import annotations

import pytest
from ag_ui.core import CustomEvent, RawEvent
from langchain_core.messages import AIMessage, ToolMessage

from tinkerfin_agui_adapter import AgUiAdapterError, DeepAgentAgUiAdapter, RunIdentity
from tinkerfin_agui_adapter.ids import ScopedIdCodec


def adapter() -> DeepAgentAgUiAdapter:
    return DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run")
    )


def begin(target: DeepAgentAgUiAdapter, namespace: tuple[str, ...] = ()) -> None:
    call = {"id": "call", "name": "inspect", "args": {}, "type": "tool_call"}
    if namespace:
        target.process(
            {
                "type": "tasks",
                "ns": (),
                "data": {
                    "id": "child",
                    "name": "worker",
                    "input": {},
                    "triggers": ("start",),
                },
            }
        )
    target.process(
        {
            "type": "messages",
            "ns": namespace,
            "data": (
                AIMessage(id="proposal", content="", tool_calls=[call]),
                {"langgraph_node": "model"},
            ),
        }
    )
    target.process(
        {
            "type": "tasks",
            "ns": namespace,
            "data": {
                "id": "execute",
                "name": "tools",
                "input": [call],
                "triggers": ("tools",),
            },
        }
    )


def progress(
    namespace: tuple[str, ...] = (),
    *,
    identifier: str = "call",
    value: object = "partial",
) -> dict[str, object]:
    return {
        "type": "custom",
        "ns": namespace,
        "data": {"operation": "tool_output", "toolCallId": identifier, "value": value},
    }


@pytest.mark.parametrize("namespace", [(), ("worker:child",)])
def test_progress_uses_scoped_tool_identity_and_keeps_json_value(
    namespace: tuple[str, ...],
) -> None:
    target = adapter()
    begin(target, namespace)
    result = target.process(
        progress(namespace, value={"stdout": "partial", "count": 1})
    )
    assert len(result) == 1 and isinstance(result[0], CustomEvent)
    assert result[0].name == "tinkerfin.tool_output"
    assert result[0].value == {
        "toolCallId": ScopedIdCodec().encode("tool", namespace, "call"),
        "value": {"stdout": "partial", "count": 1},
    }
    assert isinstance(result[0].raw_event, dict)
    assert result[0].raw_event["runId"] == "run"
    assert result[0].raw_event["source"]["graphNamespace"] == list(namespace)
    assert all(
        event.type.value not in {"RUN_STARTED", "RUN_FINISHED", "TOOL_CALL_RESULT"}
        for event in result
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown",
        "wrong_scope",
        "before_execution",
        "after_result",
        "after_task_end",
        "nonfinite",
        "injected_event",
    ],
)
def test_progress_rejects_unowned_or_finished_calls_without_mutating_valid_stream(
    invalid: str,
) -> None:
    target = adapter()
    if invalid != "before_execution":
        begin(target)
    part = progress()
    if invalid == "unknown":
        part = progress(identifier="foreign")
    elif invalid == "wrong_scope":
        target.process(
            {
                "type": "tasks",
                "ns": (),
                "data": {
                    "id": "child",
                    "name": "worker",
                    "input": {},
                    "triggers": ("start",),
                },
            }
        )
        part = progress(("worker:child",))
    elif invalid == "after_result":
        target.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    ToolMessage(content="done", tool_call_id="call", name="inspect"),
                    {"langgraph_node": "tools"},
                ),
            }
        )
    elif invalid == "after_task_end":
        target.process(
            {
                "type": "tasks",
                "ns": (),
                "data": {
                    "id": "execute",
                    "name": "tools",
                    "result": {},
                    "error": None,
                    "interrupts": [],
                },
            }
        )
    elif invalid == "nonfinite":
        part = progress(value=float("nan"))
    elif invalid == "injected_event":
        part = {
            "type": "custom",
            "ns": (),
            "data": {
                "operation": "tool_output",
                "toolCallId": "call",
                "value": "partial",
                "event": {"type": "RUN_FINISHED"},
            },
        }
    with pytest.raises(AgUiAdapterError):
        target.process(part)
    if invalid in {"unknown", "wrong_scope", "nonfinite", "injected_event"}:
        assert isinstance(target.process(progress())[0], CustomEvent)


def test_unrelated_custom_data_remains_raw_without_protocol_event_injection() -> None:
    target = adapter()
    result = target.process(
        {
            "type": "custom",
            "ns": (),
            "data": {"event": {"type": "RUN_FINISHED", "runId": "untrusted"}},
        }
    )
    assert len(result) == 1 and isinstance(result[0], RawEvent)
