"""Canonical file review retains the exact original tool proposal."""

import pytest
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage

from tinkerfin_agui_adapter import HitlCorrelationError, ResumeMapper
from tinkerfin_agui_adapter.hitl import HitlRequest, match_hitl_tool_call_id_groups
from tinkerfin_agui_adapter.ids import ScopedIdCodec
from tinkerfin_agui_adapter.interrupt_projection import project_interrupt
from tinkerfin_agui_adapter.models import AgentRuntimeInterrupt


def test_prepared_file_review_keeps_exact_proposal_correlation_and_effective_arguments() -> (
    None
):
    original = {"file_path": "report.txt", "content": "report"}
    effective = {**original, "file_path": "/project/report.txt"}
    native = AgentRuntimeInterrupt.model_validate(
        {
            "id": "review",
            "value": {
                "action_requests": [
                    {"name": "write_file", "args": effective, "source_args": original}
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "edit", "reject"],
                    }
                ],
            },
        }
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"id": "write", "name": "write_file", "args": original}],
        )
    ]
    actions = HitlRequest.model_validate(native.value).action_requests
    groups = match_hitl_tool_call_id_groups([actions], messages)
    assert groups == (("write",),)
    projected = project_interrupt(
        native,
        tool_call_ids=(ScopedIdCodec().encode("tool", (), "write"),),
        source={"kind": "root", "graphNamespace": []},
    )
    metadata = projected[0].metadata
    assert metadata is not None
    deepagents = metadata["deepagents"]
    assert isinstance(deepagents, dict)
    assert deepagents["originalArgs"] == effective
    langgraph_value = metadata["langgraphValue"]
    assert isinstance(langgraph_value, dict)
    action_requests = langgraph_value["action_requests"]
    assert isinstance(action_requests, list)
    action_request = action_requests[0]
    assert isinstance(action_request, dict)
    assert action_request["source_args"] == original
    entries = (
        ResumeEntry(
            interrupt_id="review", status="resolved", payload={"type": "approve"}
        ),
    )
    assert ResumeMapper().map(
        entries=entries,
        interrupts=(native,),
        messages_by_graph_namespace={(): messages},
    ) == ResumeMapper().map_agui(entries=entries, interrupts=projected)
    changed = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "write",
                    "name": "write_file",
                    "args": {**original, "file_path": "other.txt"},
                }
            ],
        )
    ]
    with pytest.raises(HitlCorrelationError):
        match_hitl_tool_call_id_groups([actions], changed)
