"""Finite public JSON shared by Native capture and protocol conversion."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, TypeGuard, cast

from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic.dataclasses import is_pydantic_dataclass
from pydantic_core import SchemaSerializer, core_schema, to_jsonable_python

from .stream import NativeExtraStreamPart, NativeUpdatesStreamPart

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def normalize_operational_data(value: object) -> JsonValue:
    """Preserve operational fields as finite plain JSON without type reconstruction.

    Tuples become arrays and bytes become URL-safe base64 strings. Pydantic models
    and dataclasses retain aliases, JSON serializers, and byte-encoding settings;
    unconfigured model byte fields use the same base64 default.
    Literal business dictionaries, including ``$type`` fields, retain their meaning.
    Opaque values fail rather than being stringified or reconstructed by class name.

    Args:
        value: JSON-compatible scalar, container, dataclass, or Pydantic value.

    Returns:
        Detached plain JSON with finite numbers and unchanged operational fields.

    Raises:
        ValueError: A value is opaque, non-finite, or not JSON representable.
    """

    structured = _public_structure(value, active=set())
    normalized = _JSON.validate_python(
        to_jsonable_python(structured, bytes_mode="base64")
    )
    _require_finite(normalized)
    return normalized


def _public_structure(value: object, *, active: set[int]) -> object:
    if isinstance(value, Enum):
        return _public_structure(value.value, active=active)
    structured = (
        isinstance(value, BaseModel)
        or _is_mapping(value)
        or is_dataclass(value)
        and not isinstance(value, type)
        or _is_sequence(value)
    )
    if not structured:
        return value
    identity = id(value)
    if identity in active:
        raise ValueError("public stream payloads must not contain cycles")
    active.add(identity)
    try:
        value_type = type(value)
        if isinstance(value, BaseModel):
            schema = _copy_public_schema(value.__pydantic_core_schema__)
            return SchemaSerializer(
                schema, core_schema.CoreConfig(ser_json_bytes="base64")
            ).to_python(value, mode="json", by_alias=True)
        if is_pydantic_dataclass(value_type):
            schema = _copy_public_schema(value_type.__pydantic_core_schema__)
            return SchemaSerializer(
                schema, core_schema.CoreConfig(ser_json_bytes="base64")
            ).to_python(value, mode="json", by_alias=True)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: _public_structure(getattr(value, field.name), active=active)
                for field in fields(value)
            }
        if _is_mapping(value):
            return {
                key: _public_structure(child, active=active)
                for key, child in value.items()
            }
        if _is_sequence(value):
            return [_public_structure(child, active=active) for child in value]
        raise TypeError("public stream structured value is unsupported")
    finally:
        active.remove(identity)


def _copy_public_schema(value: Any) -> Any:
    """Set byte defaults in a detached Pydantic serialization schema.

    CoreSchema contains heterogeneous schema nodes and field maps. Arbitrary
    defaults, literal values, metadata, and configuration are not schema nodes.
    Pydantic-core 2.46's model/dataclass serializer may reuse the class's prebuilt
    serializer; an explicit serialization schema uses our local byte defaults while
    retaining the actual class, field schemas, and existing serializer hooks.
    """

    if isinstance(value, list):
        return [_copy_public_schema(child) for child in cast(list[Any], value)]
    if isinstance(value, tuple):
        return tuple(
            _copy_public_schema(child) for child in cast(tuple[Any, ...], value)
        )
    if not isinstance(value, dict):
        return value
    source = cast(dict[Any, Any], value)
    is_schema = isinstance(source.get("type"), str)
    copied = {
        key: child
        if is_schema and key in {"default", "expected", "metadata", "config"}
        else _copy_public_schema(child)
        for key, child in source.items()
    }
    if is_schema and copied["type"] in {"model", "dataclass"}:
        config = core_schema.CoreConfig(**copied.get("config", {}))
        config.setdefault("ser_json_bytes", "base64")
        copied["config"] = config
        if "serialization" not in copied:
            if copied["type"] == "model":
                copied["serialization"] = core_schema.model_schema(
                    copied["cls"],
                    copied["schema"],
                    root_model=copied.get("root_model"),
                    generic_origin=copied.get("generic_origin"),
                    extra_behavior=copied.get("extra_behavior"),
                    config=config,
                )
            else:
                copied["serialization"] = core_schema.dataclass_schema(
                    copied["cls"],
                    copied["schema"],
                    copied["fields"],
                    generic_origin=copied.get("generic_origin"),
                    slots=copied.get("slots"),
                    config=config,
                )
    return copied


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _require_finite(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid operational JSON")
    if isinstance(value, list):
        for item in value:
            _require_finite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _require_finite(item)


def sanitize_public_data(value: object) -> JsonValue:
    """Remove provider reasoning from its metadata path after plain JSON conversion.

    Only ``additional_kwargs.reasoning_content`` is private here. Tool operational
    arguments use ``normalize_operational_data`` so same-named business fields remain
    unchanged at those explicitly declared boundaries.

    Args:
        value: A public payload whose provider metadata must be filtered.

    Returns:
        Finite plain JSON without provider reasoning at the reserved metadata path.

    Raises:
        ValueError: The payload cannot be represented as finite plain JSON.
    """

    return _filter_provider_metadata(normalize_operational_data(value))


def _filter_provider_metadata(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_filter_provider_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: {
            nested_key: _filter_provider_metadata(nested)
            for nested_key, nested in child.items()
            if nested_key != "reasoning_content"
        }
        if key == "additional_kwargs" and isinstance(child, dict)
        else _filter_provider_metadata(child)
        for key, child in value.items()
    }


def public_task_error(error: object | None) -> dict[str, str] | None:
    """Expose one safe task failure category independently of private diagnostics."""

    return None if error is None else {"type": "TaskError"}


def _task_error_fields(payload: Mapping[object, object]) -> dict[object, object]:
    result = dict(payload)
    if "error" in result:
        result["error"] = public_task_error(result["error"])
    return result


def _checkpoint_errors(payload: Mapping[object, object]) -> dict[object, object]:
    result = dict(payload)
    tasks = result.get("tasks")
    if isinstance(tasks, (tuple, list)):
        result["tasks"] = [
            _task_error_fields(task) if _is_mapping(task) else task
            for task in cast(Sequence[object], tasks)
        ]
    return result


def public_extra_data(
    part: NativeExtraStreamPart | NativeUpdatesStreamPart,
) -> JsonValue:
    """Capture extra-mode payloads in the same JSON form used by public RAW events.

    Error classification applies only to declared Native task-error fields. Domain
    dictionaries and model values are normalized without interpreting type tags.

    Args:
        part: A validated updates, custom, checkpoint, or debug part.

    Returns:
        Detached JSON with classified task errors and private provider reasoning removed.

    Raises:
        ValueError: A value outside a declared task-error field is not JSON compatible.
    """

    data = part.data
    if _is_mapping(data):
        if part.type == "checkpoints":
            data = _checkpoint_errors(data)
        elif part.type == "debug":
            payload = data.get("payload")
            if _is_mapping(payload):
                if data.get("type") == "task_result":
                    data = {**data, "payload": _task_error_fields(payload)}
                elif data.get("type") == "checkpoint":
                    data = {**data, "payload": _checkpoint_errors(payload)}
    return sanitize_public_data(data)


__all__ = [
    "normalize_operational_data",
    "public_extra_data",
    "public_task_error",
    "sanitize_public_data",
]
