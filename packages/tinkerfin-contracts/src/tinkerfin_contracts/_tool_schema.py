"""Keep portable tool schemas self-contained before any argument validation."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

FILE_PATH_METADATA = "x-tinkerfin-file-path"

_SCHEMA_MAPS = (
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
    "dependencies",
)
_SCHEMA_VALUES = (
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "additionalItems",
    "unevaluatedItems",
    "contains",
    "items",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
)
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_UNSUPPORTED_PATH_LOCATIONS = frozenset(
    {
        "dependentSchemas",
        "dependencies",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)


@dataclass(frozen=True, slots=True)
class FilePathDeclaration:
    """Describe access required by one schema annotation."""

    operation: Literal["read", "write"]
    default_root: bool


def file_path_declaration(node: dict[str, JsonValue]) -> FilePathDeclaration | None:
    """Read a validated annotation without interpreting literal schema data."""
    if FILE_PATH_METADATA not in node:
        return None
    declaration = node[FILE_PATH_METADATA]
    if not isinstance(declaration, dict) or set(declaration) != {
        "operation",
        "default_root",
    }:
        raise ValueError("tool schema contains an invalid file path declaration")
    operation = declaration["operation"]
    default_root = declaration["default_root"]
    if operation not in ("read", "write") or not isinstance(default_root, bool):
        raise ValueError("tool schema contains an invalid file path declaration")
    if operation == "read":
        return FilePathDeclaration("read", default_root)
    return FilePathDeclaration("write", default_root)


def _local_pointer(reference: str) -> str:
    if not reference.startswith("#"):
        raise ValueError(
            "tool schemas require local references to their own definitions"
        )
    fragment = reference[1:]
    if re.search(r"%(?![0-9a-fA-F]{2})", fragment):
        raise ValueError("tool schema contains an invalid local reference")
    try:
        pointer = unquote(fragment, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("tool schema contains an invalid local reference") from error
    if pointer and not pointer.startswith("/") or re.search(r"~(?:[^01]|$)", pointer):
        raise ValueError("tool schema contains an invalid local reference")
    return pointer


def resolve_local_reference(root: dict[str, JsonValue], reference: str) -> JsonValue:
    """Resolve one JSON-pointer reference inside this schema document."""
    pointer = _local_pointer(reference)
    if not pointer:
        return root
    value: JsonValue = root
    try:
        for component in pointer[1:].split("/"):
            key = component.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                value = value[key]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                value = value[int(key)]
            else:
                raise ValueError("tool schema contains an invalid local reference")
    except (KeyError, IndexError) as error:
        raise ValueError("tool schema contains an invalid local reference") from error
    return value


def local_references(schema: dict[str, JsonValue]) -> None:
    """Check and canonicalize references, including unsupported path positions."""
    visited: set[tuple[int, bool]] = set()

    def visit(node: JsonValue, unsupported: bool = False) -> None:
        if not isinstance(node, dict) or (id(node), unsupported) in visited:
            return
        visited.add((id(node), unsupported))
        if node is not schema and "$id" in node:
            raise ValueError("tool schemas require one local reference document")
        declaration = file_path_declaration(node)
        if declaration is not None and unsupported:
            raise ValueError("FilePath occurs in an unsupported schema position")
        reference = node.get("$ref")
        if isinstance(reference, str):
            target = resolve_local_reference(schema, reference)
            if not isinstance(target, (dict, bool)):
                # This is an invalid JSON Schema value, not a Python API type error.
                raise ValueError("tool reference target must be a local schema")  # noqa: TRY004
            # RFC 6901 decodes the URI fragment before the JSON Pointer. Keep
            # its leading slash literal for referencing 0.37.0's lookup API;
            # quote percent signs again so validation never decodes twice.
            node["$ref"] = "#" + quote(
                _local_pointer(reference), safe="/~!$&'()*+,;=:@?"
            )
            visit(target, unsupported)
        if "$dynamicRef" in node or "$recursiveRef" in node:
            raise ValueError(
                "tool schemas support local $ref definitions, not dynamic references"
            )
        for keyword in _SCHEMA_MAPS:
            children = node.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    visit(child, unsupported or keyword in _UNSUPPORTED_PATH_LOCATIONS)
        for keyword in _SCHEMA_VALUES:
            child = node.get(keyword)
            if child is not None:
                visit(child, unsupported or keyword in _UNSUPPORTED_PATH_LOCATIONS)
        for keyword in _SCHEMA_LISTS:
            children = node.get(keyword)
            if isinstance(children, list):
                for child in children:
                    visit(child, unsupported)

    visit(schema)


def validate_tool_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a checked copy without retrieving schemas or mutating the input."""
    validated = deepcopy(schema)
    local_references(validated)
    try:
        Draft202012Validator.check_schema(validated)
    except SchemaError as error:
        raise ValueError("tool descriptor has an invalid input schema") from error
    return validated
