"""Resolve declared filesystem arguments without changing their source proposal."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue

from ._tool_schema import (
    FilePathDeclaration,
    file_path_declaration,
    resolve_local_reference,
    validate_tool_schema,
)
from .workspace import WorkspacePathResolver
from .workspace_tools import ToolDescriptor


class InvalidToolArguments(ValueError):
    """The original values do not satisfy the declared tool input schema."""


class _Missing(Enum):
    VALUE = "missing"


_Location = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class _Path:
    location: _Location
    declaration: FilePathDeclaration
    value: str | None


@dataclass(frozen=True, slots=True)
class _Selection:
    paths: tuple[_Path, ...]
    defaults: tuple[tuple[_Location, JsonValue], ...]

    @property
    def declarations(self) -> frozenset[tuple[_Location, FilePathDeclaration]]:
        return frozenset((path.location, path.declaration) for path in self.paths)


def _child(value: JsonValue, component: str | int) -> JsonValue:
    if isinstance(value, dict) and isinstance(component, str):
        return value[component]
    if isinstance(value, list) and isinstance(component, int):
        return value[component]
    raise TypeError("tool path location changed during resolution")


def _assign(
    arguments: dict[str, JsonValue], location: _Location, value: JsonValue
) -> None:
    if not location:
        raise ValueError("tool input must remain an object")
    parent: JsonValue = arguments
    for component in location[:-1]:
        parent = _child(parent, component)
    key = location[-1]
    if isinstance(parent, dict) and isinstance(key, str):
        parent[key] = value
    elif isinstance(parent, list) and isinstance(key, int):
        parent[key] = value
    else:
        raise TypeError("tool path location is not writable")


def _paths(
    schema: dict[str, JsonValue],
    arguments: dict[str, JsonValue],
    validator: Draft202012Validator,
) -> _Selection:
    # Inspect the immutable proposal for every matching branch before resolving
    # anything. Defaults are applied later, so they cannot select other branches
    # or suppress a conflicting declaration while collection is in progress.
    found: dict[_Location, _Path] = {}
    declarations: dict[_Location, FilePathDeclaration] = {}
    defaults: dict[_Location, JsonValue] = {}
    active: set[tuple[int, _Location]] = set()

    def walk(node: JsonValue, value: JsonValue | _Missing, location: _Location) -> bool:
        if not isinstance(node, dict) or (id(node), location) in active:
            return False
        active.add((id(node), location))
        try:
            defaulted = value is _Missing.VALUE and "default" in node
            if defaulted:
                value = deepcopy(node["default"])
            selected = False
            reference = node.get("$ref")
            if isinstance(reference, str):
                selected |= walk(
                    resolve_local_reference(schema, reference), value, location
                )
            declaration = file_path_declaration(node)
            if declaration is not None:
                previous_declaration = declarations.get(location)
                if (
                    previous_declaration is not None
                    and previous_declaration != declaration
                ):
                    raise ValueError(
                        "tool argument declares conflicting filesystem access"
                    )
                declarations[location] = declaration
                if (
                    isinstance(value, str)
                    or declaration.default_root
                    and (value is None or value is _Missing.VALUE)
                ):
                    path = _Path(
                        location, declaration, value if isinstance(value, str) else None
                    )
                    previous = found.get(location)
                    if previous is not None and previous != path:
                        raise ValueError(
                            "tool argument declares conflicting filesystem access"
                        )
                    found[location] = path
                    selected = True
                elif value is not None and value is not _Missing.VALUE:
                    raise ValueError(
                        "FilePath must annotate a string or a list item string"
                    )
            for keyword in ("allOf", "anyOf", "oneOf"):
                branches = node.get(keyword)
                if isinstance(branches, list):
                    for branch in branches:
                        if not isinstance(branch, (dict, bool)):
                            continue
                        if (
                            keyword == "allOf"
                            or value is not _Missing.VALUE
                            and validator.evolve(schema=branch).is_valid(value)  # pyright: ignore[reportUnknownMemberType]
                        ):
                            selected |= walk(branch, value, location)
            if isinstance(value, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    for name, child in properties.items():
                        selected |= walk(
                            child, value.get(name, _Missing.VALUE), (*location, name)
                        )
                patterns = node.get("patternProperties")
                additional = node.get("additionalProperties")
                for name, child in value.items():
                    matched = isinstance(properties, dict) and name in properties
                    if isinstance(patterns, dict):
                        for pattern, declaration_schema in patterns.items():
                            if re.search(pattern, name):
                                matched = True
                                selected |= walk(
                                    declaration_schema, child, (*location, name)
                                )
                    if not matched and isinstance(additional, dict):
                        selected |= walk(additional, child, (*location, name))
            elif isinstance(value, list):
                prefixes = node.get("prefixItems")
                items = node.get("items")
                for index, child in enumerate(value):
                    item_schema = (
                        prefixes[index]
                        if isinstance(prefixes, list) and index < len(prefixes)
                        else items
                    )
                    if item_schema is not None:
                        selected |= walk(item_schema, child, (*location, index))
            if defaulted and selected and value is not _Missing.VALUE:
                if location in defaults and defaults[location] != value:
                    raise ValueError(
                        "tool argument declares conflicting file path defaults"
                    )
                defaults[location] = value
            return selected
        finally:
            active.remove((id(node), location))

    walk(schema, arguments, ())
    return _Selection(
        tuple(found[key] for key in sorted(found, key=json.dumps)),
        tuple(
            (key, defaults[key])
            for key in sorted(defaults, key=lambda key: (len(key), json.dumps(key)))
        ),
    )


async def resolve_tool_arguments(
    descriptor: ToolDescriptor,
    arguments: Mapping[str, JsonValue],
    resolver: WorkspacePathResolver,
) -> dict[str, JsonValue]:
    """Resolve declared file paths while preserving their schema and access intent.

    Args:
        descriptor: Trusted host-declared schema, including nested FilePath fields.
        arguments: Original finite JSON arguments; this mapping is never modified.
        resolver: Read-only filesystem interpretation under current authorization.

    Returns:
        A fresh mapping whose canonical absolute paths satisfy the full input
        schema and select the same file access declarations as the proposal.

    Raises:
        InvalidToolArguments: Original arguments fail schema validation, before
            any path resolution occurs.
        ValueError: Declarations conflict, defaults are invalid, or resolution
            produces invalid paths, invalid arguments, or changed access intent.
            These failures must not execute the original unresolved arguments.
        BaseException: Resolution fails or is cancelled; no tool is executed.
    """
    schema = validate_tool_schema(descriptor.input_schema)
    validator = Draft202012Validator(schema)
    result = deepcopy(dict(arguments))
    json.dumps(result, allow_nan=False)
    try:
        # jsonschema's bundled typing leaves validator call signatures partial.
        validator.validate(result)  # pyright: ignore[reportUnknownMemberType]
    except ValidationError as error:
        raise InvalidToolArguments(
            "tool arguments do not match their input schema"
        ) from error
    selected = _paths(schema, result, validator)
    for location, value in selected.defaults:
        _assign(result, location, deepcopy(value))
    try:
        validator.validate(result)  # pyright: ignore[reportUnknownMemberType]
    except ValidationError as error:
        raise ValueError("file path defaults do not match the input schema") from error
    if (
        selected.defaults
        and _paths(schema, result, validator).declarations != selected.declarations
    ):
        raise ValueError("file path defaults changed their declared filesystem access")
    for path in selected.paths:
        resolved = await resolver(path.value, operation=path.declaration.operation)
        if (
            not isinstance(resolved, str)
            or not resolved.startswith("/")
            or "\0" in resolved
            or any(part in {".", ".."} for part in resolved.split("/"))
            or resolved != "/"
            and "" in resolved[1:].split("/")
        ):
            raise ValueError("workspace did not resolve a canonical absolute path")
        _assign(result, path.location, resolved)
    try:
        validator.validate(result)  # pyright: ignore[reportUnknownMemberType]
    except ValidationError as error:
        raise ValueError(
            "resolved tool arguments do not match their input schema"
        ) from error
    if _paths(schema, result, validator).declarations != selected.declarations:
        raise ValueError("resolved file paths changed their declared filesystem access")
    return result


__all__ = ["InvalidToolArguments", "resolve_tool_arguments"]
