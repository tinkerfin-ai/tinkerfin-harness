"""Public business-redaction contracts and mandatory Trace safety passes."""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, cast, runtime_checkable

from pydantic import JsonValue, ValidationError

from tinkerfin_contracts.media import Attachment

from .errors import TraceCaptureRejected

RedactionContentKind: TypeAlias = Literal[
    "message",
    "model_request",
    "model_response",
    "tool_arguments",
    "tool_result",
    "state",
    "interaction",
    "plan",
    "custom",
]

_CONTENT_KINDS = frozenset(
    {
        "message",
        "model_request",
        "model_response",
        "tool_arguments",
        "tool_result",
        "state",
        "interaction",
        "plan",
        "custom",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "auth_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "set_cookie",
        "token",
        "x_api_key",
    }
)


@dataclass(frozen=True, slots=True)
class RedactionContext:
    """Describe the functional source of one value without execution identity.

    Attributes:
        content_kind: Stable content role used to select business redaction rules.
        component_name: Public model, Tool, or contribution name when the
            source has one. Thread, Run, and user identities are never included.
    """

    content_kind: RedactionContentKind
    component_name: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous source roles and component names."""

        if self.content_kind not in _CONTENT_KINDS:
            raise ValueError("content_kind is not a supported redaction role")
        if self.component_name is not None and (
            not self.component_name
            or self.component_name != self.component_name.strip()
            or len(self.component_name) > 1024
        ):
            raise ValueError("component_name must be canonical bounded text or None")


@runtime_checkable
class TraceRedactor(Protocol):
    """Synchronously remove business-sensitive values from one detached JSON graph.

    Implementations must be deterministic, reentrant, free of I/O, and must not mutate
    the supplied value. The framework validates the returned graph and rejects the
    capture when the contract is violated.
    """

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        """Return a newly allocated standard JSON value for the supplied source."""

        ...


class CompositeRedactor:
    """Apply business Redactors in declaration order with validation after each one."""

    __slots__ = ("_redactors",)

    def __init__(self, *redactors: TraceRedactor) -> None:
        """Freeze one non-empty ordered Redactor chain.

        Args:
            *redactors: Business Redactors applied from left to right.

        Raises:
            TypeError: A value does not implement the synchronous Redactor contract.
            ValueError: No Redactor was supplied.
        """

        if not redactors:
            raise ValueError("CompositeRedactor requires at least one Redactor")
        for redactor in redactors:
            _validate_redactor(redactor)
        self._redactors = tuple(redactors)

    @property
    def redactors(self) -> tuple[TraceRedactor, ...]:
        """Return the immutable declaration-order Redactor chain."""

        return self._redactors

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        """Pass each validated result to the next Redactor."""

        current = _normalize_json(value)
        for redactor in self._redactors:
            current = _apply_redactor(redactor, current, context=context)
        return current


def redact_json_paths(
    value: JsonValue,
    *,
    paths: tuple[str, ...],
) -> JsonValue:
    """Replace matching RFC 6901 paths without mutating the source value.

    Args:
        value: Standard JSON graph to copy and redact.
        paths: Unique canonical JSON Pointers. A missing path is ignored; the root
            pointer replaces the whole value and must be used alone.

    Returns:
        A detached JSON graph whose selected values use the Trace redaction marker.

    Raises:
        TypeError: Paths are not supplied as a tuple of strings.
        ValueError: A path is malformed, duplicated, or ambiguously selects the root.
        TraceCaptureRejected: The source is not finite standard JSON.
    """

    if not isinstance(paths, tuple) or any(not isinstance(path, str) for path in paths):
        raise TypeError("paths must be a tuple of JSON Pointer strings")
    if any(not _is_canonical_pointer(path) for path in paths):
        raise ValueError("paths must contain canonical JSON Pointers")
    if len(set(paths)) != len(paths):
        raise ValueError("paths must be unique")
    if "" in paths:
        if len(paths) != 1:
            raise ValueError("the root path must be used alone")
        return _redacted_marker()
    copied = _normalize_json(value)
    for path in sorted(paths, key=lambda item: (item.count("/"), item)):
        _replace_pointer(copied, path)
    return copied


def _validate_redactor(redactor: object) -> None:
    """Reject objects that cannot honor the synchronous public contract."""

    if not isinstance(redactor, TraceRedactor):
        raise TypeError("redactor must implement TraceRedactor")
    method = redactor.redact
    if inspect.iscoroutinefunction(method):
        raise TypeError("TraceRedactor.redact must be synchronous")


def secure_redact(
    value: JsonValue,
    *,
    context: RedactionContext,
    redactor: TraceRedactor | None,
) -> JsonValue:
    """Apply non-replaceable framework safety before and after business redaction."""

    normalized = _normalize_json(value)
    safe = _strip_private_reasoning(
        _redact_credentials(_restore_attachment_references(normalized))
    )
    if redactor is None:
        return safe
    business_safe = _apply_redactor(redactor, safe, context=context)
    return _strip_private_reasoning(
        _redact_credentials(_restore_attachment_references(business_safe))
    )


def _apply_redactor(
    redactor: TraceRedactor,
    value: JsonValue,
    *,
    context: RedactionContext,
) -> JsonValue:
    """Call one untrusted extension without exposing or accepting mutable aliases."""

    _validate_redactor(redactor)
    supplied = _normalize_json(value)
    before = _canonical_json(supplied)
    try:
        result = redactor.redact(supplied, context=context)
    except TraceCaptureRejected:
        raise
    except Exception as error:
        raise TraceCaptureRejected(
            "Trace business redaction failed",
            diagnostic_context={"redactor_type": _qualified_name(redactor)},
            cause=error,
        ) from error
    if isinstance(result, Awaitable):
        if inspect.iscoroutine(result):
            result.close()
        raise TraceCaptureRejected(
            "Trace business redaction returned an awaitable",
            diagnostic_context={"redactor_type": _qualified_name(redactor)},
        )
    try:
        input_mutated = _canonical_json(supplied) != before
    except (TypeError, ValueError) as error:
        raise TraceCaptureRejected(
            "Trace business redaction mutated its input",
            diagnostic_context={"redactor_type": _qualified_name(redactor)},
            cause=error,
        ) from error
    if input_mutated:
        raise TraceCaptureRejected(
            "Trace business redaction mutated its input",
            diagnostic_context={"redactor_type": _qualified_name(redactor)},
        )
    try:
        return _normalize_json(result)
    except TraceCaptureRejected as error:
        raise TraceCaptureRejected(
            "Trace business redaction returned invalid JSON",
            diagnostic_context={"redactor_type": _qualified_name(redactor)},
            cause=error,
        ) from error


def _normalize_json(value: object) -> JsonValue:
    """Detach finite acyclic JSON without coercing third-party objects."""

    try:
        return _normalize_json_value(value, active=set())
    except RecursionError as error:
        raise TraceCaptureRejected(
            "Trace capture requires bounded acyclic JSON",
            cause=error,
        ) from error


def _normalize_json_value(value: object, *, active: set[int]) -> JsonValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceCaptureRejected("Trace capture requires a finite JSON value")
        return value
    if isinstance(value, list):
        sequence = cast(list[object], value)
        identity = id(sequence)
        if identity in active:
            raise TraceCaptureRejected("Trace capture requires acyclic JSON")
        active.add(identity)
        try:
            return [_normalize_json_value(item, active=active) for item in sequence]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise TraceCaptureRejected("Trace JSON object keys must be strings")
        identity = id(mapping)
        if identity in active:
            raise TraceCaptureRejected("Trace capture requires acyclic JSON")
        active.add(identity)
        try:
            return {
                cast(str, key): _normalize_json_value(item, active=active)
                for key, item in mapping.items()
            }
        finally:
            active.remove(identity)
    raise TraceCaptureRejected(
        "Trace capture requires standard JSON values",
        diagnostic_context={"value_type": _qualified_name(value)},
    )


def _credential_key(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _is_credential_key(value: str) -> bool:
    normalized = _credential_key(value)
    return any(
        normalized == credential or normalized.endswith(f"_{credential}")
        for credential in _CREDENTIAL_KEYS
    )


def _restore_attachment_references(value: JsonValue) -> JsonValue:
    """Remove request-only file bytes using the explicit attachment metadata contract."""
    if isinstance(value, list):
        return [_restore_attachment_references(item) for item in value]
    if not isinstance(value, dict):
        return value
    extras = value.get("extras")
    content_type = value.get("type")
    if (
        isinstance(content_type, str)
        and content_type in {"image_url", "image", "audio", "video", "file"}
        and isinstance(extras, dict)
        and "attachment" in extras
    ):
        try:
            return Attachment.model_validate(extras["attachment"]).content_block()
        except ValidationError as error:
            raise TraceCaptureRejected(
                "Trace attachment metadata is invalid", cause=error
            ) from error
    return {key: _restore_attachment_references(item) for key, item in value.items()}


def _redact_credentials(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and _is_credential_key(value[0])
        ):
            return [value[0], _redacted_marker()]
        return [_redact_credentials(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            _redacted_marker() if _is_credential_key(key) else _redact_credentials(item)
        )
        for key, item in value.items()
    }


def _strip_private_reasoning(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_strip_private_reasoning(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, JsonValue] = {}
    for key, item in value.items():
        if key == "additional_kwargs" and isinstance(item, dict):
            metadata = {
                metadata_key: metadata_value
                for metadata_key, metadata_value in item.items()
                if metadata_key != "reasoning_content"
            }
            cleaned[key] = _strip_private_reasoning(metadata)
        else:
            cleaned[key] = _strip_private_reasoning(item)
    return cleaned


def _redacted_marker() -> dict[str, JsonValue]:
    return {"$type": "redacted"}


def _is_canonical_pointer(path: str) -> bool:
    if path == "":
        return True
    if not path.startswith("/"):
        return False
    index = 0
    while index < len(path):
        if path[index] != "~":
            index += 1
            continue
        if index + 1 >= len(path) or path[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


def _pointer_components(path: str) -> tuple[str, ...]:
    return tuple(
        value.replace("~1", "/").replace("~0", "~") for value in path.split("/")[1:]
    )


def _replace_pointer(value: JsonValue, path: str) -> None:
    components = _pointer_components(path)
    current = value
    for component in components[:-1]:
        if isinstance(current, dict):
            if component not in current:
                return
            current = current[component]
            continue
        if isinstance(current, list) and _is_array_index(component):
            index = int(component)
            if index >= len(current):
                return
            current = current[index]
            continue
        return
    target = components[-1]
    if isinstance(current, dict):
        if target in current:
            current[target] = _redacted_marker()
    elif isinstance(current, list) and _is_array_index(target):
        index = int(target)
        if index < len(current):
            current[index] = _redacted_marker()


def _is_array_index(value: str) -> bool:
    return value == "0" or (
        bool(value)
        and value.isascii()
        and value[0] in "123456789"
        and value.isdecimal()
    )


def _canonical_json(value: JsonValue) -> bytes:
    """Encode only an already validated graph for mutation comparison."""

    import json

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _qualified_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


__all__ = [
    "CompositeRedactor",
    "RedactionContentKind",
    "RedactionContext",
    "TraceRedactor",
    "redact_json_paths",
]
