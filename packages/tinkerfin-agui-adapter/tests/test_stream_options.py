from __future__ import annotations

import pytest
from ag_ui.core import RawEvent, StateSnapshotEvent
from langchain_core.messages import AIMessageChunk
from pydantic_core import PydanticSerializationError

from tinkerfin_agui_adapter import (
    AgUiStreamContractError,
    DeepAgentAgUiAdapter,
    RunIdentity,
)


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


def _task_start(*, namespace: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": namespace,
        "data": {
            "id": "graph-task-1",
            "name": "tools",
            "input": [
                {
                    "name": "task",
                    "args": {
                        "description": "Research the topic",
                        "subagent_type": "researcher",
                    },
                    "id": "tool-call-1",
                    "type": "tool_call",
                }
            ],
            "triggers": ("branch:to:tools",),
        },
    }


def test_extra_modes_emit_sanitized_raw_events() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    events = adapter.process(
        {
            "type": "updates",
            "ns": (),
            "data": {
                "node": {
                    "content": "visible",
                    "additional_kwargs": {
                        "reasoning_content": "private",
                        "provider": "deepseek",
                    },
                }
            },
        }
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, RawEvent)
    assert event.source == "langgraph.updates"
    assert event.raw_event == {"type": "updates", "ns": []}
    assert event.event == {
        "data": {
            "node": {
                "content": "visible",
                "additional_kwargs": {"provider": "deepseek"},
            }
        },
        "provenance": {
            "kind": "root",
            "agentType": "main",
            "agentName": "main",
            "graphNamespace": [],
        },
    }


def test_updates_drop_only_top_level_private_state_channels_per_node() -> None:
    private_key = "_framework_private"
    nested = {private_key: "business-value"}
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        private_state_keys=frozenset({private_key}),
    )

    event = adapter.process(
        {
            "type": "updates",
            "ns": (),
            "data": {
                "first": {private_key: "internal", "nested": nested},
                "second": "non-mapping-update",
            },
        }
    )[0]

    assert isinstance(event, RawEvent)
    assert event.event["data"] == {
        "first": {"nested": nested},
        "second": "non-mapping-update",
    }


def test_updates_require_a_node_mapping() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    with pytest.raises(AgUiStreamContractError, match="node mapping"):
        adapter.process({"type": "updates", "ns": (), "data": ["invalid"]})


def test_checkpoint_projection_drops_runtime_configuration_and_task_state() -> None:
    private_key = "_framework_private"
    nested = {private_key: "business-value"}
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        private_state_keys=frozenset({private_key}),
    )

    event = adapter.process(
        {
            "type": "checkpoints",
            "ns": (),
            "data": {
                "config": {"configurable": {"thread_id": "secret-thread"}},
                "parent_config": {"configurable": {"checkpoint_id": "secret-parent"}},
                "metadata": {
                    "step": 3,
                    "source": "loop",
                    "parents": {"child": "secret-checkpoint"},
                },
                "values": {
                    "answer": 42,
                    private_key: "internal",
                    "nested": nested,
                },
                "next": ["model"],
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "model",
                        "result": {
                            "answer": 42,
                            private_key: "internal",
                            "nested": nested,
                        },
                        "interrupts": [],
                        "state": {"configurable": {"checkpoint_id": "secret-child"}},
                    }
                ],
            },
        }
    )[0]

    assert isinstance(event, RawEvent)
    assert event.event["data"] == {
        "step": 3,
        "values": {"answer": 42, "nested": nested},
        "next": ["model"],
        "tasks": [
            {
                "id": "task-1",
                "name": "model",
                "result": {"answer": 42, "nested": nested},
                "interrupts": [],
            }
        ],
    }
    assert "secret" not in event.model_dump_json()


def test_private_state_policy_covers_subgraphs_debug_and_interrupt_snapshot() -> None:
    private_key = "_framework_private"
    nested = {private_key: "business-value"}
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        private_state_keys=frozenset({private_key}),
    )
    adapter.process(_task_start())
    child = adapter.process(
        {
            "type": "values",
            "ns": ("tools:graph-task-1",),
            "data": {private_key: "internal", "nested": nested},
            "interrupts": (),
        }
    )[0]
    assert isinstance(child, RawEvent)
    assert child.event["state"] == {"nested": nested}

    debug_parts = (
        {
            "type": "checkpoint",
            "payload": {
                "values": {private_key: "internal", "nested": nested},
                "next": [],
            },
        },
        {
            "type": "task",
            "payload": {
                "id": "debug-task",
                "name": "model",
                "input": {private_key: "internal", "nested": nested},
                "triggers": [],
            },
        },
        {
            "type": "task_result",
            "payload": {
                "id": "debug-task",
                "name": "model",
                "error": None,
                "interrupts": [],
                "result": {private_key: "internal", "nested": nested},
            },
        },
    )
    for step, data in enumerate(debug_parts):
        event = adapter.process(
            {
                "type": "debug",
                "ns": (),
                "data": {"step": step, **data},
            }
        )[0]
        assert isinstance(event, RawEvent)
        serialized = event.model_dump_json(by_alias=True)
        assert '"internal"' not in serialized
        assert '"business-value"' in serialized

    interrupt_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {private_key: "internal", "nested": nested, "messages": []},
            "interrupts": (
                {
                    "id": "runtime-pause",
                    "value": {
                        "schema": "tinkerfin.runtime-interrupt",
                        "kind": "tinkerfin:pause",
                        "message": "Continue?",
                        "responseSchema": {"type": "object"},
                        "metadata": {},
                    },
                },
            ),
        }
    )
    snapshot = next(
        event for event in interrupt_events if isinstance(event, StateSnapshotEvent)
    )
    assert snapshot.snapshot == {"nested": nested}


def test_checkpoint_projection_normalizes_task_exceptions() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    event = adapter.process(
        {
            "type": "checkpoints",
            "ns": (),
            "data": {
                "config": {"configurable": {"thread_id": "private-thread"}},
                "parent_config": None,
                "metadata": {"step": 4},
                "values": {},
                "next": [],
                "tasks": [
                    {
                        "id": "task-error",
                        "name": "model",
                        "error": RuntimeError("boom"),
                        "state": {"configurable": {"checkpoint_id": "private"}},
                    }
                ],
            },
        }
    )[0]

    assert isinstance(event, RawEvent)
    assert event.event["data"]["tasks"] == [
        {
            "id": "task-error",
            "name": "model",
            "error": {"type": "TaskError"},
        }
    ]
    assert "private" not in event.model_dump_json()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {
                "step": 2,
                "timestamp": "2026-08-13T00:00:00Z",
                "type": "checkpoint",
                "payload": {
                    "config": {"configurable": {"thread_id": "private-thread"}},
                    "parent_config": {
                        "configurable": {"checkpoint_id": "private-parent"}
                    },
                    "metadata": {
                        "step": 2,
                        "source": "loop",
                        "parents": {"child": "private-checkpoint"},
                    },
                    "values": {"answer": 42},
                    "next": ["model"],
                    "tasks": [
                        {
                            "id": "task-1",
                            "name": "model",
                            "interrupts": [],
                            "state": {
                                "configurable": {"checkpoint_id": "private-child"}
                            },
                        }
                    ],
                },
            },
            {
                "step": 2,
                "timestamp": "2026-08-13T00:00:00Z",
                "type": "checkpoint",
                "payload": {
                    "step": 2,
                    "values": {"answer": 42},
                    "next": ["model"],
                    "tasks": [{"id": "task-1", "name": "model", "interrupts": []}],
                },
            },
        ),
        (
            {
                "step": 3,
                "timestamp": "2026-08-13T00:00:01Z",
                "type": "task",
                "payload": {
                    "id": "task-2",
                    "name": "model",
                    "input": {
                        "content": "visible",
                        "additional_kwargs": {
                            "reasoning_content": "private-reasoning",
                            "provider": "deepseek",
                        },
                    },
                    "triggers": ["branch:to:model"],
                    "metadata": {"langgraph_checkpoint_ns": "private-checkpoint"},
                },
            },
            {
                "step": 3,
                "timestamp": "2026-08-13T00:00:01Z",
                "type": "task",
                "payload": {
                    "id": "task-2",
                    "name": "model",
                    "input": {
                        "content": "visible",
                        "additional_kwargs": {"provider": "deepseek"},
                    },
                    "triggers": ["branch:to:model"],
                },
            },
        ),
        (
            {
                "step": 3,
                "timestamp": "2026-08-13T00:00:02Z",
                "type": "task_result",
                "payload": {
                    "id": "task-2",
                    "name": "model",
                    "error": RuntimeError("boom"),
                    "interrupts": [],
                    "result": {
                        "answer": 42,
                        "additional_kwargs": {"reasoning_content": "private-reasoning"},
                    },
                },
            },
            {
                "step": 3,
                "timestamp": "2026-08-13T00:00:02Z",
                "type": "task_result",
                "payload": {
                    "id": "task-2",
                    "name": "model",
                    "error": {"type": "TaskError"},
                    "interrupts": [],
                    "result": {"answer": 42, "additional_kwargs": {}},
                },
            },
        ),
    ],
)
def test_debug_projection_whitelists_each_native_payload(
    data: dict[str, object],
    expected: dict[str, object],
) -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    event = adapter.process({"type": "debug", "ns": (), "data": data})[0]

    assert isinstance(event, RawEvent)
    assert event.event["data"] == expected
    assert "private" not in event.model_dump_json()


def test_root_custom_mode_emits_a_sanitized_raw_event() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    event = adapter.process(
        {
            "type": "custom",
            "ns": (),
            "data": {
                "progress": 0.5,
                "additional_kwargs": {"reasoning_content": "private"},
            },
        }
    )[0]

    assert isinstance(event, RawEvent)
    assert event.source == "langgraph.custom"
    assert event.event["data"] == {"progress": 0.5, "additional_kwargs": {}}


def test_extra_mode_rejects_opaque_values_before_event_construction() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    with pytest.raises(AgUiStreamContractError) as raised:
        adapter.process(
            {
                "type": "custom",
                "ns": (),
                "data": {"opaque": object()},
            }
        )
    assert isinstance(raised.value.cause, PydanticSerializationError)


def test_disabled_subagent_events_are_consumed_without_public_output() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity(), expose_subagent_events=False)
    root_events = adapter.process(_task_start())

    child_namespace = ("tools:graph-task-1",)
    child_parts = [
        {
            "type": "tasks",
            "ns": child_namespace,
            "data": {
                "id": "child-model-task",
                "name": "model",
                "input": {"messages": []},
                "triggers": ("branch:to:model",),
            },
        },
        {
            "type": "messages",
            "ns": child_namespace,
            "data": (
                AIMessageChunk(
                    content="hidden", id="child-message", chunk_position="last"
                ),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        },
        {
            "type": "values",
            "ns": child_namespace,
            "data": {"messages": [], "private_state": True},
            "interrupts": (),
        },
        {"type": "custom", "ns": child_namespace, "data": {"hidden": True}},
    ]

    child_events = [event for part in child_parts for event in adapter.process(part)]
    finish_events = adapter.finish()

    assert len(root_events) == 1
    assert isinstance(root_events[0], RawEvent)
    assert root_events[0].source == "langgraph.tasks"
    assert child_events == []
    assert finish_events == []


def test_root_state_boundary_does_not_leak_subagent_end_events() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity(), expose_subagent_events=False)
    adapter.process(_task_start())
    child_namespace = ("tools:graph-task-1",)

    assert (
        adapter.process(
            {
                "type": "messages",
                "ns": child_namespace,
                "data": (
                    AIMessageChunk(content="hidden", id="child-message"),
                    {"lc_agent_name": "researcher", "langgraph_node": "model"},
                ),
            }
        )
        == []
    )

    boundary_events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": []},
            "interrupts": (),
        }
    )

    assert boundary_events == []
    assert adapter.finish() == []
