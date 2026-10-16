"""Finite JSON validation shared by every observation payload."""

from __future__ import annotations

import math
from typing import Annotated, TypeAlias

from pydantic import AfterValidator, JsonValue


def _require_finite(value: JsonValue) -> JsonValue:
    # Pydantic's JsonValue uses an unrestricted JSON-input fast path. A validator
    # on the payload type protects both input modes; model float configuration alone
    # does not constrain that path. See test_finite_json.py for the public boundary.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _require_finite(item)
    elif isinstance(value, list):
        for item in value:
            _require_finite(item)
    return value


FiniteJsonValue: TypeAlias = Annotated[JsonValue, AfterValidator(_require_finite)]
