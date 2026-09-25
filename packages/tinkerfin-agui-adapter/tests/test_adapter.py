"""Contract tests for native LangGraph `tasks` to AG-UI sub-run correlation."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest
from ag_ui.core import (
    AssistantMessage,
    BaseEvent,
    RawEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ag_ui.core import (
    ToolMessage as AgUiToolMessage,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ToolCallChunk,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    TOOL_REVIEW_SCHEMA,
    AgUiAdapterErrorCode,
    AgUiStreamContractError,
    AttachmentMessagesSnapshotEvent,
    AttachmentToolCallResultEvent,
    HitlCorrelationError,
    RunIdentity,
    create_subagent_provenance,
    parse_tool_review_interrupt,
)
from tinkerfin_agui_adapter.adapter import DeepAgentAgUiAdapter
from tinkerfin_agui_adapter.ids import ScopedIdCodec
from tinkerfin_native_stream import NativeStreamContractError

_IDS = ScopedIdCodec()


def _identity(*, run_id: str = "run-main") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)


def _message_id(namespace: tuple[str, ...], raw_id: str) -> str:
    return _IDS.encode("message", namespace, raw_id)


def _tool_id(namespace: tuple[str, ...], raw_id: str) -> str:
    return _IDS.encode("tool", namespace, raw_id)


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """Provide deterministic Tool calls for Deep Agents integration tests."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


def _adapter() -> DeepAgentAgUiAdapter:
    return DeepAgentAgUiAdapter(identity=_identity())


def _raw_event(event: BaseEvent) -> dict[str, Any]:
    raw_event = event.raw_event
    assert isinstance(raw_event, dict)
    return raw_event


def _task_start(
    *,
    namespace: tuple[str, ...] = (),
    graph_task_id: str,
    tool_call_id: str,
    description: str,
    subagent_type: str = "researcher",
) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": namespace,
        "data": {
            "id": graph_task_id,
            "name": "tools",
            "input": [
                {
                    "name": "task",
                    "args": {
                        "description": description,
                        "subagent_type": subagent_type,
                    },
                    "id": tool_call_id,
                    "type": "tool_call",
                }
            ],
            "triggers": ("__pregel_push",),
            "metadata": {
                "ls_integration": "deepagents",
                "lc_agent_name": None if not namespace else "researcher",
            },
        },
    }


def _task_group_start(
    *,
    graph_task_id: str,
    calls: tuple[tuple[str, str, str], ...],
) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": graph_task_id,
            "name": "tools",
            "input": [
                {
                    "name": "task",
                    "args": {
                        "description": description,
                        "subagent_type": agent_name,
                    },
                    "id": tool_call_id,
                    "type": "tool_call",
                }
                for tool_call_id, description, agent_name in calls
            ],
            "triggers": ("__pregel_push",),
            "metadata": {"ls_integration": "deepagents"},
        },
    }


def _task_result(
    *,
    namespace: tuple[str, ...] = (),
    graph_task_id: str,
    error: object = None,
) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": namespace,
        "data": {
            "id": graph_task_id,
            "name": "tools",
            "error": error,
            "interrupts": [],
            "result": {},
        },
    }


def _task_tool_result(
    *,
    namespace: tuple[str, ...] = (),
    tool_call_id: str,
    status: str = "success",
) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": namespace,
        "data": (
            ToolMessage(
                content=f"result-{tool_call_id}",
                name="task",
                tool_call_id=tool_call_id,
                id=f"message-{tool_call_id}",
                status=status,
            ),
            {
                "lc_agent_name": None if not namespace else "researcher",
                "langgraph_node": "tools",
            },
        ),
    }


def test_task_start_emits_sanitized_raw_with_subagent_correlation() -> None:
    adapter = _adapter()

    events = adapter.process(
        _task_start(
            graph_task_id="graph-a",
            tool_call_id="call-parent-a",
            description="并行任务 A",
        )
    )

    assert len(events) == 1
    raw = events[0]
    assert isinstance(raw, RawEvent)
    assert raw.source == "langgraph.tasks"
    assert raw.raw_event == {"type": "tasks", "phase": "start", "ns": []}
    assert raw.event == {
        "data": {
            "id": "graph-a",
            "name": "tools",
            "input": [
                {
                    "name": "task",
                    "args": {
                        "description": "并行任务 A",
                        "subagent_type": "researcher",
                    },
                    "id": "call-parent-a",
                    "type": "tool_call",
                }
            ],
            "triggers": ["__pregel_push"],
            "metadata": {
                "ls_integration": "deepagents",
            },
        },
        "provenance": {
            "kind": "root",
            "agentType": "main",
            "agentName": "main",
            "graphNamespace": [],
            "subagents": [
                create_subagent_provenance(
                    identity=_identity(),
                    graph_namespace=("tools:graph-a",),
                    parent_graph_namespace=(),
                    graph_task_id="graph-a",
                    agent_name="researcher",
                    parent_tool_call_id=_tool_id((), "call-parent-a"),
                    description="并行任务 A",
                ).model_dump(mode="json", by_alias=True)
            ],
        },
    }


def test_conflicting_duplicate_task_start_preserves_original_correlation() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-same",
            tool_call_id="call-same",
            description="first-description",
            subagent_type="researcher",
        )
    )

    with pytest.raises(ValueError, match="conflicting task start"):
        adapter.process(
            _task_start(
                graph_task_id="graph-same",
                tool_call_id="call-same",
                description="second-description",
                subagent_type="analyst",
            )
        )

    child_namespace = ("tools:graph-same",)
    with pytest.raises(
        ValueError,
        match="expected='researcher' actual='analyst'",
    ):
        adapter.process(
            {
                "type": "messages",
                "ns": child_namespace,
                "data": (
                    AIMessageChunk(id="message-analyst", content="must not emit"),
                    {"lc_agent_name": "analyst", "langgraph_node": "model"},
                ),
            }
        )

    events = adapter.process(
        {
            "type": "messages",
            "ns": child_namespace,
            "data": (
                AIMessageChunk(
                    id="message-researcher",
                    content="accepted",
                    chunk_position="last",
                ),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        }
    )

    assert [event.type.value for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]


def test_identical_duplicate_task_start_is_idempotent() -> None:
    adapter = _adapter()
    start = _task_start(
        graph_task_id="graph-same",
        tool_call_id="call-same",
        description="same-description",
        subagent_type="researcher",
    )

    first = adapter.process(start)
    duplicate = adapter.process(start)

    assert len(first) == len(duplicate) == 1
    assert isinstance(first[0], RawEvent)
    assert isinstance(duplicate[0], RawEvent)
    assert duplicate[0].model_dump(
        mode="json", by_alias=True, exclude_none=True
    ) == first[0].model_dump(mode="json", by_alias=True, exclude_none=True)


@pytest.mark.parametrize(
    ("initial_calls", "replayed_calls", "probe_is_original"),
    [
        (
            (
                ("call-a", "task A", "researcher"),
                ("call-b", "task B", "analyst"),
                ("call-c", "task C", "reviewer"),
            ),
            (
                ("call-a", "task A", "researcher"),
                ("call-b", "task B", "analyst"),
            ),
            True,
        ),
        (
            (
                ("call-a", "task A", "researcher"),
                ("call-b", "task B", "analyst"),
            ),
            (
                ("call-a", "task A", "researcher"),
                ("call-b", "task B", "analyst"),
                ("call-c", "task C", "reviewer"),
            ),
            False,
        ),
    ],
    ids=("shrink", "expand"),
)
def test_task_start_replay_rejects_a_different_ordered_invocation_set_atomically(
    initial_calls: tuple[tuple[str, str, str], ...],
    replayed_calls: tuple[tuple[str, str, str], ...],
    probe_is_original: bool,
) -> None:
    adapter = _adapter()
    adapter.process(_task_group_start(graph_task_id="graph-set", calls=initial_calls))

    with pytest.raises(ValueError, match="conflicting task start"):
        adapter.process(
            _task_group_start(graph_task_id="graph-set", calls=replayed_calls)
        )

    probe = {
        "type": "messages",
        "ns": ("tools:graph-set:2",),
        "data": (
            AIMessageChunk(
                id="message-probe",
                content="probe",
                chunk_position="last",
            ),
            {"lc_agent_name": "reviewer", "langgraph_node": "model"},
        ),
    }
    if probe_is_original:
        assert [event.type.value for event in adapter.process(probe)] == [
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
        ]
    else:
        with pytest.raises(
            RuntimeError,
            match="subgraph stream arrived before its native task-start",
        ):
            adapter.process(probe)


@pytest.mark.parametrize(
    "changed_field",
    ["name", "input", "triggers", "metadata", "metadata-omitted"],
)
def test_task_start_replay_rejects_a_changed_full_payload(
    changed_field: str,
) -> None:
    adapter = _adapter()
    original = _task_start(
        graph_task_id="graph-full-payload",
        tool_call_id="call-original",
        description="original task",
    )
    adapter.process(original)
    replay = _task_start(
        graph_task_id="graph-full-payload",
        tool_call_id="call-original",
        description="original task",
    )
    replay_data = replay["data"]
    assert isinstance(replay_data, dict)
    if changed_field == "name":
        replay_data["name"] = "model"
        replay_data["input"] = {"messages": []}
    elif changed_field == "input":
        replay_data["input"] = [
            {
                "name": "task",
                "args": {
                    "description": "original task",
                    "subagent_type": "researcher",
                },
                "id": "call-original",
                "type": "tool_call",
            },
            {
                "name": "read_file",
                "args": {"file_path": "extra.txt"},
                "id": "call-extra",
                "type": "tool_call",
            },
        ]
    elif changed_field == "triggers":
        replay_data["triggers"] = ("changed-trigger",)
    elif changed_field == "metadata":
        replay_data["metadata"] = {
            "ls_integration": "deepagents",
            "attempt": 2,
        }
    else:
        replay_data.pop("metadata")

    with pytest.raises(ValueError, match="conflicting task start"):
        adapter.process(replay)

    events = adapter.process(
        {
            "type": "messages",
            "ns": ("tools:graph-full-payload",),
            "data": (
                AIMessageChunk(
                    id="message-original",
                    content="original remains valid",
                    chunk_position="last",
                ),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        }
    )
    assert [event.type.value for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]


@pytest.mark.parametrize(
    ("first_value", "replayed_value"),
    [(True, 1), (1, 1.0), (False, 0)],
    ids=("bool-int", "int-float", "false-zero"),
)
def test_task_start_fingerprint_compares_json_scalar_types_strictly(
    first_value: object,
    replayed_value: object,
) -> None:
    adapter = _adapter()

    def start(value: object) -> dict[str, object]:
        return {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-json-types",
                "name": "model",
                "input": {"value": value},
                "triggers": ("branch:to:model",),
            },
        }

    first = adapter.process(start(first_value))

    with pytest.raises(ValueError, match="conflicting task start"):
        adapter.process(start(replayed_value))

    exact_replay = adapter.process(start(first_value))
    assert exact_replay[0].model_dump(
        mode="json", by_alias=True, exclude_none=True
    ) == first[0].model_dump(mode="json", by_alias=True, exclude_none=True)


def test_nested_task_start_keeps_full_parent_and_child_namespace() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="outer",
            tool_call_id="call-outer",
            description="外层任务",
        )
    )

    events = adapter.process(
        _task_start(
            namespace=("tools:outer",),
            graph_task_id="inner",
            tool_call_id="call-inner",
            description="内层任务",
            subagent_type="analyst",
        )
    )

    raw = next(event for event in events if isinstance(event, RawEvent))
    assert raw.raw_event == {
        "type": "tasks",
        "phase": "start",
        "ns": ["tools:outer"],
    }
    provenance = raw.event["provenance"]
    assert provenance["graphNamespace"] == ["tools:outer"]
    assert provenance["graphTaskId"] == "outer"
    assert provenance["subagents"] == [
        create_subagent_provenance(
            identity=_identity(),
            graph_namespace=("tools:outer", "tools:inner"),
            parent_graph_namespace=("tools:outer",),
            graph_task_id="inner",
            agent_name="analyst",
            parent_tool_call_id=_tool_id(("tools:outer",), "call-inner"),
            description="内层任务",
        ).model_dump(mode="json", by_alias=True)
    ]


def test_parallel_task_results_use_parent_tool_call_id_not_result_order() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-a",
            tool_call_id="call-parent-a",
            description="任务 A",
        )
    )
    adapter.process(
        _task_start(
            graph_task_id="graph-b",
            tool_call_id="call-parent-b",
            description="任务 B",
        )
    )

    result_b = next(
        event
        for event in adapter.process(_task_tool_result(tool_call_id="call-parent-b"))
        if isinstance(event, AttachmentToolCallResultEvent)
    )
    result_a = next(
        event
        for event in adapter.process(_task_tool_result(tool_call_id="call-parent-a"))
        if isinstance(event, AttachmentToolCallResultEvent)
    )

    assert _raw_event(result_b)["relatedGraphNamespace"] == ["tools:graph-b"]
    assert _raw_event(result_a)["relatedGraphNamespace"] == ["tools:graph-a"]
    payload = result_b.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert "relatedRunId" not in payload
    AttachmentToolCallResultEvent.model_validate(payload)


def test_one_tool_node_correlates_multiple_parallel_subagent_invocations() -> None:
    adapter = _adapter()

    events = adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-group",
                "name": "tools",
                "input": [
                    {
                        "name": "task",
                        "args": {
                            "description": "任务 A",
                            "subagent_type": "researcher",
                        },
                        "id": "call-group-a",
                        "type": "tool_call",
                    },
                    {
                        "name": "task",
                        "args": {
                            "description": "任务 B",
                            "subagent_type": "analyst",
                        },
                        "id": "call-group-b",
                        "type": "tool_call",
                    },
                ],
                "triggers": ("__pregel_push",),
            },
        }
    )

    raw = next(event for event in events if isinstance(event, RawEvent))
    subagents = raw.event["provenance"]["subagents"]
    assert {tuple(item["graphNamespace"]) for item in subagents} == {
        ("tools:graph-group:0",),
        ("tools:graph-group:1",),
    }
    assert {item["parentToolCallId"] for item in subagents} == {
        _tool_id((), "call-group-a"),
        _tool_id((), "call-group-b"),
    }


def test_duplicate_task_call_id_leaves_no_correlation_and_allows_retry() -> None:
    adapter = _adapter()
    duplicate_start = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "graph-duplicate",
            "name": "tools",
            "input": [
                {
                    "name": "task",
                    "args": {
                        "description": "duplicate A",
                        "subagent_type": "researcher",
                    },
                    "id": "same-call",
                    "type": "tool_call",
                },
                {
                    "name": "task",
                    "args": {
                        "description": "duplicate B",
                        "subagent_type": "analyst",
                    },
                    "id": "same-call",
                    "type": "tool_call",
                },
            ],
            "triggers": ("__pregel_push",),
        },
    }

    with pytest.raises(ValueError, match="duplicate parent tool call ID: same-call"):
        adapter.process(duplicate_start)

    for namespace in (
        ("tools:graph-duplicate:0",),
        ("tools:graph-duplicate:1",),
    ):
        with pytest.raises(
            RuntimeError,
            match="subgraph stream arrived before its native task-start",
        ):
            adapter.process(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (
                        AIMessageChunk(id="leak-probe", content="must not emit"),
                        {"lc_agent_name": None, "langgraph_node": "model"},
                    ),
                }
            )

    adapter.process(
        _task_start(
            graph_task_id="graph-retry",
            tool_call_id="fresh-call",
            description="valid retry",
        )
    )
    retry_events = adapter.process(
        {
            "type": "messages",
            "ns": ("tools:graph-retry",),
            "data": (
                AIMessageChunk(id="retry-message", content="retry accepted"),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        }
    )

    assert [event.type.value for event in retry_events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
    ]


def test_orphan_subgraph_names_do_not_poison_later_correlation() -> None:
    adapter = _adapter()
    namespace = ("tools:orphan-name",)

    for index, agent_name in enumerate(("poison-a", "poison-b")):
        with pytest.raises(
            RuntimeError,
            match="subgraph stream arrived before its native task-start",
        ):
            adapter.process(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (
                        AIMessageChunk(
                            id=f"orphan-message-{index}",
                            content="must not emit",
                        ),
                        {
                            "lc_agent_name": agent_name,
                            "langgraph_node": "model",
                        },
                    ),
                }
            )

    assert adapter.finish() == []

    adapter.process(
        _task_start(
            graph_task_id="orphan-name",
            tool_call_id="legal-call",
            description="legal correlation",
            subagent_type="poison-b",
        )
    )
    events = adapter.process(
        {
            "type": "messages",
            "ns": namespace,
            "data": (
                AIMessageChunk(id="legal-message", content="accepted"),
                {"lc_agent_name": "poison-b", "langgraph_node": "model"},
            ),
        }
    )

    assert [event.type.value for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
    ]


def test_task_error_is_raw_provenance_not_a_standard_run_terminal() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-error",
            tool_call_id="call-error",
            description="失败任务",
        )
    )

    events = adapter.process(_task_result(graph_task_id="graph-error", error="boom"))

    assert len(events) == 1
    raw = events[0]
    assert isinstance(raw, RawEvent)
    assert raw.raw_event == {"type": "tasks", "phase": "result", "ns": []}
    assert raw.event["data"] == {
        "id": "graph-error",
        "name": "tools",
        "error": "boom",
        "interrupts": [],
        "result": {},
    }


def test_task_result_requires_a_matching_start_and_is_idempotent() -> None:
    adapter = _adapter()
    result = _task_result(graph_task_id="graph-result", error=None)

    with pytest.raises(ValueError, match="has no matching task start"):
        adapter.process(result)

    start = _task_start(
        graph_task_id="graph-result",
        tool_call_id="call-result",
        description="result correlation",
    )
    adapter.process(start)
    first = adapter.process(result)
    duplicate = adapter.process(result)

    assert len(first) == 1
    assert isinstance(first[0], RawEvent)
    assert duplicate == []


@pytest.mark.parametrize("conflict", ["name", "error", "interrupts", "result"])
def test_task_result_conflict_is_rejected_before_publishing_another_raw(
    conflict: str,
) -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-result-conflict",
            tool_call_id="call-result-conflict",
            description="result conflict",
        )
    )
    original = _task_result(graph_task_id="graph-result-conflict", error=None)
    adapter.process(original)
    changed = _task_result(graph_task_id="graph-result-conflict", error=None)
    changed_data = changed["data"]
    assert isinstance(changed_data, dict)
    if conflict == "name":
        changed_data["name"] = "model"
    elif conflict == "error":
        changed_data["error"] = "changed"
    elif conflict == "interrupts":
        changed_data["interrupts"] = [{"id": "changed"}]
    else:
        changed_data["result"] = {"messages": ["changed"]}

    with pytest.raises(ValueError, match="conflicting task result"):
        adapter.process(changed)

    assert adapter.process(original) == []


def test_abort_does_not_synthesize_subagent_run_terminals() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="outer",
            tool_call_id="call-outer",
            description="外层任务",
        )
    )
    adapter.process(
        _task_start(
            namespace=("tools:outer",),
            graph_task_id="inner",
            tool_call_id="call-inner",
            description="内层任务",
        )
    )

    events = adapter.abort()
    assert events == []


def test_tool_message_error_status_is_preserved_in_raw_event() -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessage(
                    id="message-tool",
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/a.txt"},
                            "id": "call-read",
                            "type": "tool_call",
                        }
                    ],
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    result = next(
        event
        for event in adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    ToolMessage(
                        content="file missing",
                        name="read_file",
                        tool_call_id="call-read",
                        id="tool-message-read",
                        status="error",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "tools"},
                ),
            }
        )
        if isinstance(event, AttachmentToolCallResultEvent)
    )

    assert _raw_event(result)["toolResultStatus"] == "error"


def test_tool_result_preserves_reasoning_shaped_business_data() -> None:
    adapter = _adapter()
    reasoning_secret = "tool-reasoning-secret"
    thinking_secret = "tool-thinking-secret"
    nested_secret = "tool-nested-reasoning-secret"
    tool_message = ToolMessage(
        content=[
            {"type": "reasoning", "reasoning": reasoning_secret},
            {"type": "thinking", "thinking": thinking_secret},
            {"type": "text", "text": "Visible tool result"},
            {
                "type": "data",
                "payload": {"reasoning_content": nested_secret},
                "visible": "kept",
            },
        ],
        name="read_file",
        tool_call_id="call-private-tool",
        id="tool-message-private",
    )
    assistant_message = AIMessage(
        id="message-private-tool",
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"file_path": "/a.txt"},
                "id": "call-private-tool",
                "type": "tool_call",
            }
        ],
    )
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                assistant_message,
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    live_events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                tool_message,
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )
    interrupt_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [assistant_message, tool_message]},
            "interrupts": (
                {"id": "interrupt-private-tool", "value": {"pause": "review"}},
            ),
        }
    )
    result = next(
        event
        for event in live_events
        if isinstance(event, AttachmentToolCallResultEvent)
    )
    snapshot = next(
        event
        for event in interrupt_events
        if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    serialized = "\n".join(
        event.model_dump_json(by_alias=True, exclude_none=True)
        for event in (result, snapshot)
    )

    for business_value in (reasoning_secret, thinking_secret, nested_secret):
        assert business_value in serialized
    assert isinstance(result.content, str)
    tool_result = snapshot.messages[-1]
    assert isinstance(tool_result, AgUiToolMessage)
    assert "Visible tool result" in result.content
    assert "Visible tool result" in tool_result.content
    assert '"visible": "kept"' in result.content
    assert '"visible": "kept"' in tool_result.content


def test_real_sample_shape_keeps_parallel_chunks_namespaces_and_results() -> None:
    """Cover empty content, fragmented arguments, parallel slots, and reversed results."""

    adapter = _adapter()
    model_message_id = "lc-main-parallel"
    events: list[BaseEvent] = []

    def feed_chunk(
        *,
        namespace: tuple[str, ...],
        agent_name: str | None,
        tool_call_id: str | None,
        tool_name: str | None,
        index: int,
        args: str,
        message_id: str = model_message_id,
    ) -> None:
        events.extend(
            adapter.process(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (
                        AIMessageChunk(
                            id=message_id,
                            content="",
                            tool_call_chunks=[
                                {
                                    "name": tool_name,
                                    "args": args,
                                    "id": tool_call_id,
                                    "index": index,
                                    "type": "tool_call_chunk",
                                }
                            ],
                        ),
                        {"lc_agent_name": agent_name, "langgraph_node": "model"},
                    ),
                }
            )
        )

    # The tasks at mainagent.txt:395/457 share one message ID but use distinct
    # indexes. Later argument frames mirror the capture by omitting name and ID.
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id="call-parent-a",
        tool_name="task",
        index=1,
        args="",
    )
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id=None,
        tool_name=None,
        index=1,
        args='{"description":"研究 A",',
    )
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id="call-parent-b",
        tool_name="task",
        index=2,
        args="",
    )
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id=None,
        tool_name=None,
        index=2,
        args='{"description":"研究 B",',
    )
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id=None,
        tool_name=None,
        index=1,
        args='"subagent_type":"researcher"}',
    )
    feed_chunk(
        namespace=(),
        agent_name=None,
        tool_call_id=None,
        tool_name=None,
        index=2,
        args='"subagent_type":"researcher"}',
    )

    # Native task starts establish parent Tool-call to child-namespace mappings
    # before subgraph output arrives.
    adapter.process(
        _task_start(
            graph_task_id="graph-a",
            tool_call_id="call-parent-a",
            description="研究 A",
        )
    )
    adapter.process(
        _task_start(
            graph_task_id="graph-b",
            tool_call_id="call-parent-b",
            description="研究 B",
        )
    )

    # Subgraph frames carry native namespaces. Reusing message IDs and indexes here
    # proves that scoped correlation does not cross streams.
    feed_chunk(
        namespace=("tools:graph-a",),
        agent_name="researcher",
        tool_call_id="call-child-search",
        tool_name="web_search",
        index=1,
        args="",
    )
    feed_chunk(
        namespace=("tools:graph-a",),
        agent_name="researcher",
        tool_call_id=None,
        tool_name=None,
        index=1,
        args='{"query":"业务 A"}',
    )

    assert not any(
        isinstance(event, (TextMessageStartEvent, TextMessageContentEvent))
        for event in events
    )
    starts = [event for event in events if isinstance(event, ToolCallStartEvent)]
    assert [(event.tool_call_id, event.tool_call_name) for event in starts] == [
        (_tool_id((), "call-parent-a"), "task"),
        (_tool_id((), "call-parent-b"), "task"),
        (_tool_id(("tools:graph-a",), "call-child-search"), "web_search"),
    ]
    args_by_call: dict[str, str] = {}
    for event in events:
        if isinstance(event, ToolCallArgsEvent):
            args_by_call[event.tool_call_id] = (
                args_by_call.get(event.tool_call_id, "") + event.delta
            )
    assert args_by_call == {
        _tool_id((), "call-parent-a"): (
            '{"description":"研究 A","subagent_type":"researcher"}'
        ),
        _tool_id((), "call-parent-b"): (
            '{"description":"研究 B","subagent_type":"researcher"}'
        ),
        _tool_id(("tools:graph-a",), "call-child-search"): ('{"query":"业务 A"}'),
    }

    results: list[AttachmentToolCallResultEvent] = []
    for tool_call_id in ("call-parent-b", "call-parent-a"):
        results.extend(
            event
            for event in adapter.process(_task_tool_result(tool_call_id=tool_call_id))
            if isinstance(event, AttachmentToolCallResultEvent)
        )

    # Results at mainagent.txt:524/525 arrive in B/A order and still correlate by
    # exact tool_call_id.
    assert [event.tool_call_id for event in results] == [
        _tool_id((), "call-parent-b"),
        _tool_id((), "call-parent-a"),
    ]
    assert [_raw_event(event)["relatedGraphNamespace"] for event in results] == [
        ["tools:graph-b"],
        ["tools:graph-a"],
    ]


def test_root_and_subgraph_reused_native_ids_have_distinct_ag_ui_ids() -> None:
    adapter = _adapter()
    events: list[BaseEvent] = []
    events.extend(
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(
                        id="shared-message",
                        content="root",
                        chunk_position="last",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )
    )
    adapter.process(
        _task_start(
            graph_task_id="graph-collision",
            tool_call_id="parent-call",
            description="collision child",
        )
    )
    events.extend(
        adapter.process(
            {
                "type": "messages",
                "ns": ("tools:graph-collision",),
                "data": (
                    AIMessageChunk(
                        id="shared-message",
                        content="child",
                        chunk_position="last",
                    ),
                    {
                        "lc_agent_name": "researcher",
                        "langgraph_node": "model",
                    },
                ),
            }
        )
    )

    message_ids = [
        event.message_id for event in events if isinstance(event, TextMessageStartEvent)
    ]
    assert len(message_ids) == 2
    assert len(set(message_ids)) == 2


def test_root_and_subgraph_reused_tool_ids_have_distinct_ag_ui_ids() -> None:
    adapter = _adapter()
    events: list[BaseEvent] = []
    events.extend(
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(
                        id="root-message",
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "search",
                                "args": "{}",
                                "id": "shared-call",
                                "index": 0,
                                "type": "tool_call_chunk",
                            }
                        ],
                        chunk_position="last",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )
    )
    adapter.process(
        _task_start(
            graph_task_id="graph-tool-collision",
            tool_call_id="parent-call",
            description="collision child",
        )
    )
    child_namespace = ("tools:graph-tool-collision",)
    events.extend(
        adapter.process(
            {
                "type": "messages",
                "ns": child_namespace,
                "data": (
                    AIMessageChunk(
                        id="child-message",
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "search",
                                "args": "{}",
                                "id": "shared-call",
                                "index": 0,
                                "type": "tool_call_chunk",
                            }
                        ],
                        chunk_position="last",
                    ),
                    {
                        "lc_agent_name": "researcher",
                        "langgraph_node": "model",
                    },
                ),
            }
        )
    )

    starts = [event for event in events if isinstance(event, ToolCallStartEvent)]
    assert [event.tool_call_id for event in starts] == [
        _tool_id((), "shared-call"),
        _tool_id(child_namespace, "shared-call"),
    ]
    assert starts[0].tool_call_id != starts[1].tool_call_id


def test_write_todos_content_comes_only_from_values_state() -> None:
    adapter = _adapter()
    state_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {
                "todos": [
                    {"content": "读取资料", "status": "in_progress"},
                    {"content": "输出报告", "status": "pending"},
                ],
                "files": {},
            },
            "interrupts": (),
        }
    )
    snapshot = next(
        event for event in state_events if isinstance(event, StateSnapshotEvent)
    )
    assert snapshot.snapshot["todos"] == [
        {"content": "读取资料", "status": "in_progress"},
        {"content": "输出报告", "status": "pending"},
    ]

    result = next(
        event
        for event in adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    ToolMessage(
                        content=(
                            "Updated todo list to "
                            "[{'content': '读取资料', 'status': 'in_progress'}]"
                        ),
                        name="write_todos",
                        tool_call_id="call-todos",
                        id="tool-message-todos",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "tools"},
                ),
            }
        )
        if isinstance(event, AttachmentToolCallResultEvent)
    )
    assert result.tool_call_id == _tool_id((), "call-todos")
    assert result.content == (
        "Updated todo list to [{'content': '读取资料', 'status': 'in_progress'}]"
    )


@pytest.mark.parametrize(
    ("first_value", "second_value"),
    [(True, 1), (1, 1.0), (False, 0)],
    ids=("bool-int", "int-float", "false-zero"),
)
def test_state_delta_preserves_json_scalar_type_changes(
    first_value: object,
    second_value: object,
) -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"value": first_value},
            "interrupts": (),
        }
    )

    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"value": second_value},
            "interrupts": (),
        }
    )

    delta = next(event for event in events if isinstance(event, StateDeltaEvent))
    assert delta.delta == [{"op": "replace", "path": "/value", "value": second_value}]


def test_hitl_actions_map_to_the_unique_ordered_tool_call_subsequence() -> None:
    adapter = _adapter()
    final_message = AIMessage(
        id="message-write",
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": []},
                "id": "call-unreviewed",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "/b.txt", "content": "call-write-b"},
                "id": "call-write-b",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "/a.txt", "content": "call-write-a"},
                "id": "call-write-a",
                "type": "tool_call",
            },
        ],
    )
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                final_message,
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-write",
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {
                                    "file_path": "/b.txt",
                                    "content": "call-write-b",
                                },
                            },
                            {
                                "name": "write_file",
                                "args": {
                                    "file_path": "/a.txt",
                                    "content": "call-write-a",
                                },
                            },
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve"],
                            },
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["edit", "reject"],
                            },
                        ],
                    },
                },
            ),
        }
    )

    adapter.finish()
    outcome = adapter.main_outcome()

    assert outcome.type == "interrupt"
    assert [item.tool_call_id for item in outcome.interrupts] == [
        _tool_id((), "call-write-b"),
        _tool_id((), "call-write-a"),
    ]
    assert all(
        item.metadata is not None
        and item.metadata["deepagents"].get("callMatch") is None
        for item in outcome.interrupts
    )


def test_hitl_action_matches_the_unique_checkpoint_message_not_only_the_last() -> None:
    adapter = _adapter()
    reviewed_message = AIMessage(
        id="message-reviewed-branch",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/reviewed.txt"},
                "id": "call-reviewed-branch",
                "type": "tool_call",
            }
        ],
    )
    other_message = AIMessage(
        id="message-other-branch",
        content="",
        tool_calls=[
            {
                "name": "ask_user",
                "args": {"question": "Continue?"},
                "id": "call-other-branch",
                "type": "tool_call",
            }
        ],
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [reviewed_message, other_message]},
            "interrupts": (
                {
                    "id": "interrupt-reviewed-branch",
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"file_path": "/reviewed.txt"},
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
    )

    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (), "call-reviewed-branch"
    )


def test_hitl_response_schema_exposes_only_allowed_native_decisions() -> None:
    adapter = _adapter()
    final_message = AIMessage(
        id="message-review",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt"},
                "id": "call-review",
                "type": "tool_call",
            }
        ],
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-review",
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"file_path": "a.txt"},
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": [
                                    "approve",
                                    "edit",
                                    "reject",
                                    "respond",
                                ],
                            }
                        ],
                    },
                },
            ),
        }
    )

    interrupt = adapter.main_outcome().interrupts[0]
    assert interrupt.response_schema == {
        "oneOf": [
            {
                "type": "object",
                "properties": {"type": {"const": "approve"}},
                "required": ["type"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "edit"},
                    "edited_action": {
                        "type": "object",
                        "required": ["name", "args"],
                        "properties": {
                            "name": {"const": "write_file"},
                            "args": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["type", "edited_action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "reject"},
                    "message": {"type": "string"},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "respond"},
                    "message": {"type": "string"},
                },
                "required": ["type", "message"],
                "additionalProperties": False,
            },
        ]
    }


def test_hitl_edit_response_schema_preserves_review_args_schema() -> None:
    adapter = _adapter()
    args_schema = {
        "type": "object",
        "required": ["file_path"],
        "properties": {
            "file_path": {"type": "string", "pattern": "^/workspace/"},
        },
        "additionalProperties": False,
    }
    final_message = AIMessage(
        id="message-edit-schema",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/workspace/a.txt"},
                "id": "call-edit-schema",
                "type": "tool_call",
            }
        ],
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-edit-schema",
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"file_path": "/workspace/a.txt"},
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["edit"],
                                "args_schema": args_schema,
                            }
                        ],
                    },
                },
            ),
        }
    )

    response_schema = adapter.main_outcome().interrupts[0].response_schema
    assert response_schema is not None
    edit_args_schema = response_schema["oneOf"][0]["properties"]["edited_action"][
        "properties"
    ]["args"]
    assert edit_args_schema == args_schema


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
def test_hitl_operational_args_remain_exact_under_reasoning_privacy(
    expose_reasoning_events: bool,
) -> None:
    args = {
        "additional_kwargs": {
            "reasoning_content": "TOOL-ARG-BUSINESS",
            "keep": "tool-sibling",
        },
        "reasoning_content": "TOOL-TOP-LEVEL",
        "type": "thinking",
    }
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        expose_reasoning_events=expose_reasoning_events,
    )
    final_message = AIMessage(
        id="message-operational-review",
        content="",
        tool_calls=[
            {
                "name": "business_tool",
                "args": args,
                "id": "call-operational-review",
                "type": "tool_call",
            }
        ],
    )

    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-operational",
                    "value": {
                        "action_requests": [{"name": "business_tool", "args": args}],
                        "review_configs": [
                            {
                                "action_name": "business_tool",
                                "allowed_decisions": ["approve"],
                            }
                        ],
                    },
                },
            ),
        }
    )

    snapshot = next(
        event for event in events if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    assistant = snapshot.messages[0]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.tool_calls is not None
    assert json.loads(assistant.tool_calls[0].function.arguments) == args
    interrupt = adapter.main_outcome().interrupts[0]
    assert interrupt.metadata is not None
    assert interrupt.metadata["deepagents"]["schema"] == TOOL_REVIEW_SCHEMA
    assert interrupt.metadata["deepagents"]["originalArgs"] == args
    assert interrupt.metadata["deepagents"]["nativeInterruptId"] == (
        "interrupt-operational"
    )
    assert interrupt.metadata["deepagents"]["actionIndex"] == 0
    assert interrupt.metadata["langgraphValue"]["action_requests"][0]["args"] == args
    assert parse_tool_review_interrupt(interrupt).original_args.root == args


def test_interrupt_emits_closed_stream_and_authoritative_snapshots_in_order() -> None:
    adapter = _adapter()
    live_events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-pending",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "write_file",
                            "args": '{"file_path":"a.txt"}',
                            "id": "call-pending",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )
    final_message = AIMessage(
        id="message-pending",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "a.txt"},
                "id": "call-pending",
                "type": "tool_call",
            }
        ],
    )

    interrupt_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-pending",
                    "value": {
                        "action_requests": [
                            {"name": "write_file", "args": {"file_path": "a.txt"}}
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                },
            ),
        }
    )

    assert [type(event) for event in interrupt_events] == [
        ToolCallEndEvent,
        StateSnapshotEvent,
        AttachmentMessagesSnapshotEvent,
    ]
    start = next(
        event for event in live_events if isinstance(event, ToolCallStartEvent)
    )
    arguments = next(
        event for event in live_events if isinstance(event, ToolCallArgsEvent)
    )
    assert isinstance(arguments, ToolCallArgsEvent)
    assert arguments.delta == '{"file_path":"a.txt"}'
    messages = interrupt_events[-1]
    assert isinstance(messages, AttachmentMessagesSnapshotEvent)
    assistant = messages.messages[0]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.id == _message_id((), "message-pending")
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0].id == start.tool_call_id
    assert adapter.finish() == []


def test_ambiguous_hitl_tool_correlation_is_rejected() -> None:
    adapter = _adapter()
    duplicate_calls = [
        {
            "name": "write_file",
            "args": {"file_path": "same.txt"},
            "id": call_id,
            "type": "tool_call",
        }
        for call_id in ("call-first", "call-second")
    ]
    final_message = AIMessage(
        id="message-ambiguous",
        content="",
        tool_calls=duplicate_calls,
    )
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                final_message,
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    with pytest.raises(HitlCorrelationError, match="ambiguous"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"messages": [final_message]},
                "interrupts": (
                    {
                        "id": "interrupt-ambiguous",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "write_file",
                                    "args": {"file_path": "same.txt"},
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
        )


def test_hitl_checkpoint_ignores_a_completed_historical_duplicate() -> None:
    adapter = _adapter()
    arguments = {"file_path": "/same.txt"}
    historical = AIMessage(
        id="message-completed-history",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": arguments,
                "id": "call-completed-history",
                "type": "tool_call",
            }
        ],
    )
    result = ToolMessage(
        id="result-completed-history",
        content="done",
        name="write_file",
        tool_call_id="call-completed-history",
    )
    current = AIMessage(
        id="message-current-review",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": arguments,
                "id": "call-current-review",
                "type": "tool_call",
            }
        ],
    )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [historical, result, current]},
            "interrupts": (
                {
                    "id": "interrupt-current-review",
                    "value": {
                        "action_requests": [{"name": "write_file", "args": arguments}],
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
    )

    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (),
        "call-current-review",
    )


def test_hitl_stream_history_ignores_a_completed_historical_duplicate() -> None:
    adapter = _adapter()
    arguments = {"file_path": "/same.txt"}
    metadata = {"lc_agent_name": None, "langgraph_node": "model"}
    for message in (
        AIMessage(
            id="message-completed-stream-history",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": arguments,
                    "id": "call-completed-stream-history",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id="result-completed-stream-history",
            content="done",
            name="write_file",
            tool_call_id="call-completed-stream-history",
        ),
        AIMessage(
            id="message-current-stream-review",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": arguments,
                    "id": "call-current-stream-review",
                    "type": "tool_call",
                }
            ],
        ),
    ):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (message, metadata),
            }
        )

    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {},
            "interrupts": (
                {
                    "id": "interrupt-current-stream-review",
                    "value": {
                        "action_requests": [{"name": "write_file", "args": arguments}],
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
    )

    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (),
        "call-current-stream-review",
    )


def test_hitl_history_fallback_rejects_cross_message_ambiguity() -> None:
    adapter = _adapter()
    metadata = {"lc_agent_name": None, "langgraph_node": "model"}
    for message_id, call_id in (
        ("message-history-first", "call-history-first"),
        ("message-history-second", "call-history-second"),
    ):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessage(
                        id=message_id,
                        content="",
                        tool_calls=[
                            {
                                "name": "write_file",
                                "args": {"file_path": "/same.txt"},
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    ),
                    metadata,
                ),
            }
        )

    with pytest.raises(HitlCorrelationError, match="ambiguous"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {},
                "interrupts": (
                    {
                        "id": "interrupt-history-ambiguous",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "write_file",
                                    "args": {"file_path": "/same.txt"},
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
        )


def test_hitl_history_ambiguity_is_not_hidden_by_malformed_arguments() -> None:
    adapter = _adapter()
    metadata = {"lc_agent_name": None, "langgraph_node": "model"}
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-history-malformed",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "write_file",
                            "args": '{"file_path":',
                            "id": "call-history-malformed",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                metadata,
            ),
        }
    )
    for message_id, call_id in (
        ("message-history-valid-first", "call-history-valid-first"),
        ("message-history-valid-second", "call-history-valid-second"),
    ):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessage(
                        id=message_id,
                        content="",
                        tool_calls=[
                            {
                                "name": "write_file",
                                "args": {"file_path": "/same.txt"},
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    ),
                    metadata,
                ),
            }
        )

    with pytest.raises(HitlCorrelationError, match="ambiguous") as raised:
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {},
                "interrupts": (
                    {
                        "id": "interrupt-history-valid-ambiguous",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "write_file",
                                    "args": {"file_path": "/same.txt"},
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
        )

    assert not isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_hitl_history_reports_malformed_tool_arguments() -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-malformed-history",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "write_file",
                            "args": '{"file_path":',
                            "id": "call-malformed-history",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    with pytest.raises(HitlCorrelationError, match="invalid JSON") as raised:
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {},
                "interrupts": (
                    {
                        "id": "interrupt-malformed-history",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "write_file",
                                    "args": {"file_path": "expected.txt"},
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
        )

    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def _history_tool_part(
    *,
    message_id: str,
    calls: Sequence[tuple[int, str, str, str]],
    namespace: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": namespace,
        "data": (
            AIMessageChunk(
                id=message_id,
                content="",
                tool_call_chunks=[
                    {
                        "name": name,
                        "args": arguments,
                        "id": call_id,
                        "index": index,
                        "type": "tool_call_chunk",
                    }
                    for index, name, arguments, call_id in calls
                ],
            ),
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }


def _hitl_history_interrupt_part(
    *,
    action_groups: Sequence[Sequence[tuple[str, dict[str, object]]]],
    namespace: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "values",
        "ns": namespace,
        "data": {},
        "interrupts": tuple(
            {
                "id": f"interrupt-history-{group_index}",
                "value": {
                    "action_requests": [
                        {"name": name, "args": args} for name, args in actions
                    ],
                    "review_configs": [
                        {
                            "action_name": name,
                            "allowed_decisions": ["approve"],
                        }
                        for name, _args in actions
                    ],
                },
            }
            for group_index, actions in enumerate(action_groups)
        ),
    }


def test_hitl_history_rejects_a_same_name_malformed_alternative() -> None:
    adapter = _adapter()
    adapter.process(
        _history_tool_part(
            message_id="message-malformed-alternative",
            calls=((0, "write_file", '{"path":', "call-malformed-alternative"),),
        )
    )
    adapter.process(
        _history_tool_part(
            message_id="message-valid-alternative",
            calls=((0, "write_file", '{"path":"ok"}', "call-valid-alternative"),),
        )
    )

    with pytest.raises(HitlCorrelationError, match="invalid JSON") as raised:
        adapter.process(
            _hitl_history_interrupt_part(
                action_groups=((("write_file", {"path": "ok"}),),)
            )
        )

    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_hitl_history_keeps_valid_sibling_of_other_name_malformed_call() -> None:
    adapter = _adapter()
    adapter.process(
        _history_tool_part(
            message_id="message-valid-sibling",
            calls=(
                (0, "search", '{"query":', "call-unrelated-malformed"),
                (1, "write_file", '{"path":"ok"}', "call-valid-sibling"),
            ),
        )
    )

    adapter.process(
        _hitl_history_interrupt_part(action_groups=((("write_file", {"path": "ok"}),),))
    )

    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (), "call-valid-sibling"
    )


def test_hitl_history_ignores_malformed_call_that_cannot_match_by_position() -> None:
    adapter = _adapter()
    adapter.process(
        _history_tool_part(
            message_id="message-positioned-calls",
            calls=(
                (0, "write_file", '{"path":', "call-position-malformed"),
                (1, "read_file", '{"path":"input"}', "call-position-read"),
                (2, "write_file", '{"path":"ok"}', "call-position-write"),
            ),
        )
    )

    adapter.process(
        _hitl_history_interrupt_part(
            action_groups=(
                (
                    ("read_file", {"path": "input"}),
                    ("write_file", {"path": "ok"}),
                ),
            )
        )
    )

    assert [
        interrupt.tool_call_id for interrupt in adapter.main_outcome().interrupts
    ] == [
        _tool_id((), "call-position-read"),
        _tool_id((), "call-position-write"),
    ]


def test_hitl_history_rejects_relevant_malformed_sibling_in_same_message() -> None:
    adapter = _adapter()
    adapter.process(
        _history_tool_part(
            message_id="message-relevant-sibling",
            calls=(
                (0, "write_file", '{"path":', "call-relevant-malformed"),
                (1, "write_file", '{"path":"ok"}', "call-relevant-valid"),
            ),
        )
    )

    with pytest.raises(HitlCorrelationError, match="invalid JSON"):
        adapter.process(
            _hitl_history_interrupt_part(
                action_groups=((("write_file", {"path": "ok"}),),)
            )
        )


def test_hitl_history_ignores_same_name_malformed_call_in_other_namespace() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-malformed-child",
            tool_call_id="call-malformed-child-parent",
            description="Run malformed child",
        )
    )
    child_namespace = ("tools:graph-malformed-child",)
    adapter.process(
        _history_tool_part(
            message_id="message-malformed-child",
            calls=((0, "write_file", '{"path":', "call-malformed-child"),),
            namespace=child_namespace,
        )
    )
    adapter.process(
        _history_tool_part(
            message_id="message-valid-root",
            calls=((0, "write_file", '{"path":"ok"}', "call-valid-root"),),
        )
    )

    adapter.process(
        _hitl_history_interrupt_part(action_groups=((("write_file", {"path": "ok"}),),))
    )

    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (), "call-valid-root"
    )


def test_hitl_history_rejects_relevant_malformed_candidate_across_groups() -> None:
    adapter = _adapter()
    for message_id, calls in (
        (
            "message-group-malformed",
            ((0, "write_file", '{"path":', "call-group-malformed"),),
        ),
        (
            "message-group-write",
            ((0, "write_file", '{"path":"ok"}', "call-group-write"),),
        ),
        (
            "message-group-read",
            ((0, "read_file", '{"path":"input"}', "call-group-read"),),
        ),
    ):
        adapter.process(_history_tool_part(message_id=message_id, calls=calls))

    with pytest.raises(HitlCorrelationError, match="invalid JSON") as raised:
        adapter.process(
            _hitl_history_interrupt_part(
                action_groups=(
                    (("write_file", {"path": "ok"}),),
                    (("read_file", {"path": "input"}),),
                )
            )
        )

    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize("action_value", [True, 1, 1.0])
def test_hitl_correlation_matches_json_argument_types_exactly(
    action_value: object,
) -> None:
    adapter = _adapter()
    final_message = AIMessage(
        id="message-json-types",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"value": value},
                "id": call_id,
                "type": "tool_call",
            }
            for value, call_id in (
                (True, "call-bool"),
                (1, "call-int"),
                (1.0, "call-float"),
            )
        ],
    )
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                final_message,
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )
    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [final_message]},
            "interrupts": (
                {
                    "id": "interrupt-json-types",
                    "value": {
                        "action_requests": [
                            {"name": "write_file", "args": {"value": action_value}}
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
    )

    expected_id = {
        bool: "call-bool",
        int: "call-int",
        float: "call-float",
    }[type(action_value)]
    assert adapter.main_outcome().interrupts[0].tool_call_id == _tool_id(
        (), expected_id
    )


def test_conflicting_interrupt_replay_is_rejected_before_state_commit() -> None:
    adapter = _adapter()

    def part(*, description: str, state: str) -> dict[str, object]:
        message = AIMessage(
            id="message-interrupt-replay",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "a.txt"},
                    "id": "call-interrupt-replay",
                    "type": "tool_call",
                }
            ],
        )
        return {
            "type": "values",
            "ns": (),
            "data": {"messages": [message], "state": state},
            "interrupts": (
                {
                    "id": "interrupt-replay",
                    "value": {
                        "action_requests": [
                            {
                                "name": "write_file",
                                "args": {"file_path": "a.txt"},
                                "description": description,
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

    first_part = part(description="first", state="first")
    adapter.process(first_part)
    exact_replay = adapter.process(first_part)
    assert [event.type.value for event in exact_replay] == [
        "STATE_SNAPSHOT",
        "MESSAGES_SNAPSHOT",
    ]

    with pytest.raises(ValueError, match="conflicting interrupt"):
        adapter.process(part(description="changed", state="must-not-commit"))

    outcome = adapter.main_outcome()
    assert outcome.interrupts[0].message == "first"
    retry = adapter.process(part(description="first", state="retry"))
    snapshot = next(event for event in retry if isinstance(event, StateSnapshotEvent))
    assert snapshot.snapshot == {"state": "retry"}


def test_derived_interrupt_ids_cannot_collide_with_native_ids() -> None:
    adapter = _adapter()
    messages = [
        AIMessage(
            id="message-interrupt-collision",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"slot": slot},
                    "id": f"call-{slot}",
                    "type": "tool_call",
                }
                for slot in range(3)
            ],
        )
    ]

    with pytest.raises(ValueError, match="duplicate.*interrupt ID"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"messages": messages, "state": "must-not-commit"},
                "interrupts": (
                    {
                        "id": "batch",
                        "value": {
                            "action_requests": [
                                {"name": "write_file", "args": {"slot": 0}},
                                {"name": "write_file", "args": {"slot": 1}},
                            ],
                            "review_configs": [
                                {
                                    "action_name": "write_file",
                                    "allowed_decisions": ["approve"],
                                },
                                {
                                    "action_name": "write_file",
                                    "allowed_decisions": ["approve"],
                                },
                            ],
                        },
                    },
                    {
                        "id": "batch#1",
                        "value": {
                            "action_requests": [
                                {"name": "write_file", "args": {"slot": 2}}
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
        )

    retry = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"state": "retry"},
            "interrupts": (),
        }
    )
    snapshot = next(event for event in retry if isinstance(event, StateSnapshotEvent))
    assert snapshot.snapshot == {"state": "retry"}


def test_unmatched_hitl_tool_correlation_is_rejected_with_typed_error() -> None:
    adapter = _adapter()
    final_message = AIMessage(
        id="message-unmatched",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "actual.txt"},
                "id": "call-actual",
                "type": "tool_call",
            }
        ],
    )

    with pytest.raises(HitlCorrelationError, match="cannot be correlated"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"messages": [final_message]},
                "interrupts": (
                    {
                        "id": "interrupt-unmatched",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "write_file",
                                    "args": {"file_path": "expected.txt"},
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
        )


def test_subagent_values_do_not_overwrite_main_ui_state() -> None:
    adapter = _adapter()
    adapter.process(
        _task_start(
            graph_task_id="graph-state",
            tool_call_id="call-state",
            description="子智能体状态",
        )
    )

    events = adapter.process(
        {
            "type": "values",
            "ns": ("tools:graph-state",),
            "data": {
                "todos": [{"content": "子任务", "status": "in_progress"}],
            },
            "interrupts": (),
        }
    )

    assert not any(isinstance(event, StateSnapshotEvent) for event in events)
    assert len(events) == 1
    raw = events[0]
    assert isinstance(raw, RawEvent)
    assert raw.source == "langgraph.values"
    assert raw.raw_event == {"type": "values", "ns": ["tools:graph-state"]}
    assert raw.event["state"] == {
        "todos": [{"content": "子任务", "status": "in_progress"}]
    }


def test_malformed_tasks_payload_is_rejected_at_parser_boundary() -> None:
    adapter = _adapter()

    with pytest.raises(AgUiStreamContractError) as raised:
        adapter.process(
            {
                "type": "tasks",
                "ns": (),
                "data": {
                    "id": "graph-invalid",
                    "name": "tools",
                    "input": "not-a-tool-call-sequence",
                    "triggers": [],
                },
            }
        )

    assert raised.value.code is AgUiAdapterErrorCode.STREAM_CONTRACT_INVALID
    assert isinstance(raised.value.cause, NativeStreamContractError)
    assert isinstance(raised.value.cause.cause, ValidationError)


def test_ai_message_without_stable_id_is_rejected_before_stream_state_changes() -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="message-open", content="visible"),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    with pytest.raises(ValueError, match="stable ID"):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(content="invalid"),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )

    assert [event.type.value for event in adapter.finish()] == ["TEXT_MESSAGE_END"]


@pytest.mark.parametrize(
    ("tool_chunk", "message"),
    [
        (
            {
                "name": None,
                "args": "{}",
                "id": None,
                "index": 0,
                "type": "tool_call_chunk",
            },
            "no scoped start",
        ),
        (
            {
                "name": "search",
                "args": "{}",
                "id": None,
                "index": 0,
                "type": "tool_call_chunk",
            },
            "both id and name",
        ),
        (
            {"name": "", "args": "{}", "id": "", "index": 0, "type": "tool_call_chunk"},
            "no scoped start",
        ),
        (
            {
                "name": "search",
                "args": "{}",
                "id": "",
                "index": 0,
                "type": "tool_call_chunk",
            },
            "both id and name",
        ),
        (
            {
                "name": "",
                "args": "{}",
                "id": "call-1",
                "index": 0,
                "type": "tool_call_chunk",
            },
            "both id and name",
        ),
        (
            {
                "name": "",
                "args": "{}",
                "id": "",
                "index": None,
                "type": "tool_call_chunk",
            },
            "both id and name",
        ),
        (
            {
                "name": "search",
                "args": "{}",
                "id": None,
                "index": None,
                "type": "tool_call_chunk",
            },
            "both id and name",
        ),
    ],
)
def test_tool_fragments_require_full_stable_correlation(
    tool_chunk: dict[str, object],
    message: str,
) -> None:
    adapter = _adapter()

    with pytest.raises(ValueError, match=message):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(
                        id="message-tool-validation",
                        content="",
                        tool_call_chunks=[cast(ToolCallChunk, tool_chunk)],
                    ),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )


@pytest.mark.parametrize(
    ("second_name", "second_message_id", "second_index", "second_args"),
    [
        ("read_file", "message-first", 0, "{}"),
        ("read_file", "message-second", 0, "{}"),
        ("read_file", "message-first", 1, "{}"),
        ("read_file", "message-first", 0, '{"different":true}'),
    ],
    ids=("exact", "parent", "index", "arguments"),
)
def test_in_run_ended_tool_id_rejects_every_new_start_before_mutation(
    second_name: str,
    second_message_id: str,
    second_index: int,
    second_args: str,
) -> None:
    adapter = _adapter()

    def part(
        name: str,
        *,
        message_id: str,
        index: int,
        arguments: str,
    ) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id=message_id,
                    content="",
                    tool_call_chunks=[
                        {
                            "name": name,
                            "args": arguments,
                            "id": "call-ended",
                            "index": index,
                            "type": "tool_call_chunk",
                        }
                    ],
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }

    assert [
        event.type.value
        for event in adapter.process(
            part(
                "read_file",
                message_id="message-first",
                index=0,
                arguments="{}",
            )
        )
    ] == ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END"]

    with pytest.raises(ValueError, match="ended tool-call ID cannot start again"):
        adapter.process(
            part(
                second_name,
                message_id=second_message_id,
                index=second_index,
                arguments=second_args,
            )
        )

    assert adapter.finish() == []


def test_ended_tool_slot_rejects_a_different_id_for_the_same_message_index() -> None:
    adapter = _adapter()

    def part(tool_call_id: str) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-stable-slot",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": "{}",
                            "id": tool_call_id,
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }

    assert [event.type.value for event in adapter.process(part("call-first"))] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
    ]

    with pytest.raises(ValueError, match="tool-call indices cannot change IDs"):
        adapter.process(part("call-second"))

    assert adapter.finish() == []


def test_tool_result_rejects_a_name_conflicting_with_its_streamed_call() -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-streamed-tool",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": "{}",
                            "id": "call-name-conflict",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    with pytest.raises(ValueError, match="ToolMessage name does not match"):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    ToolMessage(
                        id="result-name-conflict",
                        name="write_file",
                        tool_call_id="call-name-conflict",
                        content="done",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "tools"},
                ),
            }
        )

    assert adapter.finish() == []


def test_resumed_adapter_suppresses_only_verified_prior_tool_lifecycles() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(run_id="run-resumed"),
        prior_tool_call_ids=frozenset({_tool_id((), "call-prior")}),
    )

    prior_events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    content="prior done",
                    name="write_file",
                    tool_call_id="call-prior",
                    id="result-prior",
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )
    new_events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    content="new done",
                    name="read_file",
                    tool_call_id="call-new",
                    id="result-new",
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )

    assert [type(event) for event in prior_events] == [AttachmentToolCallResultEvent]
    assert [type(event) for event in new_events] == [
        ToolCallStartEvent,
        ToolCallEndEvent,
        AttachmentToolCallResultEvent,
    ]
    orphan_start = new_events[0]
    assert isinstance(orphan_start, ToolCallStartEvent)
    assert orphan_start.parent_message_id is None


def test_resumed_adapter_rejects_unscoped_prior_tool_ids() -> None:
    with pytest.raises(ValueError, match="complete scoped Tool IDs"):
        DeepAgentAgUiAdapter(
            identity=_identity(run_id="run-resumed"),
            prior_tool_call_ids=frozenset({"call-prior"}),
        )


def test_failed_tool_snapshot_preserves_error_status_for_history_consumers() -> None:
    adapter = _adapter()
    assistant = AIMessage(
        id="a", content="", tool_calls=[{"id": "call", "name": "read_file", "args": {}}]
    )
    failed = ToolMessage(
        id="result", tool_call_id="call", content="file missing", status="error"
    )
    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [assistant, failed]},
            "interrupts": ({"id": "review", "value": {"pause": "review"}},),
        }
    )
    snapshot = next(
        event for event in events if isinstance(event, AttachmentMessagesSnapshotEvent)
    )
    result = snapshot.messages[-1]
    assert isinstance(result, AgUiToolMessage)
    assert result.error == "file missing"
    assert result.content == "file missing"


@pytest.mark.parametrize("namespace", [(), ("tools:child",)])
def test_unindexed_parallel_tool_calls_keep_independent_arguments_and_results(
    namespace: tuple[str, ...],
) -> None:
    adapter = _adapter()
    if namespace:
        adapter.process(
            _task_start(
                graph_task_id="child", tool_call_id="delegate", description="Inspect"
            )
        )
    events: list[BaseEvent] = []
    for call_id, value in [("call-one", "one"), ("call-two", "two")]:
        events.extend(
            adapter.process(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (
                        AIMessageChunk(
                            id="ollama-message",
                            content="",
                            tool_calls=[
                                {
                                    "id": call_id,
                                    "name": "diagnostic_echo",
                                    "args": {"value": value},
                                },
                            ],
                        ),
                        {"langgraph_node": "model"},
                    ),
                }
            )
        )
    for call_id in ["call-two", "call-one"]:
        events.extend(
            adapter.process(
                {
                    "type": "messages",
                    "ns": namespace,
                    "data": (
                        ToolMessage(
                            id=f"result-{call_id}",
                            content="ok",
                            tool_call_id=call_id,
                            name="diagnostic_echo",
                        ),
                        {"langgraph_node": "tools"},
                    ),
                }
            )
        )
    events.extend(adapter.finish())
    for call_id, value in [("call-one", "one"), ("call-two", "two")]:
        call_events = [
            event
            for event in events
            if getattr(event, "tool_call_id", None) == _tool_id(namespace, call_id)
        ]
        assert [event.type.value for event in call_events] == [
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
        ]
        args = [
            event.delta for event in call_events if isinstance(event, ToolCallArgsEvent)
        ]
        assert json.loads("".join(args)) == {"value": value}
    assert adapter.finish() == []


def test_unindexed_hitl_history_preserves_proposal_order_across_tool_names() -> None:
    adapter = _adapter()
    calls: list[ToolCallChunk] = [
        {"id": "first", "name": "write_file", "args": '{"path":"one"}', "index": None},
        {"id": "middle", "name": "read_file", "args": '{"path":"two"}', "index": None},
        {"id": "last", "name": "write_file", "args": '{"path":"three"}', "index": None},
    ]
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="review-message", content="", tool_call_chunks=calls),
                {"langgraph_node": "model"},
            ),
        }
    )
    adapter.process(
        _hitl_history_interrupt_part(
            action_groups=(
                (
                    ("write_file", {"path": "one"}),
                    ("read_file", {"path": "two"}),
                    ("write_file", {"path": "three"}),
                ),
            )
        )
    )
    assert [item.tool_call_id for item in adapter.main_outcome().interrupts] == [
        _tool_id((), call_id) for call_id in ["first", "middle", "last"]
    ]


def test_indexed_fragments_and_unindexed_calls_do_not_share_a_slot() -> None:
    adapter = _adapter()
    chunks: list[ToolCallChunk] = [
        {"id": "indexed", "name": "echo", "args": '{"value":', "index": 0},
        {"id": "unindexed", "name": "echo", "args": '{"value":"other"}', "index": None},
        {"id": None, "name": None, "args": '"original"}', "index": 0},
    ]
    events: list[BaseEvent] = []
    for chunk in chunks:
        events.extend(
            adapter.process(
                {
                    "type": "messages",
                    "ns": (),
                    "data": (
                        AIMessageChunk(
                            id="mixed-message", content="", tool_call_chunks=[chunk]
                        ),
                        {"langgraph_node": "model"},
                    ),
                }
            )
        )
    events.extend(adapter.finish())
    for call_id, expected in [("indexed", "original"), ("unindexed", "other")]:
        deltas = [
            event.delta
            for event in events
            if isinstance(event, ToolCallArgsEvent)
            and event.tool_call_id == _tool_id((), call_id)
        ]
        assert json.loads("".join(deltas)) == {"value": expected}
    with pytest.raises(HitlCorrelationError, match="checkpoint message order"):
        adapter.process(
            _hitl_history_interrupt_part(
                action_groups=(
                    (
                        ("echo", {"value": "original"}),
                        ("echo", {"value": "other"}),
                    ),
                )
            )
        )
