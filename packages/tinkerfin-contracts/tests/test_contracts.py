"""Public contract validation and structural observer boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    RunIdentity,
    RunInputObservation,
    RunResumeSummary,
    RunSourceContext,
)


def _context() -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={"configurable": {"thread_id": "thread-1"}},
    )


def test_run_identity_is_strict_frozen_and_uses_current_aliases() -> None:
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")

    assert identity.model_dump(by_alias=True) == {
        "namespace": "test",
        "threadId": "thread-1",
        "runId": "run-1",
    }
    with pytest.raises(ValidationError):
        RunIdentity(namespace="test", thread_id=" thread-1", run_id="run-1")
    with pytest.raises(ValidationError):
        RunIdentity.model_validate(
            {
                "namespace": "test",
                "threadId": "thread-1",
                "runId": "run-1",
                "version": 1,
            }
        )
    with pytest.raises(ValidationError):
        identity.thread_id = "replacement"  # type: ignore[misc]
    assert (
        RunIdentity(namespace="test", thread_id="t" * 1024, run_id="r" * 1024).run_id
        == "r" * 1024
    )
    with pytest.raises(ValidationError):
        RunIdentity(namespace="test", thread_id="t" * 1025, run_id="run-1")
    with pytest.raises(ValidationError):
        RunIdentity(namespace="test", thread_id="thread-1", run_id="r" * 1025)


def test_native_message_contract_preserves_structured_public_content() -> None:
    observation = NativeMessageObservation(
        identity=_context().identity,
        graph_namespace=("tools:task-1",),
        message=NativeMessageRecord(
            message_type="assistant_chunk",
            id="message-1",
            content=[{"type": "text", "text": "visible"}],
        ),
        metadata={"langgraph_node": "model"},
        observed_at=datetime.now(UTC),
        monotonic_ns=2,
    )

    assert observation.graph_namespace == ("tools:task-1",)
    assert observation.message.content == [{"type": "text", "text": "visible"}]


def test_resume_and_run_input_contracts_reject_ambiguous_identity() -> None:
    with pytest.raises(ValidationError, match="cannot contain a decision"):
        RunResumeSummary(
            interrupt_id="interrupt-1",
            status="cancelled",
            decision="reject",
        )
    with pytest.raises(ValidationError, match="interrupt IDs must be unique"):
        RunSourceContext(
            identity=_context().identity,
            runtime_profile="deepagents-v2",
            input_kind="resume",
            input={},
            config={},
            resume=(
                RunResumeSummary(interrupt_id="interrupt-1", status="resolved"),
                RunResumeSummary(interrupt_id="interrupt-1", status="resolved"),
            ),
        )
    with pytest.raises(ValidationError, match="identity must match"):
        RunInputObservation(
            identity=RunIdentity(
                namespace="test", thread_id="thread-1", run_id="different-run"
            ),
            source=_context(),
            observed_at=datetime.now(UTC),
            monotonic_ns=1,
        )
