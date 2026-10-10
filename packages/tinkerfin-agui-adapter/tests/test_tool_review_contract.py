"""Public Tool review metadata contract tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from math import inf
from pathlib import Path
from typing import cast

import pytest
from ag_ui.core.types import Interrupt as AgUiInterrupt

from tinkerfin_agui_adapter import (
    TOOL_REVIEW_SCHEMA,
    ToolReviewContractError,
    ToolReviewInterruptMetadata,
    parse_tool_review_interrupt,
)
from tinkerfin_agui_adapter.ids import ScopedIdCodec

_PACKAGE_ROOT = Path(__file__).parents[1]
_REPOSITORY_ROOT = Path(__file__).parents[3]


def _native_value(*, multiple: bool = False) -> dict[str, object]:
    actions = [
        {
            "name": "write_file",
            "args": {"file_path": "/a.txt", "content": "A"},
        }
    ]
    policies = [
        {
            "action_name": "write_file",
            "allowed_decisions": ["approve", "edit", "reject"],
        }
    ]
    if multiple:
        actions.append(
            {
                "name": "write_file",
                "args": {"file_path": "/b.txt", "content": "B"},
            }
        )
        policies.append(
            {
                "action_name": "write_file",
                "allowed_decisions": ["approve", "reject"],
            }
        )
    return {"action_requests": actions, "review_configs": policies}


def _interrupt(
    *,
    multiple: bool = False,
    action_index: int = 0,
    namespace: tuple[str, ...] = (),
) -> AgUiInterrupt:
    native = _native_value(multiple=multiple)
    actions = cast(list[dict[str, object]], native["action_requests"])
    policies = cast(list[dict[str, object]], native["review_configs"])
    action = actions[action_index]
    policy = policies[action_index]
    tool_name = action["name"]
    original_args = action["args"]
    allowed_decisions = policy["allowed_decisions"]
    assert isinstance(tool_name, str)
    public_id = f"native-review#{action_index}" if multiple else "native-review"
    return AgUiInterrupt(
        id=public_id,
        reason="tool_call",
        message="Review write_file",
        tool_call_id=ScopedIdCodec().encode(
            "tool",
            namespace,
            f"call-{action_index}",
        ),
        metadata={
            "langgraphValue": native,
            "source": {"graphNamespace": list(namespace)},
            "deepagents": {
                "schema": TOOL_REVIEW_SCHEMA,
                "nativeInterruptId": "native-review",
                "actionIndex": action_index,
                "toolName": tool_name,
                "allowedDecisions": allowed_decisions,
                "originalArgs": original_args,
            },
        },
    )


def test_parser_supports_same_name_multi_action_and_subagent_scope() -> None:
    parsed = parse_tool_review_interrupt(
        _interrupt(
            multiple=True,
            action_index=1,
            namespace=("tools:graph-task",),
        )
    )

    assert parsed.action_index == 1
    assert parsed.original_args.root == {
        "file_path": "/b.txt",
        "content": "B",
    }
    assert parsed.allowed_decisions == ("approve", "reject")


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.pop("schema"),
        lambda value: value.__setitem__("schema", "unknown"),
        lambda value: value.pop("nativeInterruptId"),
        lambda value: value.pop("actionIndex"),
        lambda value: value.__setitem__("actionIndex", True),
        lambda value: value.__setitem__("toolName", "other"),
        lambda value: value.__setitem__("allowedDecisions", ["approve", "approve"]),
        lambda value: value.__setitem__("allowedDecisions", ["allow"]),
        lambda value: value.__setitem__("originalArgs", {"file_path": "/other"}),
        lambda value: value.__setitem__("unknown", True),
        lambda value: value.__setitem__("originalArgs", {"ratio": inf}),
    ),
)
def test_parser_rejects_missing_unknown_or_tampered_metadata(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    interrupt = _interrupt()
    metadata = deepcopy(interrupt.metadata)
    assert isinstance(metadata, dict)
    deepagents = metadata["deepagents"]
    assert isinstance(deepagents, dict)
    mutation(deepagents)
    changed = interrupt.model_copy(update={"metadata": metadata})

    with pytest.raises(ToolReviewContractError):
        parse_tool_review_interrupt(changed)


def test_python_and_web_contract_fixtures_are_identical_and_valid() -> None:
    package_fixture_path = (
        _PACKAGE_ROOT
        / "src"
        / "tinkerfin_agui_adapter"
        / "contracts"
        / "tool-review.fixture.json"
    )
    web_fixture_path = (
        _REPOSITORY_ROOT
        / "apps"
        / "studio"
        / "web"
        / "src"
        / "features"
        / "conversation"
        / "agui"
        / "contracts"
        / "tool-review.fixture.json"
    )
    package_text = package_fixture_path.read_text(encoding="utf-8")
    assert web_fixture_path.read_text(encoding="utf-8") == package_text
    parsed = ToolReviewInterruptMetadata.model_validate_json(package_text)
    assert parsed.schema_id == TOOL_REVIEW_SCHEMA
    assert parsed.model_dump(mode="json", by_alias=True) == json.loads(package_text)
