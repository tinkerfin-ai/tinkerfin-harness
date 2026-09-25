"""Public contracts for stable identity-free AG-UI resume bindings."""

from __future__ import annotations

import pytest
from ag_ui.core.types import Interrupt, ResumeEntry
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tinkerfin import (
    AgUiResumeBinding,
    AgUiResumeBindingError,
    AgUiResumeRequest,
)
from tinkerfin_agui_adapter import ResumeMappingError, ResumeTranslation, ScopedIdCodec
from tinkerfin_native_stream import NativeRuntimeInterrupt


def _interrupts() -> tuple[Interrupt, ...]:
    native_value = {
        "action_requests": [
            {"name": "write_file", "args": {"path": "a"}},
            {"name": "write_file", "args": {"path": "b"}},
        ],
        "review_configs": [
            {"action_name": "write_file", "allowed_decisions": ["approve"]},
            {"action_name": "write_file", "allowed_decisions": ["approve"]},
        ],
    }
    codec = ScopedIdCodec()
    return tuple(
        Interrupt(
            id=f"interrupt-1#{index}",
            reason="tool_call",
            tool_call_id=codec.encode("tool", (), f"call-{index}"),
            metadata={
                "langgraphValue": native_value,
                "deepagents": {
                    "schema": "tinkerfin.deepagents.tool-review",
                    "nativeInterruptId": "interrupt-1",
                    "actionIndex": index,
                    "toolName": "write_file",
                    "allowedDecisions": ["approve"],
                    "originalArgs": {"path": "a" if index == 0 else "b"},
                },
            },
        )
        for index in range(2)
    )


def _entry(
    interrupt_id: str,
    *,
    status: str = "resolved",
) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": status,
            **({"payload": {"type": "approve"}} if status == "resolved" else {}),
        }
    )


def _resolved_binding() -> AgUiResumeBinding:
    return AgUiResumeBinding.from_agui(
        entries=(_entry("interrupt-1#0"), _entry("interrupt-1#1")),
        interrupts=_interrupts(),
    )


def test_from_agui_builds_one_identity_free_resume_binding() -> None:
    binding = _resolved_binding()

    assert binding.mode == "resume"
    assert binding.resume_data == {
        "decisions": [{"type": "approve"}, {"type": "approve"}]
    }
    assert binding.native_interrupt_ids == ("interrupt-1",)
    assert binding.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-0"),
        ScopedIdCodec().encode("tool", (), "call-1"),
    )
    assert binding.contains_cancellations is False
    assert not hasattr(binding, "identity")
    assert not hasattr(binding, "command")


def test_resume_request_builds_binding_from_native_checkpoint_evidence() -> None:
    request = AgUiResumeRequest(
        entries=(_entry("interrupt-1#1"), _entry("interrupt-1#0")),
    )
    message = AIMessage(
        id="message-review",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "a"},
                "id": "call-0",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"path": "b"},
                "id": "call-1",
                "type": "tool_call",
            },
        ],
    )
    binding = AgUiResumeBinding.from_native(
        request=request,
        interrupts=(
            NativeRuntimeInterrupt(
                id="interrupt-1",
                value={
                    "action_requests": [
                        {"name": "write_file", "args": {"path": "a"}},
                        {"name": "write_file", "args": {"path": "b"}},
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
            ),
        ),
        messages_by_graph_namespace={(): (message,)},
    )

    assert binding.native_interrupt_ids == ("interrupt-1",)
    assert binding.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-0"),
        ScopedIdCodec().encode("tool", (), "call-1"),
    )
    assert binding.resume_data == {
        "decisions": [{"type": "approve"}, {"type": "approve"}]
    }


def test_resume_request_rejects_duplicate_client_interrupt_ids() -> None:
    with pytest.raises(ValidationError, match="unique interrupt IDs"):
        AgUiResumeRequest(entries=(_entry("interrupt-1"), _entry("interrupt-1")))


def test_from_agui_converts_adapter_failures_to_the_core_error_family() -> None:
    with pytest.raises(AgUiResumeBindingError) as raised:
        AgUiResumeBinding.from_agui(
            entries=(_entry("unknown"),),
            interrupts=_interrupts(),
        )

    assert isinstance(raised.value.cause, ResumeMappingError)
    adapter_code = raised.value.context["adapter_code"]
    assert isinstance(adapter_code, str)
    assert adapter_code.startswith("agui.resume.")


def test_binding_has_a_strict_stable_json_round_trip() -> None:
    binding = _resolved_binding()
    payload = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload == {
        "mode": "resume",
        "resumeData": {"decisions": [{"type": "approve"}, {"type": "approve"}]},
        "nativeInterruptIds": ["interrupt-1"],
        "priorToolCallIds": [
            ScopedIdCodec().encode("tool", (), "call-0"),
            ScopedIdCodec().encode("tool", (), "call-1"),
        ],
        "sourceAgentNames": [],
        "unidentifiedExternalSource": False,
    }
    assert AgUiResumeBinding.model_validate(payload) == binding


def test_binding_does_not_expose_mutable_native_resume_data() -> None:
    binding = _resolved_binding()
    exposed = binding.resume_data
    assert isinstance(exposed, dict)
    exposed["decisions"] = []

    assert binding.resume_data == {
        "decisions": [{"type": "approve"}, {"type": "approve"}]
    }


def test_mixed_resume_preserves_cancelled_slots_without_fabricating_reject() -> None:
    binding = AgUiResumeBinding.from_agui(
        entries=(
            _entry("interrupt-1#1", status="cancelled"),
            _entry("interrupt-1#0"),
        ),
        interrupts=_interrupts(),
    )

    assert binding.mode == "resume"
    assert binding.contains_cancellations is True
    assert binding.resume_data == {
        "decisions": [
            {"type": "approve"},
            {"type": "tinkerfin_cancel"},
        ]
    }
    summaries = binding._observation_summaries()
    assert summaries[0].status == "cancelled"
    assert summaries[0].decision is None


def test_single_decision_observation_summary_preserves_the_public_action() -> None:
    binding = AgUiResumeBinding(
        mode="resume",
        resume_data={"decisions": [{"type": "approve"}]},
        native_interrupt_ids=("interrupt-1",),
    )

    summary = binding._observation_summaries()[0]

    assert summary.status == "resolved"
    assert summary.decision == "approve"


def test_all_cancelled_resume_is_a_round_trippable_abandonment() -> None:
    binding = AgUiResumeBinding.from_agui(
        entries=(
            _entry("interrupt-1#0", status="cancelled"),
            _entry("interrupt-1#1", status="cancelled"),
        ),
        interrupts=_interrupts(),
    )
    payload = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert binding.mode == "abandon"
    assert binding.contains_cancellations is True
    assert binding.resume_data is None
    assert "resumeData" not in payload
    assert AgUiResumeBinding.model_validate(payload) == binding


def test_binding_rejects_mixed_generic_runtime_cancellation() -> None:
    translation = ResumeTranslation(
        mode="custom",
        kind="runtime",
        resume_data=None,
        cancelled_interrupt_ids=("runtime-2",),
        decisions_by_interrupt={
            "runtime-1": ({"answer": "yes"},),
            "runtime-2": (None,),
        },
    )

    with pytest.raises(ValueError, match="runtime interrupts"):
        AgUiResumeBinding._from_translation(translation)


def test_binding_rejects_invalid_persisted_shapes() -> None:
    with pytest.raises(ValidationError, match="requires resumeData"):
        AgUiResumeBinding.model_validate(
            {
                "mode": "resume",
                "nativeInterruptIds": ["interrupt-1"],
            }
        )
    with pytest.raises(ValidationError, match="cannot include resumeData"):
        AgUiResumeBinding.model_validate(
            {
                "mode": "abandon",
                "resumeData": {},
                "nativeInterruptIds": ["interrupt-1"],
            }
        )
    with pytest.raises(ValidationError, match="complete scoped Tool IDs"):
        AgUiResumeBinding.model_validate(
            {
                "mode": "resume",
                "resumeData": {"decisions": []},
                "nativeInterruptIds": ["interrupt-1"],
                "priorToolCallIds": ["call-1"],
            }
        )
