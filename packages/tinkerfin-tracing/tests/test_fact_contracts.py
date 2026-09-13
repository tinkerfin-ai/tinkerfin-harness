"""Strict semantic fact contracts accepted by custom Trace Store implementations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CanonicalTracePayloadCodec,
    CapturedValue,
    ContextContributionFact,
    InteractionFact,
    ModelCallFact,
    RunFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
)

NOW = datetime.now(UTC)


def _common() -> dict[str, object]:
    return {
        "sourceObservationId": "observation-1",
        "identity": RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1"),
        "occurredAt": NOW,
        "monotonicNs": 1,
    }


def test_run_fact_requires_phase_specific_lifecycle_evidence() -> None:
    with pytest.raises(ValidationError, match="require input_kind"):
        RunFact.model_validate({**_common(), "phase": "started"})
    with pytest.raises(ValidationError, match="require outcome"):
        RunFact.model_validate({**_common(), "phase": "terminal"})
    with pytest.raises(ValidationError, match="require interrupt_ids"):
        RunFact.model_validate({**_common(), "phase": "resume_checkpointed"})


def test_tool_result_and_interaction_phase_cannot_be_ambiguous() -> None:
    with pytest.raises(ValidationError, match="content and result_status"):
        ToolFact.model_validate(
            {
                **_common(),
                "phase": "result",
                "toolCallId": "tool:call-1",
                "sourceToolCallId": "call-1",
                "toolName": "search",
            }
        )
    with pytest.raises(ValidationError, match="must be pending"):
        InteractionFact.model_validate(
            {
                **_common(),
                "phase": "opened",
                "interactionId": "interaction:1",
                "sourceInteractionId": "1",
                "interactionKind": "approval",
                "status": "resolved",
            }
        )

    result = ToolFact.model_validate(
        {
            **_common(),
            "phase": "result",
            "toolCallId": "tool:call-1",
            "sourceToolCallId": "call-1",
            "toolName": "search",
            "content": CapturedValue(
                disposition="inline",
                safe_size_bytes=4,
                value=None,
            ),
            "resultStatus": "success",
        }
    )
    assert result.content is not None
    assert result.content.value is None


def test_failure_origin_serialization_is_stable_for_all_failure_fact_kinds() -> None:
    codec = CanonicalTracePayloadCodec()
    facts = (
        RunFact.model_validate(
            {
                **_common(),
                "phase": "terminal",
                "outcome": "failed",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ToolFact.model_validate(
            {
                **_common(),
                "phase": "result",
                "toolCallId": "tool:call-1",
                "sourceToolCallId": "call-1",
                "toolName": "search",
                "content": CapturedValue(
                    disposition="inline",
                    safe_size_bytes=4,
                    value=None,
                ),
                "resultStatus": "error",
            }
        ),
        ModelCallFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "callId": "model:1",
                "systemMessagePositions": (),
                "outputMessageIds": (),
                "errorType": "builtins.RuntimeError",
            }
        ),
        ToolExecutionFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "executionId": "tool-execution:1",
                "toolName": "search",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ContextContributionFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "contributionId": "context:1",
                "contextKind": "retrieval",
                "name": "knowledge-base",
                "errorType": "builtins.RuntimeError",
            }
        ),
    )

    for fact in facts:
        origin = fact.model_copy(update={"failure_origin": True})
        fallback_payload = codec.encode_fact(fact).data
        origin_payload = codec.encode_fact(origin).data

        assert b'"failureOrigin"' not in fallback_payload
        assert b'"failureOrigin":true' in origin_payload
        assert codec.decode_fact(fallback_payload) == fact
        assert codec.decode_fact(origin_payload) == origin


def test_model_call_fact_requires_current_message_link_evidence() -> None:
    with pytest.raises(ValidationError, match="systemMessagePositions"):
        ModelCallFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "callId": "model:1",
                "errorType": "builtins.RuntimeError",
            }
        )
    with pytest.raises(ValidationError, match="unique and ordered"):
        ModelCallFact.model_validate(
            {
                **_common(),
                "phase": "started",
                "callId": "model:1",
                "contextStartedAt": NOW,
                "request": CapturedValue(
                    disposition="inline",
                    safe_size_bytes=2,
                    value={},
                ),
                "systemMessagePositions": (2, 1),
                "outputMessageIds": (),
            }
        )
    completed = ModelCallFact.model_validate(
        {
            **_common(),
            "phase": "completed",
            "callId": "model:1",
            "systemMessagePositions": (),
            "outputMessageIds": ("assistant-1",),
        }
    )
    assert completed.output_message_ids == ("assistant-1",)


def test_subagent_scope_evidence_is_explicit_and_omitted_at_root() -> None:
    codec = CanonicalTracePayloadCodec()
    root = ModelCallFact.model_validate(
        {
            **_common(),
            "phase": "started",
            "callId": "model:root",
            "contextStartedAt": NOW,
            "request": CapturedValue(
                disposition="inline",
                safe_size_bytes=2,
                value={},
            ),
            "systemMessagePositions": (),
            "outputMessageIds": (),
        }
    )
    child = root.model_copy(
        update={
            "call_id": "model:child",
            "graph_namespace": ("tools:child",),
            "in_subagent_scope": True,
        }
    )

    root_payload = codec.encode_fact(root).data
    child_payload = codec.encode_fact(child).data

    assert b'"inSubagentScope"' not in root_payload
    assert b'"inSubagentScope":true' in child_payload
    assert codec.decode_fact(root_payload) == root
    assert codec.decode_fact(child_payload) == child


@pytest.mark.parametrize(
    ("phase", "status"),
    [
        ("started", "succeeded"),
        ("updated", "running"),
        ("completed", "waiting"),
    ],
)
def test_subagent_status_matches_its_lifecycle_phase(
    phase: str,
    status: str,
) -> None:
    with pytest.raises(ValidationError, match="lifecycle phase"):
        SubagentFact.model_validate(
            {
                **_common(),
                "graph_namespace": ("tools:child",),
                "phase": phase,
                "subagentId": "subagent:child",
                "status": status,
            }
        )


def test_subagent_relationship_evidence_belongs_only_to_its_start() -> None:
    with pytest.raises(ValidationError, match="opening evidence"):
        SubagentFact.model_validate(
            {
                **_common(),
                "graph_namespace": ("tools:child",),
                "phase": "updated",
                "subagentId": "subagent:child",
                "parentToolCallId": "call-task",
                "status": "waiting",
            }
        )

    started = SubagentFact.model_validate(
        {
            **_common(),
            "graph_namespace": ("tools:child",),
            "phase": "started",
            "subagentId": "subagent:child",
            "parentToolCallId": "call-task",
            "modelCallId": "model:root",
            "input": CapturedValue(
                disposition="inline",
                safe_size_bytes=2,
                value={},
            ),
            "status": "running",
        }
    )
    assert started.parent_tool_call_id == "call-task"


def test_failure_origin_requires_a_failed_terminal_run() -> None:
    with pytest.raises(ValidationError, match="failed terminal"):
        RunFact.model_validate(
            {
                **_common(),
                "phase": "closed",
                "outcome": "failed",
                "errorType": "builtins.RuntimeError",
                "failureOrigin": True,
            }
        )


@pytest.mark.parametrize(
    "phase_fields",
    (
        {"phase": "started", "input_kind": "ordinary"},
        {"phase": "input", "input_kind": "ordinary"},
        {"phase": "resumed", "input_kind": "resume"},
        {"phase": "resumed", "input_kind": "continuation"},
        {"phase": "resume_checkpointed", "interrupt_ids": ("approval",)},
        {
            "phase": "observer_failed",
            "observer_name": "observer",
            "error_type": "ValueError",
        },
        {"phase": "terminal", "outcome": "cancelled"},
        {"phase": "closed", "outcome": "cancelled"},
    ),
)
@pytest.mark.parametrize(
    "scope", ({"graph_namespace": ("child",)}, {"in_subagent_scope": True})
)
def test_all_run_lifecycle_phases_are_root_scoped(
    phase_fields: dict[str, object],
    scope: dict[str, object],
) -> None:
    fields = {**_common(), **phase_fields}
    if phase_fields["phase"] in {"input", "resumed"}:
        fields.update(
            input=CapturedValue(disposition="inline", safe_size_bytes=4),
            config=CapturedValue(disposition="inline", safe_size_bytes=4),
        )
    valid = RunFact.model_validate(fields)
    assert valid.graph_namespace == () and valid.in_subagent_scope is False
    with pytest.raises(ValidationError, match="root scope"):
        RunFact.model_validate({**fields, **scope})
