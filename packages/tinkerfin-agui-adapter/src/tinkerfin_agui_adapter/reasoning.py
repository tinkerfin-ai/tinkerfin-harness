"""Provider-reasoning extraction and public-payload privacy boundaries."""

from __future__ import annotations

from pydantic import JsonValue

from tinkerfin_native_stream.public import (
    normalize_operational_data as normalize_operational_data,
)
from tinkerfin_native_stream.public import (
    sanitize_public_data as sanitize_public_data,
)


def json_values_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare JSON values without conflating booleans and number types."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right
