"""Stable JSON and interrupt models used by the stream adapter."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue, RootModel
from pydantic.alias_generators import to_camel

from tinkerfin_native_stream import NativeRuntimeInterrupt as AgentRuntimeInterrupt

from .reasoning import normalize_operational_data


class RuntimeModel(BaseModel):
    """Common strict configuration for adapter boundary models."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class JsonObject(RootModel[dict[str, JsonValue]]):
    """JSON object validated before crossing a public boundary."""


def to_json_value(value: object) -> JsonValue:
    """Normalize an auditable JSON value without stringifying opaque objects."""

    return normalize_operational_data(value)


__all__ = [
    "AgentRuntimeInterrupt",
    "JsonObject",
    "RuntimeModel",
    "to_json_value",
]
