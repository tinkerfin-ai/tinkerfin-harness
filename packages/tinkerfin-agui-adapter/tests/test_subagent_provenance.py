"""Public subagent provenance and Adapter event contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ag_ui.core import RawEvent, TextMessageStartEvent, ToolCallResultEvent
from langchain_core.messages import AIMessageChunk, ToolCallChunk, ToolMessage
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    SUBAGENT_PROVENANCE_SCHEMA,
    AgUiStreamContractError,
    DeepAgentAgUiAdapter,
    RunIdentity,
    ScopedIdCodec,
    SubagentProvenance,
    create_subagent_provenance,
    subagent_invocation_id,
)
from tinkerfin_contracts import (
    GraphOrigin,
    GraphTaskReference,
    NativeMessageRecord,
    SubagentRequestReference,
    subagent_request_id,
)
from tinkerfin_native_stream import NativeStreamPart

_PACKAGE_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "failure", ["message_identity", "missing_owner", "conflicting_owner"]
)
def test_rejected_recorded_message_preserves_the_original_delegate(
    failure: str,
) -> None:
    identity = RunIdentity(namespace="test", thread_id="thread", run_id="run")
    adapter = DeepAgentAgUiAdapter(identity=identity)
    adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "parent",
                "name": "tools",
                "triggers": ("__pregel_push",),
                "input": [
                    {
                        "id": "delegate",
                        "name": "task",
                        "type": "tool_call",
                        "args": {"subagent_type": "worker", "description": "Work"},
                    }
                ],
            },
        }
    )
    namespace = ("tools:parent",)
    request = SubagentRequestReference(
        id=subagent_request_id(namespace),
        parent_graph_namespace=(),
        parent_tool_call_id="delegate",
        graph_task_id="parent",
        agent_name="poison",
        description="Work",
    )
    if failure == "conflicting_owner":
        request = request.model_copy(update={"id": "another-delegate"})
    forged = NativeStreamPart(
        type="messages",
        ns=namespace,
        data={
            "message": NativeMessageRecord(
                message_type="assistant_chunk",
                content="rejected",
                id=None if failure == "message_identity" else "probe",
            ).model_dump(mode="json", by_alias=True),
            "metadata": {},
        },
        graphOrigin=GraphOrigin(
            parent_task=GraphTaskReference(
                graph_namespace=(), task_id="parent", node_name="tools"
            ),
            subagent_request=None if failure == "missing_owner" else request,
        ),
    )
    with pytest.raises(AgUiStreamContractError):
        adapter.process(forged)
    events = adapter.process(
        {
            "type": "messages",
            "ns": namespace,
            "data": (
                AIMessageChunk(id="valid", content="accepted", chunk_position="last"),
                {"lc_agent_name": "worker"},
            ),
        }
    )
    source = events[0].raw_event
    assert isinstance(source, dict)
    assert source["source"]["kind"] == "deep_agent_subagent"
    assert source["source"]["agentName"] == "worker"


_REPOSITORY_ROOT = Path(__file__).parents[3]


def _parent_tool_id(namespace: tuple[str, ...] = ("tools:parent",)) -> str:
    return ScopedIdCodec().encode("tool", namespace, "call-task")


def test_subagent_invocation_id_has_a_frozen_known_vector() -> None:
    identity = RunIdentity(
        namespace="test", thread_id="thread-known", run_id="run-known"
    )
    parent_tool_call_id = _parent_tool_id()

    assert parent_tool_call_id == ("tf:tool:W1sidG9vbHM6cGFyZW50Il0sImNhbGwtdGFzayJd")
    assert (
        subagent_invocation_id(
            identity=identity,
            parent_tool_call_id=parent_tool_call_id,
            subagent_id=subagent_request_id(("tools:parent", "tools:graph-task")),
        )
        == "subagent-f40ee085-115c-51b6-b683-45659b5b3639"
    )
    resumed = create_subagent_provenance(
        identity=RunIdentity(
            namespace="test", thread_id="thread-known", run_id="run-resumed"
        ),
        subagent_id=subagent_request_id(("tools:parent", "tools:graph-task")),
        parent_graph_namespace=("tools:parent",),
        agent_name="researcher",
        parent_tool_call_id=parent_tool_call_id,
        description="Research",
    )
    assert resumed.subagent_invocation_id == (
        "subagent-f40ee085-115c-51b6-b683-45659b5b3639"
    )
    assert resumed.request_run_id == "run-resumed"
    assert (
        subagent_invocation_id(
            identity=RunIdentity(
                namespace="test", thread_id="other-thread", run_id="run-known"
            ),
            parent_tool_call_id=parent_tool_call_id,
            subagent_id=subagent_request_id(("tools:parent", "tools:graph-task")),
        )
        != resumed.subagent_invocation_id
    )


def _converted_invocation(
    *,
    run_id: str,
    extra_task_args: dict[str, object] | None = None,
) -> tuple[DeepAgentAgUiAdapter, RawEvent, SubagentProvenance]:
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread-1", run_id=run_id)
    )
    task_args = {
        "description": "Research",
        "subagent_type": "researcher",
        **(extra_task_args or {}),
    }
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-task",
                    content="",
                    tool_call_chunks=[
                        ToolCallChunk(
                            name="task",
                            args=json.dumps(task_args, separators=(",", ":")),
                            id="parent-task",
                            index=0,
                        )
                    ],
                    chunk_position="last",
                ),
                {"langgraph_node": "model"},
            ),
        }
    )
    task_event = adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-task",
                "name": "tools",
                "input": [
                    {
                        "name": "task",
                        "args": task_args,
                        "id": "parent-task",
                        "type": "tool_call",
                    }
                ],
                "triggers": ("branch:to:tools",),
            },
        }
    )[0]
    assert isinstance(task_event, RawEvent)
    provenance = task_event.event["provenance"]
    assert isinstance(provenance, dict)
    descriptors = provenance["subagents"]
    assert isinstance(descriptors, list)
    descriptor = SubagentProvenance.model_validate(descriptors[0])
    return adapter, task_event, descriptor


def test_subagent_provenance_uses_deep_agents_effective_task_arguments() -> None:
    adapter, task_event, descriptor = _converted_invocation(
        run_id="run-extra-task-args",
        extra_task_args={"prompt": "Provider-generated duplicate instructions"},
    )

    assert isinstance(adapter, DeepAgentAgUiAdapter)
    assert descriptor.agent_name == "researcher"
    assert descriptor.description == "Research"
    task_data = task_event.event["data"]
    assert isinstance(task_data, dict)
    task_input = task_data["input"]
    assert isinstance(task_input, list)
    assert task_input[0]["args"]["prompt"] == (
        "Provider-generated duplicate instructions"
    )


def test_adapter_publishes_stable_identity_without_rewriting_main_run() -> None:
    adapter, task_event, descriptor = _converted_invocation(run_id="run-before")
    invocation_id = descriptor.subagent_invocation_id
    parent_tool_call_id = descriptor.parent_tool_call_id
    assert task_event.event["provenance"] is not None
    assert descriptor.schema_id == SUBAGENT_PROVENANCE_SCHEMA
    assert descriptor.request_run_id == "run-before"
    assert descriptor.parent_tool_call_id == parent_tool_call_id

    child_namespace = ("tools:graph-task",)
    child_text = adapter.process(
        {
            "type": "messages",
            "ns": child_namespace,
            "data": (
                AIMessageChunk(
                    id="child-message",
                    content="working",
                    chunk_position="last",
                ),
                {"lc_agent_name": "researcher", "langgraph_node": "model"},
            ),
        }
    )
    started = next(
        event for event in child_text if isinstance(event, TextMessageStartEvent)
    )
    started_raw = started.raw_event
    assert isinstance(started_raw, dict)
    started_source = started_raw["source"]
    assert isinstance(started_source, dict)
    assert started_raw["runId"] == "run-before"
    assert started_source["subagentInvocationId"] == invocation_id
    assert "parentRunId" not in started.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    parent_result = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    "child done",
                    id="parent-result",
                    tool_call_id="parent-task",
                    name="task",
                ),
                {"langgraph_node": "tools"},
            ),
        }
    )
    result = next(
        event for event in parent_result if isinstance(event, ToolCallResultEvent)
    )
    result_raw = result.raw_event
    assert isinstance(result_raw, dict)
    assert result_raw["runId"] == "run-before"
    assert result_raw["relatedSubagentInvocationId"] == invocation_id
    assert "relatedGraphNamespace" not in result_raw

    _, _resumed_task, resumed_descriptor = _converted_invocation(run_id="run-after")
    assert resumed_descriptor.subagent_invocation_id == invocation_id
    assert resumed_descriptor.parent_tool_call_id == parent_tool_call_id
    assert resumed_descriptor.request_run_id == "run-after"


def test_subagent_provenance_is_frozen_and_strict() -> None:
    value = create_subagent_provenance(
        identity=RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1"),
        subagent_id=subagent_request_id(("tools:parent", "tools:graph-task")),
        parent_graph_namespace=("tools:parent",),
        agent_name="researcher",
        parent_tool_call_id=_parent_tool_id(),
        description="Research",
    )
    with pytest.raises(ValidationError):
        setattr(value, "request_run_id", "other")
    with pytest.raises(ValidationError):
        SubagentProvenance.model_validate(
            {
                **value.model_dump(mode="json", by_alias=True),
                "unknown": True,
            }
        )
    with pytest.raises(ValueError, match="scoped Tool"):
        subagent_invocation_id(
            identity=RunIdentity(
                namespace="test", thread_id="thread-1", run_id="run-1"
            ),
            parent_tool_call_id="not-scoped",
            subagent_id=subagent_request_id(("tools:parent", "tools:graph-task")),
        )


def test_python_and_web_subagent_fixtures_are_identical_and_valid() -> None:
    package_path = (
        _PACKAGE_ROOT
        / "src"
        / "tinkerfin_agui_adapter"
        / "contracts"
        / "subagent-provenance.fixture.json"
    )
    web_path = (
        _REPOSITORY_ROOT
        / "apps"
        / "studio"
        / "web"
        / "src"
        / "features"
        / "conversation"
        / "agui"
        / "contracts"
        / "subagent-provenance.fixture.json"
    )
    package_text = package_path.read_text(encoding="utf-8")
    assert web_path.read_text(encoding="utf-8") == package_text
    parsed = SubagentProvenance.model_validate_json(package_text)
    assert parsed.schema_id == SUBAGENT_PROVENANCE_SCHEMA
    assert parsed.model_dump(mode="json", by_alias=True) == json.loads(package_text)
