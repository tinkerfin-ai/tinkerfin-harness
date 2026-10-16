"""Validate JSON snapshots before they cross a persistence boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast


def require_finite_json(value: object) -> None:
    """Reject values that cannot be represented by the persisted JSON contract."""

    _validate(value, active_containers=set())


def _validate(value: object, *, active_containers: set[int]) -> None:
    """Validate one JSON value while detecting cycles in mutable containers."""

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Automation JSON numbers must be finite")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Automation JSON strings must be valid UTF-8") from error
        return

    if isinstance(value, Mapping):
        container = cast(object, value)
        mapping = cast(Mapping[object, object], value)
        _enter_container(container, active_containers)
        try:
            for key, item in mapping.items():
                if not isinstance(key, str):
                    raise TypeError("Automation JSON object keys must be strings")
                _validate(key, active_containers=active_containers)
                _validate(item, active_containers=active_containers)
        finally:
            active_containers.remove(id(container))
        return

    if isinstance(value, list):
        container = cast(object, value)
        items = cast(list[object], value)
        _enter_container(container, active_containers)
        try:
            for item in items:
                _validate(item, active_containers=active_containers)
        finally:
            active_containers.remove(id(container))
        return

    raise TypeError(
        "Automation JSON values must be null, boolean, integer, finite number, "
        f"string, list, or object; got {type(value).__name__}"
    )


def _enter_container(value: object, active_containers: set[int]) -> None:
    """Reject cyclic containers before recursion can exhaust the call stack."""

    marker = id(value)
    if marker in active_containers:
        raise ValueError("Automation JSON values must not contain cycles")
    active_containers.add(marker)
