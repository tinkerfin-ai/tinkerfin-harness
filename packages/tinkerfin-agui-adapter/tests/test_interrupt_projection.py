"""Resolved native review projection shares the public live interrupt contract."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import JsonValue

from tinkerfin_agui_adapter import (
    AgentRuntimeInterrupt,
    HitlCorrelationError,
    ScopedIdCodec,
    parse_tool_review_interrupt,
    project_interrupt,
)


def _review() -> AgentRuntimeInterrupt:
    return AgentRuntimeInterrupt(
        id="group:#original",
        value={
            "action_requests": [
                {"name": "save", "args": {"path": "报告.md"}},
                {"name": "deliver", "args": {}},
            ],
            "review_configs": [
                {
                    "action_name": "save",
                    "allowed_decisions": ["approve", "edit", "reject"],
                },
                {"action_name": "deliver", "allowed_decisions": ["approve", "respond"]},
            ],
        },
    )


def test_public_projection_keeps_native_payload_and_positional_decisions() -> None:
    request = _review()
    original = deepcopy(request.value)
    ids = [
        ScopedIdCodec().encode("tool", ("tools:child",), name) for name in ("a", "b")
    ]
    result = project_interrupt(request, tool_call_ids=ids)
    assert [item.id for item in result] == ["group:#original#0", "group:#original#1"]
    assert [item.tool_call_id for item in result] == ids
    assert [parse_tool_review_interrupt(item).action_index for item in result] == [0, 1]
    assert parse_tool_review_interrupt(result[0]).original_args.root == {
        "path": "报告.md"
    }
    assert request.value == original


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["raw", "raw"],
        ["raw-a", "raw-b"],
        [
            ScopedIdCodec().encode("tool", (), "a"),
            ScopedIdCodec().encode("tool", ("tools:child",), "b"),
        ],
    ],
)
def test_public_projection_rejects_incomplete_or_cross_scope_tool_assignment(
    ids: list[str],
) -> None:
    with pytest.raises(HitlCorrelationError):
        project_interrupt(_review(), tool_call_ids=ids)


def test_unstructured_interrupt_keeps_null_payload_without_tool_identity() -> None:
    result = project_interrupt(AgentRuntimeInterrupt(id="pause", value=None))
    assert len(result) == 1
    assert result[0].id == "pause" and result[0].reason == "langgraph:interrupt"
    assert result[0].tool_call_id is None
    assert result[0].metadata == {"langgraphValue": None, "source": {}}


@pytest.mark.parametrize(
    "source_namespace", [[], ["tools:other"], "tools:child", [1], [""]]
)
def test_tool_review_rejects_inconsistent_or_invalid_source_scope(source_namespace):
    ids = [
        ScopedIdCodec().encode("tool", ("tools:child",), name) for name in ("a", "b")
    ]
    with pytest.raises(HitlCorrelationError):
        project_interrupt(
            _review(), tool_call_ids=ids, source={"graphNamespace": source_namespace}
        )


def test_tool_review_keeps_matching_source_without_mutation():
    ids = [
        ScopedIdCodec().encode("tool", ("tools:child",), name) for name in ("a", "b")
    ]
    source: dict[str, JsonValue] = {"graphNamespace": ["tools:child"]}
    expected = deepcopy(source)
    result = project_interrupt(_review(), tool_call_ids=ids, source=source)
    assert source == expected
    assert all(
        item.metadata is not None and item.metadata["source"] == expected
        for item in result
    )
