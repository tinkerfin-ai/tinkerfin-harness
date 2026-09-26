from __future__ import annotations

import json

import pytest
from ag_ui.core import (
    RawEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolCallResultEvent,
)
from langchain_core.messages import AIMessageChunk, ToolMessage

from tinkerfin_agui_adapter import DeepAgentAgUiAdapter, RunIdentity, astream_events
from tinkerfin_agui_adapter.ids import ScopedIdCodec


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")


def _root_tool_id(raw_id: str) -> str:
    return ScopedIdCodec().encode("tool", (), raw_id)


def _task(*, phase: str, secret: str) -> dict[str, object]:
    data: dict[str, object] = {"id": "task-1", "name": "model"}
    if phase == "start":
        data.update(
            input={
                "provider": {
                    "additional_kwargs": {
                        "reasoning_content": secret,
                        "keep": "input-sibling",
                    }
                },
                "reasoning_content": "input-business",
            },
            triggers=("branch:to:model",),
            metadata={
                "additional_kwargs": {
                    "reasoning_content": secret,
                    "keep": "metadata-sibling",
                }
            },
        )
    else:
        data.update(
            error={
                "additional_kwargs": {
                    "reasoning_content": secret,
                    "keep": "error-sibling",
                },
                "reasoning_content": "error-business",
            },
            result={
                "provider": {
                    "additional_kwargs": {
                        "reasoning_content": secret,
                        "keep": "result-sibling",
                    }
                },
                "reasoning_content": "result-business",
            },
            interrupts=[],
        )
    return {"type": "tasks", "ns": (), "data": data}


@pytest.mark.parametrize("expose_reasoning_events", [False, True])
def test_task_raw_filters_provider_reasoning_in_every_phase(
    expose_reasoning_events: bool,
) -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(), expose_reasoning_events=expose_reasoning_events
    )

    events = [
        *adapter.process(_task(phase="start", secret="START-SECRET")),
        *adapter.process(_task(phase="result", secret="RESULT-SECRET")),
    ]

    assert all(isinstance(event, RawEvent) for event in events)
    serialized = "\n".join(
        event.model_dump_json(by_alias=True, exclude_none=True) for event in events
    )
    assert "START-SECRET" not in serialized
    assert "RESULT-SECRET" not in serialized
    assert "error-sibling" not in serialized
    assert "error-business" not in serialized
    for visible in (
        "input-sibling",
        "metadata-sibling",
        "result-sibling",
        "input-business",
        "result-business",
    ):
        assert visible in serialized


def test_private_exception_details_still_detect_conflicting_task_replays() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(_task(phase="start", secret="provider-secret"))
    payload = {
        "id": "task-1",
        "name": "model",
        "result": {},
        "interrupts": [],
        "error": RuntimeError("private-first"),
    }
    result = {"type": "tasks", "ns": (), "data": payload}
    events = adapter.process(result)
    assert "private-first" not in events[0].model_dump_json()
    assert adapter.process(result) == []
    with pytest.raises(ValueError, match="conflicting task result"):
        adapter.process(
            {
                **result,
                "data": {**payload, "error": RuntimeError("private-second")},
            }
        )


def test_task_tool_args_are_preserved_as_operational_data() -> None:
    args = {
        "additional_kwargs": {"reasoning_content": "TOOL-BUSINESS", "keep": True},
        "reasoning_content": "TOP-BUSINESS",
        "type": "thinking",
    }
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    raw = adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-tools",
                "name": "tools",
                "input": [
                    {
                        "name": "business_tool",
                        "args": args,
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
                "triggers": ("__pregel_push",),
            },
        }
    )[0]

    assert isinstance(raw, RawEvent)
    assert raw.event["data"]["input"][0]["args"] == args


def test_duplicate_tool_message_emits_result_exactly_once() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    part = {
        "type": "messages",
        "ns": (),
        "data": (
            ToolMessage(
                id="result-1",
                name="read_file",
                tool_call_id="call-1",
                content="done",
            ),
            {"lc_agent_name": None, "langgraph_node": "tools"},
        ),
    }

    first = adapter.process(part)
    second = adapter.process(part)

    assert [event.type.value for event in first] == [
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
    ]
    assert second == []


def test_conflicting_duplicate_tool_message_fails_correlation() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())

    def part(content: str) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    id="result-1",
                    name="read_file",
                    tool_call_id="call-1",
                    content=content,
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }

    adapter.process(part("first"))
    with pytest.raises(ValueError, match="conflicting ToolMessage"):
        adapter.process(part("second"))


def test_nameless_tool_message_uses_the_correlated_streamed_tool_name() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-tool",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": "{}",
                            "id": "call-nameless-result",
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

    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    id="result-nameless",
                    name=None,
                    tool_call_id="call-nameless-result",
                    content="done",
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )

    result = next(event for event in events if isinstance(event, ToolCallResultEvent))
    assert result.role == "tool"
    assert result.content == "done"
    assert adapter.finish() == []


def _native_tool_start(
    *,
    graph_task_id: str,
    tool_name: str,
    tool_call_id: str,
    args: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": graph_task_id,
            "name": "tools",
            "input": [
                {
                    "name": tool_name,
                    "args": {} if args is None else args,
                    "id": tool_call_id,
                    "type": "tool_call",
                }
            ],
            "triggers": ("__pregel_push",),
            "metadata": {
                "ls_integration": "deepagents",
                "lc_versions": {"deepagents": "0.7.5"},
                "lc_agent_name": None,
            },
        },
    }


def _tool_result(
    *,
    tool_call_id: str,
    name: str | None,
    content: str = "ok",
) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": (),
        "data": (
            ToolMessage(
                id=f"result-{tool_call_id}",
                content=content,
                tool_call_id=tool_call_id,
                name=name,
            ),
            {"lc_agent_name": None, "langgraph_node": "tools"},
        ),
    }


def test_resume_task_start_supplies_canonical_name_for_nameless_result_replays() -> (
    None
):
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        prior_tool_call_ids=frozenset({_root_tool_id("call-approved")}),
    )
    adapter.process(
        _native_tool_start(
            graph_task_id="native-tools-task",
            tool_name="approved_tool",
            tool_call_id="call-approved",
        )
    )

    first = adapter.process(_tool_result(tool_call_id="call-approved", name=None))
    nameless_replay = adapter.process(
        _tool_result(tool_call_id="call-approved", name=None)
    )
    named_replay = adapter.process(
        _tool_result(tool_call_id="call-approved", name="approved_tool")
    )

    assert [event.type.value for event in first] == ["TOOL_CALL_RESULT"]
    assert nameless_replay == []
    assert named_replay == []
    with pytest.raises(ValueError, match="conflicting ToolMessage"):
        adapter.process(
            _tool_result(
                tool_call_id="call-approved",
                name=None,
                content="changed",
            )
        )


def test_wrong_explicit_result_name_is_atomic_before_nameless_retry() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        prior_tool_call_ids=frozenset({_root_tool_id("call-approved")}),
    )
    adapter.process(
        _native_tool_start(
            graph_task_id="native-tools-task",
            tool_name="approved_tool",
            tool_call_id="call-approved",
        )
    )

    with pytest.raises(ValueError, match="ToolMessage name does not match"):
        adapter.process(
            _tool_result(tool_call_id="call-approved", name="different_tool")
        )

    assert [
        event.type.value
        for event in adapter.process(
            _tool_result(tool_call_id="call-approved", name=None)
        )
    ] == ["TOOL_CALL_RESULT"]


def test_consumed_prior_tool_id_cannot_silently_restart() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        prior_tool_call_ids=frozenset({_root_tool_id("call-approved")}),
    )
    adapter.process(
        _native_tool_start(
            graph_task_id="native-tools-task",
            tool_name="approved_tool",
            tool_call_id="call-approved",
        )
    )
    adapter.process(_tool_result(tool_call_id="call-approved", name=None))

    with pytest.raises(ValueError, match="ended tool-call ID cannot start again"):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(
                        id="assistant-reuse",
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "approved_tool",
                                "args": "{}",
                                "id": "call-approved",
                                "index": 0,
                                "type": "tool_call_chunk",
                            }
                        ],
                    ),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )


def test_nameless_task_result_preserves_logical_subagent_reference() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=_identity(),
        prior_tool_call_ids=frozenset({_root_tool_id("call-task")}),
    )
    started = adapter.process(
        _native_tool_start(
            graph_task_id="native-task-node",
            tool_name="task",
            tool_call_id="call-task",
            args={
                "description": "Research",
                "subagent_type": "researcher",
            },
        )
    )

    result = next(
        event
        for event in adapter.process(_tool_result(tool_call_id="call-task", name=None))
        if isinstance(event, ToolCallResultEvent)
    )

    assert isinstance(result.raw_event, dict)
    raw = next(event for event in started if isinstance(event, RawEvent))
    assert (
        result.raw_event["relatedSubagentInvocationId"]
        == raw.event["provenance"]["subagents"][0]["subagentInvocationId"]
    )
    assert "relatedGraphNamespace" not in result.raw_event


def test_native_task_starts_reject_scoped_tool_id_reuse_atomically() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    adapter.process(
        _native_tool_start(
            graph_task_id="native-first",
            tool_name="read_file",
            tool_call_id="call-shared",
        )
    )

    with pytest.raises(ValueError, match="duplicate native tool call ID"):
        adapter.process(
            _native_tool_start(
                graph_task_id="native-second",
                tool_name="write_file",
                tool_call_id="call-shared",
            )
        )

    events = adapter.process(_tool_result(tool_call_id="call-shared", name=None))
    assert [event.type.value for event in events] == [
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
    ]


def test_native_task_start_rejects_duplicate_same_name_ids_atomically() -> None:
    adapter = DeepAgentAgUiAdapter(identity=_identity())
    duplicate = _native_tool_start(
        graph_task_id="native-duplicate",
        tool_name="read_file",
        tool_call_id="call-duplicate",
    )
    data = duplicate["data"]
    assert isinstance(data, dict)
    tool_calls = data["input"]
    assert isinstance(tool_calls, list)
    second = dict(tool_calls[0])
    second["args"] = {"path": "second.txt"}
    tool_calls.append(second)

    with pytest.raises(ValueError, match="duplicate native tool call ID"):
        adapter.process(duplicate)

    retry = adapter.process(
        _native_tool_start(
            graph_task_id="native-retry",
            tool_name="read_file",
            tool_call_id="call-duplicate",
        )
    )
    assert [event.type.value for event in retry] == ["RAW"]


@pytest.mark.asyncio
async def test_task_error_does_not_create_a_second_main_terminal() -> None:
    async def parts():
        yield _task(phase="start", secret="start")
        yield _task(phase="result", secret="result")

    events = [
        event async for event in astream_events(parts=parts(), identity=_identity())
    ]

    assert len([event for event in events if isinstance(event, RunStartedEvent)]) == 1
    assert len([event for event in events if isinstance(event, RunFinishedEvent)]) == 1
    assert not any(isinstance(event, RunErrorEvent) for event in events)
    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "RAW",
        "RAW",
        "RUN_FINISHED",
    ]
    for event in events:
        type(event).model_validate(
            json.loads(event.model_dump_json(by_alias=True, exclude_none=True))
        )
