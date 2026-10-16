"""Locked parent delegation for TinkerFin's narrow HITL extension."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain.agents.middleware.human_in_the_loop import Decision
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolCall

from tinkerfin._hitl import (
    CANCEL_DECISION_TYPE,
    HITL_CONTRACT_ID,
    ToolReviewMiddleware,
    _permission_interrupts,
)


def _tool_call() -> ToolCall:
    return ToolCall(
        type="tool_call",
        name="write_file",
        args={"file_path": "/result.txt", "content": "ok"},
        id="call-1",
    )


@pytest.mark.parametrize(
    "decision",
    [
        {"type": "approve"},
        {
            "type": "edit",
            "edited_action": {
                "name": "write_file",
                "args": {"file_path": "/edited.txt", "content": "edited"},
            },
        },
        {"type": "reject", "message": "stop"},
        {"type": "respond", "message": "human response"},
    ],
)
def test_standard_decisions_delegate_exactly_to_locked_parent(
    decision: dict[str, Any],
) -> None:
    config = InterruptOnConfig(
        allowed_decisions=["approve", "edit", "reject", "respond"]
    )
    typed = cast(Decision, decision)

    expected = HumanInTheLoopMiddleware._process_decision(
        typed,
        _tool_call(),
        config,
    )
    actual = ToolReviewMiddleware._process_decision(
        typed,
        _tool_call(),
        config,
    )

    assert actual == expected


def test_internal_cancel_is_non_executable_deterministic_and_auditable() -> None:
    config = InterruptOnConfig(allowed_decisions=["approve"])
    decision = cast(Decision, {"type": CANCEL_DECISION_TYPE})

    first_call, first_message = ToolReviewMiddleware._process_decision(
        decision,
        _tool_call(),
        config,
    )
    second_call, second_message = ToolReviewMiddleware._process_decision(
        decision,
        _tool_call(),
        config,
    )

    assert first_call is None
    assert second_call is None
    assert first_message == second_message
    assert first_message is not None
    assert first_message.status == "error"
    assert first_message.tool_call_id == "call-1"
    assert HITL_CONTRACT_ID == "tinkerfin.deepagents.hitl-cancel"
    assert first_message.additional_kwargs["tinkerfin"] == {
        "schema": HITL_CONTRACT_ID,
        "outcome": "cancelled",
        "executed": False,
    }


def _when(
    configs: dict[str, InterruptOnConfig],
    tool_name: str,
) -> Callable[[ToolCallRequest], bool]:
    value = configs[tool_name].get("when")
    assert callable(value)
    return value


def _request(tool_name: str, args: dict[str, object]) -> ToolCallRequest:
    return cast(
        ToolCallRequest,
        SimpleNamespace(tool_call={"name": tool_name, "args": args, "id": "call"}),
    )


def test_permission_adapter_preserves_exact_rule_precedence() -> None:
    configs = _permission_interrupts(
        (
            FilesystemPermission(
                operations=["write"],
                paths=["/secrets/private/**"],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=["/secrets/**"],
                mode="interrupt",
            ),
        )
    )
    when = _when(configs, "write_file")

    assert when(_request("write_file", {"file_path": "/secrets/public/a.txt"}))
    assert not when(_request("write_file", {"file_path": "/secrets/private/a.txt"}))
    assert not when(_request("write_file", {"file_path": "../secrets/a.txt"}))


def test_permission_adapter_preserves_bulk_overlap_and_glob_redirects() -> None:
    configs = _permission_interrupts(
        (
            FilesystemPermission(
                operations=["read"],
                paths=["/secrets/**"],
                mode="interrupt",
            ),
        )
    )

    assert _when(configs, "grep")(_request("grep", {"path": None}))
    assert _when(configs, "ls")(_request("ls", {"path": "/"}))
    assert not _when(configs, "ls")(_request("ls", {"path": "/workspace"}))
    assert _when(configs, "glob")(
        _request(
            "glob",
            {"path": "/workspace", "pattern": "/secrets/**"},
        )
    )
