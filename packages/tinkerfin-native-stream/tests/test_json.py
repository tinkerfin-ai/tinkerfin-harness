"""Public finite-JSON normalization contracts."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

from tinkerfin_native_stream import NativeStreamContractError, to_json_value
from tinkerfin_native_stream.public import normalize_operational_data


def test_public_normalizer_hides_recursive_cycle_state() -> None:
    assert tuple(inspect.signature(to_json_value).parameters) == ("value",)

    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(NativeStreamContractError, match="must not contain cycles"):
        to_json_value(recursive)


def test_public_json_types_do_not_change_diagnostic_capture_tags() -> None:
    class Status(Enum):
        READY = "ready"

    value = {
        "identifier": UUID("00000000-0000-0000-0000-000000000001"),
        "at": datetime(2026, 1, 1, tzinfo=UTC),
        "status": Status.READY,
        "bytes": b"visible",
        "binary": b"\xff",
        "literal": {"$type": "tuple", "items": [1, 2]},
    }
    assert normalize_operational_data(value) == {
        "identifier": "00000000-0000-0000-0000-000000000001",
        "at": "2026-01-01T00:00:00Z",
        "status": "ready",
        "bytes": "dmlzaWJsZQ==",
        "binary": "_w==",
        "literal": {"$type": "tuple", "items": [1, 2]},
    }
    assert to_json_value(b"\xff") == {"$type": "bytes", "base64": "/w=="}


class _Buffer(BaseModel):
    blob: bytes


class _NestedBuffer(BaseModel):
    nested: _Buffer


class _RedactedBuffer(BaseModel):
    blob: bytes
    secret: str

    @field_serializer("secret", when_used="json")
    def redact(self, value: str) -> str:
        return "redacted"


class _RenderedModel(BaseModel):
    secret: str

    @model_serializer(mode="plain", when_used="json")
    def render(self) -> dict[str, str]:
        return {"public": "redacted"}


class _WrappedBuffer(BaseModel):
    blob: bytes
    secret: str

    @model_serializer(mode="wrap", when_used="json")
    def render(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        result["secret"] = "redacted"
        return result


class _UnionBuffer(BaseModel):
    value: _Buffer | _RedactedBuffer


class _HexBuffer(BaseModel):
    model_config = ConfigDict(ser_json_bytes="hex")
    blob: bytes = Field(serialization_alias="encoded")


@pydantic_dataclass
class _DataclassBuffer:
    blob: bytes
    secret: str

    @field_serializer("secret", when_used="json")
    def redact(self, value: str) -> str:
        return "redacted"


@pydantic_dataclass(slots=True)
class _RenderedDataclass:
    secret: str

    @model_serializer(mode="plain", when_used="json")
    def render(self) -> dict[str, str]:
        return {"public": "redacted"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_Buffer(blob=b"\xff"), {"blob": "_w=="}),
        (_NestedBuffer(nested=_Buffer(blob=b"\xff")), {"nested": {"blob": "_w=="}}),
        (
            _RedactedBuffer(blob=b"\xff", secret="private marker"),
            {"blob": "_w==", "secret": "redacted"},
        ),
        (_RenderedModel(secret="private marker"), {"public": "redacted"}),
        (
            _WrappedBuffer(blob=b"\xff", secret="private marker"),
            {"blob": "_w==", "secret": "redacted"},
        ),
        (_UnionBuffer(value=_Buffer(blob=b"\xff")), {"value": {"blob": "_w=="}}),
        (
            _UnionBuffer(value=_RedactedBuffer(blob=b"\xff", secret="private marker")),
            {"value": {"blob": "_w==", "secret": "redacted"}},
        ),
        (_HexBuffer(blob=b"\xff"), {"encoded": "ff"}),
        (RootModel[bytes](b"\xff"), "_w=="),
        (
            _DataclassBuffer(blob=b"\xff", secret="private marker"),
            {"blob": "_w==", "secret": "redacted"},
        ),
        (_RenderedDataclass(secret="private marker"), {"public": "redacted"}),
    ],
)
def test_public_models_preserve_binary_fields_and_json_serializer_privacy(
    value: BaseModel | _DataclassBuffer | _RenderedDataclass, expected: object
) -> None:
    assert normalize_operational_data(value) == expected


def test_public_model_encoding_preserves_host_model_configuration() -> None:
    model = _Buffer(blob=b"\xff")
    original_config = dict(_Buffer.model_config)
    original_serializer = _Buffer.__pydantic_serializer__
    original_schema = _Buffer.__pydantic_core_schema__

    assert normalize_operational_data(model) == {"blob": "_w=="}
    assert _Buffer.model_config == original_config
    assert _Buffer.__pydantic_serializer__ is original_serializer
    assert _Buffer.__pydantic_core_schema__ is original_schema
    assert model.blob == b"\xff"
    with pytest.raises(UnicodeDecodeError):
        model.model_dump(mode="json")


def test_public_model_defaults_and_metadata_are_business_values() -> None:
    class WithDefault(BaseModel):
        payload: dict[str, str] = Field(
            default={"type": "model", "schema": "business"},
            json_schema_extra={"type": "model"},
        )

    assert normalize_operational_data(WithDefault()) == {
        "payload": {"type": "model", "schema": "business"}
    }
