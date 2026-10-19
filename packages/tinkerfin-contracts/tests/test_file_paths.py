"""Declared paths retain their access intent across JSON Schema applicators."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, JsonValue, ValidationError

from tinkerfin_contracts import (
    FilePath,
    InvalidToolArguments,
    ToolDescriptor,
    resolve_tool_arguments,
)


def file_schema(
    operation: Literal["read", "write"] = "read", *, default_root: bool = False
) -> dict[str, JsonValue]:
    return {
        "type": "string",
        "x-tinkerfin-file-path": {
            "operation": operation,
            "default_root": default_root,
        },
    }


def descriptor(schema: dict[str, JsonValue]) -> ToolDescriptor:
    return ToolDescriptor(
        name="files", description="Use selected files.", input_schema=schema
    )


class Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def __call__(
        self, path: str | None, *, operation: Literal["read", "write"]
    ) -> str:
        self.calls.append((path, operation))
        return "/work" if path is None else "/work/" + path


async def test_patterned_dictionary_values_resolve_without_mutating_the_proposal() -> (
    None
):
    class Input(BaseModel):
        files: dict[
            Annotated[str, Field(pattern="^file_")],
            Annotated[str, FilePath("write")],
        ]

    request: dict[str, JsonValue] = {"files": {"file_report": "report.md"}}
    resolver = Resolver()
    result = await resolve_tool_arguments(
        descriptor(Input.model_json_schema()), request, resolver
    )
    assert result == {"files": {"file_report": "/work/report.md"}}
    assert request == {"files": {"file_report": "report.md"}}
    assert resolver.calls == [("report.md", "write")]


async def test_overlapping_properties_and_patterns_resolve_once_and_exclude_additional() -> (
    None
):
    selected = descriptor(
        {
            "type": "object",
            "properties": {"file_main": file_schema("write")},
            "patternProperties": {
                "^file_": file_schema("write"),
                "main$": file_schema("write"),
            },
            "additionalProperties": file_schema("read"),
        }
    )
    resolver = Resolver()
    result = await resolve_tool_arguments(
        selected, {"file_main": "main.md", "other": "other.md"}, resolver
    )
    assert result == {"file_main": "/work/main.md", "other": "/work/other.md"}
    assert resolver.calls == [("main.md", "write"), ("other.md", "read")]


@pytest.mark.parametrize(
    "other", [file_schema("write"), file_schema(default_root=True)]
)
async def test_overlapping_access_conflicts_fail_before_any_resolution(
    other: dict[str, JsonValue],
) -> None:
    selected = descriptor(
        {
            "type": "object",
            "properties": {"file": file_schema()},
            "patternProperties": {"^file$": other},
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="conflicting filesystem access"):
        await resolve_tool_arguments(selected, {"file": "report.md"}, resolver)
    assert resolver.calls == []


async def test_nullable_root_declarations_cannot_disagree_before_resolution() -> None:
    selected = descriptor(
        {
            "type": "object",
            "properties": {"file": {**file_schema(), "type": ["string", "null"]}},
            "patternProperties": {
                "file": {**file_schema(default_root=True), "type": ["string", "null"]}
            },
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="conflicting filesystem access"):
        await resolve_tool_arguments(selected, {"file": None}, resolver)
    assert resolver.calls == []


async def test_all_applicable_union_branches_and_array_positions_contribute_paths() -> (
    None
):
    selected = descriptor(
        {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "prefixItems": [{"allOf": [file_schema(), file_schema()]}],
                    "items": {"oneOf": [file_schema("write"), {"type": "null"}]},
                }
            },
            "anyOf": [
                {"properties": {"first": file_schema()}},
                {"properties": {"second": file_schema("write")}},
            ],
        }
    )
    resolver = Resolver()
    result = await resolve_tool_arguments(
        selected,
        {"files": ["head.md", "tail.md", None], "first": "a.md", "second": "b.md"},
        resolver,
    )
    assert result == {
        "files": ["/work/head.md", "/work/tail.md", None],
        "first": "/work/a.md",
        "second": "/work/b.md",
    }
    assert resolver.calls == [
        ("head.md", "read"),
        ("tail.md", "write"),
        ("a.md", "read"),
        ("b.md", "write"),
    ]


@pytest.mark.parametrize("keyword", ["allOf", "anyOf"])
async def test_applicable_union_access_conflicts_are_not_hidden_by_the_first_branch(
    keyword: str,
) -> None:
    selected = descriptor(
        {
            "type": "object",
            "properties": {"file": {keyword: [file_schema(), file_schema("write")]}},
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="conflicting filesystem access"):
        await resolve_tool_arguments(selected, {"file": "report.md"}, resolver)
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("a b", "#/$defs/a%20b"),
        ("报告", "#/$defs/%E6%8A%A5%E5%91%8A"),
        ("a/b~c", "#/$defs/a~1b~0c"),
        ("a%20b", "#/$defs/a%2520b"),
        ("path", "#%2F$defs%2Fpath"),
    ],
)
async def test_local_references_decode_the_fragment_once_before_pointer_tokens(
    name: str, reference: str
) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "$defs": {name: file_schema()},
        "properties": {"file": {"$ref": reference}},
    }
    original = deepcopy(schema)
    resolver = Resolver()
    assert await resolve_tool_arguments(
        descriptor(schema), {"file": "report.md"}, resolver
    ) == {"file": "/work/report.md"}
    assert resolver.calls == [("report.md", "read")]
    assert schema == original


@pytest.mark.parametrize(
    "reference",
    [
        "#/$defs/a%",
        "#/$defs/a%GG",
        "#/$defs/%FF",
        "#/$defs/a~2b",
        "#/$defs/a~",
        "#/examples/01",
        "#/examples/-1",
        "#/examples/٠",
        "#/examples/-",
    ],
)
def test_invalid_fragment_escapes_and_noncanonical_array_indices_are_rejected(
    reference: str,
) -> None:
    with pytest.raises(ValidationError, match="invalid local reference"):
        descriptor(
            {
                "type": "object",
                "$defs": {"a%": {}, "a%GG": {}, "a~2b": {}, "a~": {}},
                "examples": [{}, {}],
                "properties": {"file": {"$ref": reference}},
            }
        )


async def test_canonical_array_reference_can_select_a_schema_in_literal_storage() -> (
    None
):
    resolver = Resolver()
    selected = descriptor(
        {
            "type": "object",
            "examples": [file_schema()],
            "properties": {"file": {"$ref": "#/examples/0"}},
        }
    )
    assert await resolve_tool_arguments(selected, {"file": "report.md"}, resolver) == {
        "file": "/work/report.md"
    }
    assert resolver.calls == [("report.md", "read")]


@pytest.mark.parametrize(
    "keyword",
    [
        "if",
        "then",
        "else",
        "not",
        "contains",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "additionalItems",
        "contentSchema",
        "dependentSchemas",
        "dependencies",
    ],
)
@pytest.mark.parametrize("referenced", [False, True])
def test_unresolved_schema_positions_reject_file_declarations_at_registration(
    keyword: str, referenced: bool
) -> None:
    child: JsonValue = {"$ref": "#/$defs/path"} if referenced else file_schema()
    selected: JsonValue = (
        {"flag": child} if keyword in {"dependentSchemas", "dependencies"} else child
    )
    with pytest.raises(ValidationError, match="unsupported schema position"):
        descriptor(
            {"type": "object", "$defs": {"path": file_schema()}, keyword: selected}
        )
    ordinary: JsonValue = (
        {"flag": {"type": "string"}}
        if keyword in {"dependentSchemas", "dependencies"}
        else {"type": "string"}
    )
    assert descriptor({"type": "object", keyword: ordinary}).name == "files"


@pytest.mark.parametrize("keyword", ["default", "examples", "enum", "const"])
async def test_literal_data_is_not_interpreted_as_a_file_schema(keyword: str) -> None:
    literal: JsonValue = {"if": file_schema()}
    value: JsonValue = [literal] if keyword in {"examples", "enum"} else literal
    selected = descriptor(
        {"type": "object", "properties": {"payload": {keyword: value}}}
    )
    resolver = Resolver()
    request: dict[str, JsonValue] = {"payload": literal}
    assert await resolve_tool_arguments(selected, request, resolver) == request
    assert resolver.calls == []


@pytest.mark.parametrize(
    "constraint", [{"pattern": "^[^/]"}, {"enum": ["report.md"]}, {"maxLength": 10}]
)
async def test_resolved_arguments_must_still_satisfy_the_complete_schema(
    constraint: dict[str, JsonValue],
) -> None:
    selected = descriptor(
        {"type": "object", "properties": {"file": {**file_schema(), **constraint}}}
    )
    resolver = Resolver()
    request = {"file": "report.md"}
    with pytest.raises(ValueError, match="resolved tool arguments") as failure:
        await resolve_tool_arguments(selected, request, resolver)
    assert not isinstance(failure.value, InvalidToolArguments)
    assert resolver.calls == [("report.md", "read")]
    assert request == {"file": "report.md"}


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
async def test_resolution_cannot_change_selected_file_access(keyword: str) -> None:
    selected = descriptor(
        {
            "type": "object",
            "properties": {
                "file": {
                    keyword: [
                        {**file_schema(), "pattern": "^[^/]"},
                        {**file_schema("write"), "pattern": "^/"},
                    ]
                }
            },
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="changed their declared filesystem access"):
        await resolve_tool_arguments(selected, {"file": "report.md"}, resolver)
    assert resolver.calls == [("report.md", "read")]


async def test_invalid_original_arguments_never_reach_the_resolver() -> None:
    selected = descriptor(
        {"type": "object", "properties": {"file": file_schema()}, "required": ["file"]}
    )
    resolver = Resolver()
    with pytest.raises(InvalidToolArguments):
        await resolve_tool_arguments(selected, {"file": 12}, resolver)
    assert resolver.calls == []


async def test_invalid_path_defaults_never_reach_the_resolver() -> None:
    selected = descriptor(
        {
            "type": "object",
            "properties": {
                "file": {**file_schema(), "maxLength": 3, "default": "report.md"}
            },
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="file path defaults"):
        await resolve_tool_arguments(selected, {}, resolver)
    assert resolver.calls == []


async def test_missing_default_root_behind_a_reference_is_bound() -> None:
    resolver = Resolver()
    selected = descriptor(
        {
            "type": "object",
            "$defs": {"path": file_schema(default_root=True)},
            "properties": {"file": {"$ref": "#/$defs/path"}},
        }
    )
    assert await resolve_tool_arguments(selected, {}, resolver) == {"file": "/work"}
    assert resolver.calls == [(None, "read")]


async def test_default_materialization_cannot_silently_change_access() -> None:
    selected = descriptor(
        {
            "type": "object",
            "allOf": [
                {"properties": {"file": {**file_schema(), "default": "report.md"}}},
                {
                    "anyOf": [
                        {
                            "required": ["file"],
                            "properties": {"file": file_schema("write")},
                        },
                        {"not": {"required": ["file"]}},
                    ]
                },
            ],
        }
    )
    resolver = Resolver()
    with pytest.raises(ValueError, match="conflicting filesystem access"):
        await resolve_tool_arguments(selected, {}, resolver)
    assert resolver.calls == []
