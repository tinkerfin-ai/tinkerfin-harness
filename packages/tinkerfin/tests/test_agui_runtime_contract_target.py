"""Target public contract for the simplified AG-UI Runtime boundary."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields

import pytest
from pydantic import BaseModel

import tinkerfin
import tinkerfin_agui_adapter
from tinkerfin import (
    AgentRuntime,
    AgUiResumeBinding,
    AgUiResumeReceipt,
    AgUiResumeResponse,
    RunIdentity,
)


def test_target_public_surface_exposes_saved_resume_receipts() -> None:
    """Expose one canonical identity and saved public response summaries."""

    assert "AgUiRunContext" not in tinkerfin_agui_adapter.__all__
    assert not hasattr(tinkerfin_agui_adapter, "AgUiRunContext")
    assert "AgUiResumeSettlement" not in tinkerfin.__all__
    assert "AgUiResumeSettlementObserver" not in tinkerfin.__all__
    assert "AgUiResumeCheckpoint" not in tinkerfin.__all__
    assert not hasattr(tinkerfin, "AgUiResumeCheckpoint")
    assert "AgUiResumeCheckpointObserver" not in tinkerfin.__all__
    assert not hasattr(tinkerfin, "AgUiResumeCheckpointObserver")
    assert "AgUiResumeReceipt" in tinkerfin.__all__
    assert "AgUiResumeReceiptObserver" in tinkerfin.__all__
    assert "AgUiResumeResponse" in tinkerfin.__all__
    assert "AgUiResumeNotSavedObserver" in tinkerfin.__all__
    assert "AgentRuntime" in tinkerfin.__all__
    assert AgUiResumeReceipt is not None
    assert AgentRuntime is not None


def test_saved_receipt_contains_immutable_public_response_summaries() -> None:
    """A receipt contains only bound identity, an opaque ID, and public responses."""

    response = AgUiResumeResponse(interrupt_id="public-interrupt", status="resolved")
    receipt = AgUiResumeReceipt(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run"),
        parent_run_id="parent",
        receipt_id="opaque-receipt",
        responses=(response,),
    )

    assert {field.name for field in fields(receipt)} == {
        "identity",
        "parent_run_id",
        "receipt_id",
        "responses",
    }
    assert {field.name for field in fields(response)} == {"interrupt_id", "status"}
    assert isinstance(receipt.responses, tuple)
    with pytest.raises(FrozenInstanceError):
        setattr(receipt, "receipt_id", "changed")
    with pytest.raises(FrozenInstanceError):
        setattr(response, "status", "cancelled")


def test_execution_signature_has_no_protocol_input_or_settlement_callback() -> None:
    """Keep transport DTOs and ambiguous settlement wording out of the Runtime API."""

    parameters = inspect.signature(AgentRuntime.open_agui_run).parameters

    assert "thread_id" in parameters
    assert "run_id" in parameters
    assert "parent_run_id" in parameters
    assert "resume" in parameters
    assert "on_resume_saved" in parameters
    assert "on_resume_not_saved" in parameters
    assert "run_input" not in parameters
    assert "on_resume_settled" not in parameters


def test_resume_binding_owns_a_strict_stable_persistence_round_trip() -> None:
    """Round-trip resume facts without identity, parent, or a public Command."""

    assert issubclass(AgUiResumeBinding, BaseModel)
    binding = AgUiResumeBinding.model_validate(
        {
            "mode": "resume",
            "resumeData": {"decisions": [{"type": "approve"}]},
            "nativeInterruptIds": ["interrupt-1"],
            "priorToolCallIds": [],
            "sourceAgentNames": [],
            "unidentifiedExternalSource": False,
        }
    )
    persisted = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert AgUiResumeBinding.model_validate(persisted) == binding
    assert not hasattr(binding, "identity")
    assert not hasattr(binding, "command")
    assert binding.contains_cancellations is False


def test_resume_binding_round_trips_all_cancelled_abandonment() -> None:
    """Represent all-cancelled input without a fabricated native rejection."""

    binding = AgUiResumeBinding.model_validate(
        {
            "mode": "abandon",
            "nativeInterruptIds": ["interrupt-1"],
            "priorToolCallIds": [],
            "sourceAgentNames": [],
            "unidentifiedExternalSource": False,
        }
    )

    assert binding.mode == "abandon"
    assert binding.contains_cancellations is True
    assert "resumeData" not in binding.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
