from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_adapter_import_and_minimal_stream_do_not_load_integration_dependencies() -> (
    None
):
    package_root = Path(__file__).parents[1]
    source_root = package_root / "src"
    script = r"""
import asyncio
import builtins
import sys

blocked = {"deepagents", "tinkerfin", "tinkerfin_messaging", "tinkerfin_tracing"}
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".", 1)[0] in blocked:
        raise AssertionError(f"blocked runtime import: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import

from ag_ui.core import RunFinishedEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk
import tinkerfin_agui_adapter

expected_exports = {
    "AttachmentAssistantMessage",
    "AttachmentMessagesSnapshotEvent",
    "AttachmentOutputEvent",
    "AttachmentSnapshotMessage",
    "AttachmentToolCallResultEvent",
    "AttachmentToolMessage",
    "parse_attachment_output_event",
    "AgUiAdapterError", "AgUiAdapterErrorCode", "AgUiConversionError",
    "AgUiLifecycleError", "AgUiSerializationError", "AgUiStreamContractError",
    "AgUiLifecycleEventFactory", "AgentRunOutcome", "AgentRuntimeInterrupt",
    "DeepAgentAgUiAdapter", "HitlActionRequest", "HitlCorrelationError",
    "HitlNoMatchError", "HitlRequest", "HitlReviewConfig", "RunIdentity",
    "ResumeMapper", "ResumeMappingError",
    "ResumeTranslation", "RuntimeInterruptEnvelope", "ScopedIdCodec", "SseEventId",
    "SUBAGENT_PROVENANCE_SCHEMA", "SubagentProvenance",
    "TOOL_REVIEW_SCHEMA", "ToolReviewContractError",
    "ToolReviewDecision", "ToolReviewInterruptMetadata", "astream_events",
    "ValidatedDeepAgentStreamPart", "ValidatedExtraStreamPart",
    "ValidatedMessageStreamPart", "ValidatedTaskResultPayload",
    "ValidatedTaskStartPayload", "ValidatedTasksStreamPart",
    "ValidatedUpdatesStreamPart", "ValidatedValuesStreamPart",
    "create_subagent_provenance", "encode_sse", "micro_batch",
    "parse_tool_review_interrupt", "project_interrupt", "require_valid_schema", "subagent_invocation_id",
    "validate_deep_agent_stream_part", "validate_json_schema_instance",
}
assert set(tinkerfin_agui_adapter.__all__) == expected_exports
for export in expected_exports:
    assert getattr(tinkerfin_agui_adapter, export)

DeepAgentAgUiAdapter = tinkerfin_agui_adapter.DeepAgentAgUiAdapter
ResumeMapper = tinkerfin_agui_adapter.ResumeMapper
astream_events = tinkerfin_agui_adapter.astream_events
encode_sse = tinkerfin_agui_adapter.encode_sse
RunIdentity = tinkerfin_agui_adapter.RunIdentity

async def parts():
    yield {
        "type": "messages",
        "ns": (),
        "data": (
            AIMessageChunk(id="ai-1", content="Hello", chunk_position="last"),
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }
    yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

async def main():
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")
    events = [
        event
        async for event in astream_events(
            parts(), identity=identity
        )
    ]
    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[-1], RunFinishedEvent)
    assert sum(isinstance(event, RunStartedEvent) for event in events) == 1
    assert sum(isinstance(event, RunFinishedEvent) for event in events) == 1
    assert "data:" in encode_sse(events[0])
    assert DeepAgentAgUiAdapter
    assert ResumeMapper

asyncio.run(main())
assert not (blocked & set(sys.modules))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
