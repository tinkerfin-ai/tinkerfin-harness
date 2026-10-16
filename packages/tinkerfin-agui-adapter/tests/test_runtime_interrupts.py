"""Generic runtime interrupt conversion and resume contracts."""

from __future__ import annotations

import pytest
from ag_ui.core import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    ResumeMapper,
    ResumeMappingError,
    RunIdentity,
    RuntimeInterruptEnvelope,
)
from tinkerfin_agui_adapter.models import AgentRuntimeInterrupt
from tinkerfin_agui_adapter.runtime_interrupts import prepare_runtime_ag_ui_interrupt
from tinkerfin_native_stream import (
    RuntimeInterruptEnvelope as NativeRuntimeInterruptEnvelope,
)


def _entry(
    interrupt_id: str,
    *,
    status: str = "resolved",
    payload: object = None,
) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": status,
            "payload": payload,
        }
    )


def _envelope() -> RuntimeInterruptEnvelope:
    return RuntimeInterruptEnvelope(
        kind="tinkerfin:plan_review",
        message="Review Plan",
        response_schema={"type": "object"},
        metadata={"planRevision": 1},
    )


def test_agui_envelope_extends_the_shared_native_contract() -> None:
    assert issubclass(RuntimeInterruptEnvelope, NativeRuntimeInterruptEnvelope)
    assert _envelope().model_dump(mode="json", by_alias=True) == (
        NativeRuntimeInterruptEnvelope.model_validate(
            _envelope().model_dump(mode="json", by_alias=True)
        ).model_dump(mode="json", by_alias=True)
    )


def _native(interrupt_id: str, value: object) -> AgentRuntimeInterrupt:
    return AgentRuntimeInterrupt.model_validate({"id": interrupt_id, "value": value})


def test_runtime_interrupt_maps_to_ag_ui_and_resumes_without_tool_ids() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")
    )
    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [],
                "tinkerfin_plan": {"status": "awaiting_review", "revision": 1},
            },
            "interrupts": (
                {
                    "id": "plan-1",
                    "value": _envelope().model_dump(mode="json", by_alias=True),
                },
            ),
        }
    )
    events.extend(adapter.finish())
    outcome = adapter.main_outcome()

    assert [event.type.value for event in events] == [
        "STATE_SNAPSHOT",
        "MESSAGES_SNAPSHOT",
    ]
    interrupt = outcome.interrupts[0]
    assert interrupt.reason == "tinkerfin:plan_review"
    assert interrupt.tool_call_id is None
    assert interrupt.response_schema == {"type": "object"}
    translation = ResumeMapper().map_agui(
        entries=(
            _entry(
                "plan-1",
                payload={"type": "approve", "baseRevision": 1},
            ),
        ),
        interrupts=outcome.interrupts,
    )
    assert translation.mode == "command"
    assert translation.root == {"type": "approve", "baseRevision": 1}
    assert translation.prior_tool_call_ids == ()


def test_native_runtime_interrupt_resume_uses_the_same_payload() -> None:
    translation = ResumeMapper().map(
        entries=(_entry("plan-1", payload={"type": "respond", "message": "x"}),),
        interrupts=(
            _native(
                "plan-1",
                _envelope().model_dump(mode="json", by_alias=True),
            ),
        ),
    )

    assert translation.root == {"type": "respond", "message": "x"}


def test_runtime_interrupt_rejects_an_invalid_response_schema_before_publication() -> (
    None
):
    with pytest.raises(ValidationError, match="not valid"):
        RuntimeInterruptEnvelope(
            kind="input_required",
            response_schema={"type": "unknown"},
        )


def test_runtime_interrupt_revalidates_and_copies_schema_at_publication() -> None:
    invalid = _envelope()
    invalid.response_schema["type"] = "unknown"
    with pytest.raises(SchemaError):
        prepare_runtime_ag_ui_interrupt(
            _native(
                "plan-1",
                invalid.model_dump(mode="json", by_alias=True),
            ),
            invalid,
            source={},
        )

    envelope = _envelope()
    published = prepare_runtime_ag_ui_interrupt(
        _native(
            "plan-1",
            envelope.model_dump(mode="json", by_alias=True),
        ),
        envelope,
        source={},
    )
    envelope.response_schema["type"] = "array"

    assert published.response_schema == {"type": "object"}


def test_runtime_interrupt_schema_requires_finite_json() -> None:
    with pytest.raises(ValidationError, match="not valid"):
        RuntimeInterruptEnvelope(
            kind="input_required",
            response_schema={"type": "number", "maximum": float("nan")},
        )


def test_runtime_resume_validates_calendar_date_formats() -> None:
    envelope = RuntimeInterruptEnvelope(
        kind="input_required",
        response_schema={
            "type": "object",
            "required": ["date"],
            "additionalProperties": False,
            "properties": {"date": {"type": "string", "format": "date"}},
        },
    )
    interrupt = _native(
        "date-1",
        envelope.model_dump(mode="json", by_alias=True),
    )

    with pytest.raises(ResumeMappingError, match="payload"):
        ResumeMapper().map(
            entries=(_entry("date-1", payload={"date": "2026-02-29"}),),
            interrupts=(interrupt,),
        )
    translation = ResumeMapper().map(
        entries=(_entry("date-1", payload={"date": "2028-02-29"}),),
        interrupts=(interrupt,),
    )
    assert translation.root == {"date": "2028-02-29"}


def test_unknown_runtime_interrupt_contract_fails_closed() -> None:
    value = _envelope().model_dump(mode="json", by_alias=True)
    value["schema"] = "tinkerfin.runtime-interrupt.unsupported"

    with pytest.raises(ResumeMappingError, match="invalid"):
        ResumeMapper().map(
            entries=(),
            interrupts=(_native("plan-1", value),),
        )


def test_persisted_runtime_response_schema_cannot_change_before_resume() -> None:
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")
    )
    adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": []},
            "interrupts": (
                {
                    "id": "plan-1",
                    "value": _envelope().model_dump(mode="json", by_alias=True),
                },
            ),
        }
    )
    interrupt = (
        adapter.main_outcome()
        .interrupts[0]
        .model_copy(update={"response_schema": {"type": "array"}})
    )

    with pytest.raises(ResumeMappingError, match="response schema changed"):
        ResumeMapper().map_agui(
            entries=(_entry("plan-1", payload={"type": "approve"}),),
            interrupts=(interrupt,),
        )


def test_runtime_interrupt_cancellation_is_abandonment() -> None:
    interrupt = AgUiInterrupt(
        id="plan-1",
        reason="tinkerfin:plan_review",
        response_schema={"type": "object"},
        metadata={
            "langgraphValue": _envelope().model_dump(mode="json", by_alias=True),
            "runtimeInterrupt": {
                "schema": "tinkerfin.runtime-interrupt",
                "nativeInterruptId": "plan-1",
                "envelope": _envelope().model_dump(mode="json", by_alias=True),
            },
        },
    )
    translation = ResumeMapper().map_agui(
        entries=(_entry("plan-1", status="cancelled"),),
        interrupts=(interrupt,),
    )

    assert translation.mode == "abandon"
    assert translation.resume_data is None
    assert translation.cancelled_interrupt_ids == ("plan-1",)
    assert translation.decisions_by_interrupt == {"plan-1": (None,)}


def test_mixed_runtime_resume_preserves_resolved_and_cancelled_slots() -> None:
    interrupts = (
        _native(
            "plan-1",
            _envelope().model_dump(mode="json", by_alias=True),
        ),
        _native(
            "plan-2",
            _envelope().model_dump(mode="json", by_alias=True),
        ),
    )

    translation = ResumeMapper().map(
        entries=(
            _entry("plan-2", status="cancelled"),
            _entry("plan-1", payload={"type": "approve"}),
        ),
        interrupts=interrupts,
    )

    assert translation.mode == "custom"
    assert translation.resume_data is None
    assert translation.cancelled_interrupt_ids == ("plan-2",)
    assert translation.decisions_by_interrupt == {
        "plan-1": ({"type": "approve"},),
        "plan-2": (None,),
    }


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ((), "missing"),
        (
            (
                _entry("plan-1", payload={}),
                _entry("plan-1", payload={}),
            ),
            "duplicate",
        ),
        ((_entry("unknown", payload={}),), "unknown"),
    ],
)
def test_runtime_resume_rejects_incomplete_duplicate_and_unknown_entries(
    entries: tuple[ResumeEntry, ...],
    match: str,
) -> None:
    with pytest.raises(ResumeMappingError, match=match):
        ResumeMapper().map(
            entries=entries,
            interrupts=(
                _native(
                    "plan-1",
                    _envelope().model_dump(mode="json", by_alias=True),
                ),
            ),
        )


def test_runtime_and_tool_interrupts_cannot_share_a_resume_batch() -> None:
    with pytest.raises(ResumeMappingError, match="cannot share"):
        ResumeMapper().map(
            entries=(),
            interrupts=(
                _native(
                    "plan-1",
                    _envelope().model_dump(mode="json", by_alias=True),
                ),
                _native(
                    "tool-1",
                    {
                        "action_requests": [{"name": "write_file", "args": {}}],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve"],
                            }
                        ],
                    },
                ),
            ),
        )
