"""Finite JSON normalization for supported native framework objects."""

from __future__ import annotations

import base64
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import cast
from uuid import UUID

from langchain_core.messages import BaseMessage, message_to_dict
from langgraph.types import Interrupt
from pydantic import BaseModel, JsonValue

from .errors import NativeStreamContractError


def qualified_name(value: object) -> str:
    """Return one stable fully qualified Python type name."""

    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def to_json_value(value: object) -> JsonValue:
    """Convert a supported native value to finite tagged JSON.

    Args:
        value: Native value at the trusted Runtime boundary.

    Returns:
        A detached finite JSON representation.

    Raises:
        NativeStreamContractError: The graph contains cycles or non-finite numbers.
        TypeError: A value shape is unsupported or a mapping key is not text.
    """

    return _to_json_value(value, active=set())


def _to_json_value(value: object, *, active: set[int]) -> JsonValue:
    """Normalize recursively while keeping cycle detection private."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NativeStreamContractError(
                "native stream parts must not contain non-finite numbers"
            )
        return value
    if isinstance(value, bytes | bytearray):
        return {
            "$type": "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, Enum):
        return {
            "$type": "enum",
            "class": qualified_name(value),
            "value": _to_json_value(value.value, active=active),
        }
    if isinstance(value, BaseException):
        return {
            "$type": "exception",
            "class": qualified_name(value),
            "message": str(value),
        }

    identity = id(value)
    if identity in active:
        raise NativeStreamContractError("native stream parts must not contain cycles")
    active.add(identity)
    try:
        if isinstance(value, BaseMessage):
            normalized = _to_json_value(message_to_dict(value), active=active)
            return {"$type": "langchain.message", "value": normalized}
        if isinstance(value, Interrupt):
            return {
                "$type": "langgraph.interrupt",
                "id": value.id,
                "value": _to_json_value(value.value, active=active),
            }
        if isinstance(value, BaseModel):
            normalized = _to_json_value(
                value.model_dump(mode="python", by_alias=True),
                active=active,
            )
            return {
                "$type": "pydantic",
                "class": qualified_name(value),
                "value": normalized,
            }
        if is_dataclass(value) and not isinstance(value, type):
            return {
                "$type": "dataclass",
                "class": qualified_name(value),
                "value": {
                    field.name: _to_json_value(
                        getattr(value, field.name), active=active
                    )
                    for field in fields(value)
                },
            }
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            normalized_mapping: dict[str, JsonValue] = {}
            for key, item in mapping.items():
                if not isinstance(key, str):
                    raise TypeError("native mapping keys must be strings")
                normalized_mapping[key] = _to_json_value(item, active=active)
            return normalized_mapping
        if isinstance(value, tuple):
            return {
                "$type": "tuple",
                "items": [
                    _to_json_value(item, active=active)
                    for item in cast(tuple[object, ...], value)
                ],
            }
        if isinstance(value, Sequence) and not isinstance(value, str):
            return [
                _to_json_value(item, active=active)
                for item in cast(Sequence[object], value)
            ]
        raise TypeError(
            f"unsupported native stream value type: {qualified_name(value)}"
        )
    finally:
        active.remove(identity)


__all__ = ["qualified_name", "to_json_value"]
