"""Validate and snapshot LangGraph operations before any database work."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias, TypeGuard

from langgraph.store.base import (
    GetOp,
    InvalidNamespaceError,
    ListNamespacesOp,
    Op,
    PutOp,
    SearchOp,
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
Comparison: TypeAlias = Literal["$eq", "$ne", "$gt", "$gte", "$lt", "$lte"]


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def json_value(value: object, ancestors: frozenset[int] = frozenset()) -> JsonValue:
    """Copy finite JSON without coercing keys, tuples, bytes, or custom objects."""

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        value.encode("utf-8")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Store JSON numbers must be finite")
        return value
    if id(value) in ancestors:
        raise ValueError("Store JSON must not contain cycles")
    parents = ancestors | {id(value)}
    if _is_list(value):
        return [json_value(item, parents) for item in value]
    if _is_dict(value):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Store JSON object keys must be strings")
            key.encode("utf-8")
            result[key] = json_value(item, parents)
        return result
    raise TypeError("Store values must contain only JSON types")


def json_object(value: object) -> dict[str, JsonValue]:
    """Require a JSON object while keeping null available as a delete operation."""

    copied = json_value(value)
    if not isinstance(copied, dict):
        raise TypeError("Store document values must be JSON objects")
    return copied


def encode_json(value: JsonValue) -> str:
    """Serialize validated JSON, preserving Unicode and integer precision."""

    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _equality_value(value: JsonValue) -> JsonValue:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int | float):
        numerator, denominator = (
            (value, 1) if isinstance(value, int) else value.as_integer_ratio()
        )
        return ["number", str(numerator), str(denominator)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_equality_value(item) for item in value]]
    return ["object", [[key, _equality_value(value[key])] for key in sorted(value)]]


def equality_key(value: JsonValue) -> str:
    """Encode exact typed equality, including equal integral floats and integers."""

    return encode_json(_equality_value(value)).encode("ascii").hex()


def digest(value: str) -> bytes:
    """Build a compact index key whose full identity is stored and checked separately."""

    return hashlib.sha256(value.encode("utf-8")).digest()


def namespace_text(namespace: tuple[str, ...]) -> str:
    """Encode tuple ordering and complete labels without database collation rules."""

    return ".".join(label.encode("utf-8").hex() for label in namespace)


def decode_namespace(value: str) -> tuple[str, ...]:
    """Recover complete labels, including the empty tuple used by max_depth=0."""

    return (
        tuple(bytes.fromhex(label).decode("utf-8") for label in value.split("."))
        if value
        else ()
    )


def _namespace(value: tuple[str, ...], *, document: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise InvalidNamespaceError("Namespace must be a tuple of labels")
    if document and not value:
        raise InvalidNamespaceError("Document namespace cannot be empty")
    for label in value:
        if not isinstance(label, str) or not label or "." in label:
            raise InvalidNamespaceError(
                "Namespace labels must be non-empty strings without periods"
            )
        label.encode("utf-8")
    if document and value[0] == "langgraph":
        raise InvalidNamespaceError("The langgraph root namespace is reserved")
    return value


def _nonnegative(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class Get:
    """A validated exact document lookup."""

    namespace: tuple[str, ...]
    key: str


@dataclass(frozen=True, slots=True)
class Put:
    """A copied JSON document or an explicit deletion."""

    namespace: tuple[str, ...]
    key: str
    value: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class Filter:
    """One typed comparison against a literal top-level field."""

    field: str
    comparison: Comparison
    value: JsonValue


@dataclass(frozen=True, slots=True)
class Search:
    """A prefix-scoped query whose filters precede pagination."""

    prefix: tuple[str, ...]
    filters: tuple[Filter, ...]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ListNamespaces:
    """Full-path conditions with truncation applied only after matching."""

    conditions: tuple[tuple[Literal["prefix", "suffix"], tuple[str, ...]], ...]
    max_depth: int | None
    limit: int
    offset: int


Operation: TypeAlias = Get | Put | Search | ListNamespaces


def number_operand(value: JsonValue) -> float:
    """Require a finite binary64 ordering operand without bool or string coercion."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Store ordering operands must be numbers")
    try:
        operand = float(value)
    except OverflowError as error:
        raise ValueError(
            "Store ordering operands must fit finite binary64 precision"
        ) from error
    if not math.isfinite(operand):
        raise ValueError("Store ordering operands must be finite")
    return operand


def _filters(value: object) -> tuple[Filter, ...]:
    if value is None:
        return ()
    copied = json_object(value)
    result: list[Filter] = []
    for field, condition in copied.items():
        comparisons = condition if isinstance(condition, dict) else {"$eq": condition}
        if not comparisons:
            raise ValueError("Use $eq for an object-valued filter")
        for operator, operand in comparisons.items():
            if operator not in {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"}:
                raise ValueError("Unsupported Store filter; use $eq for object values")
            if operator in {"$gt", "$gte", "$lt", "$lte"}:
                number_operand(operand)
            match operator:
                case "$eq" | "$ne" | "$gt" | "$gte" | "$lt" | "$lte":
                    result.append(Filter(field, operator, operand))
                case _:
                    raise ValueError("Unsupported Store filter operator")
    return tuple(result)


def prepare_operations(operations: Iterable[Op]) -> tuple[Operation, ...]:
    """Consume inputs once and reject the entire invalid batch before I/O."""

    result: list[Operation] = []
    for op in operations:
        if isinstance(op, GetOp | PutOp):
            namespace = _namespace(op.namespace, document=True)
            if not isinstance(op.key, str):
                raise TypeError("Store document keys must be strings")
            op.key.encode("utf-8")
            if isinstance(op, PutOp):
                if op.ttl is not None:
                    raise NotImplementedError("This Store does not support TTL")
                if op.index is not None and op.index is not False:
                    raise NotImplementedError(
                        "This Store does not support vector indexing"
                    )
                value = None if op.value is None else json_object(op.value)
                result.append(Put(namespace, op.key, value))
            else:
                result.append(Get(namespace, op.key))
        elif isinstance(op, SearchOp):
            if op.query is not None:
                raise NotImplementedError("This Store does not support semantic search")
            result.append(
                Search(
                    _namespace(op.namespace_prefix, document=False),
                    _filters(op.filter),
                    _nonnegative(op.limit, "limit"),
                    _nonnegative(op.offset, "offset"),
                )
            )
        elif isinstance(op, ListNamespacesOp):
            conditions: list[tuple[Literal["prefix", "suffix"], tuple[str, ...]]] = []
            for condition in op.match_conditions or ():
                if condition.match_type not in {"prefix", "suffix"}:
                    raise ValueError("Namespace match type must be prefix or suffix")
                conditions.append(
                    (condition.match_type, _namespace(condition.path, document=False))
                )
            result.append(
                ListNamespaces(
                    tuple(conditions),
                    None
                    if op.max_depth is None
                    else _nonnegative(op.max_depth, "max_depth"),
                    _nonnegative(op.limit, "limit"),
                    _nonnegative(op.offset, "offset"),
                )
            )
        else:
            raise TypeError("Unsupported LangGraph Store operation")
    return tuple(result)
