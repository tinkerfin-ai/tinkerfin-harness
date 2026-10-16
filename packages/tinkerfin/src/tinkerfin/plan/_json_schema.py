"""Draft 2020-12 validation for Native Plan response contracts."""

from __future__ import annotations

import math
from typing import Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import JsonValue


class _JsonSchemaInstanceValidator(Protocol):
    def validate(self, instance: object) -> None: ...


def _contains_non_finite_number(value: JsonValue) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, list):
        return any(_contains_non_finite_number(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    return False


def require_valid_schema(schema: dict[str, JsonValue]) -> None:
    """Reject a malformed Plan response Schema before native publication."""

    if _contains_non_finite_number(schema):
        raise SchemaError("JSON Schema must contain only finite numbers")
    Draft202012Validator.check_schema(schema)


def validate_json_schema_instance(
    value: JsonValue,
    schema: dict[str, JsonValue],
) -> None:
    """Validate one trusted response against its frozen Plan Schema."""

    require_valid_schema(schema)
    if _contains_non_finite_number(value):
        raise ValidationError("JSON values must contain only finite numbers")
    validator = cast(
        _JsonSchemaInstanceValidator,
        Draft202012Validator(schema, format_checker=FormatChecker()),
    )
    validator.validate(value)


__all__ = [
    "SchemaError",
    "ValidationError",
    "require_valid_schema",
    "validate_json_schema_instance",
]
