"""Public-safe payload capture with explicit omission semantics."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ._models import TraceModel
from .errors import TraceCaptureRejected
from .redaction import (
    RedactionContext,
    TraceRedactor,
    _normalize_json,
    secure_redact,
)


class CapturedValue(TraceModel, frozen=True):
    """Store either one safe JSON value or an explicit omission reason."""

    disposition: Literal["inline", "omitted"]
    safe_size_bytes: int = Field(ge=0)
    value: JsonValue | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def value_matches_disposition(self) -> CapturedValue:
        """Keep inline and omitted representations mutually exclusive."""

        if self.disposition == "omitted" and self.value is not None:
            raise ValueError("omitted capture cannot retain a value")
        if self.disposition == "omitted" and self.reason is None:
            raise ValueError("omitted capture requires a reason")
        if self.disposition == "inline" and self.reason is not None:
            raise ValueError("inline capture cannot have an omission reason")
        if self.disposition == "inline" and self.safe_size_bytes != len(
            _encode(self.value)
        ):
            raise ValueError("inline safe_size_bytes must match canonical JSON bytes")
        return self


class ToolCaptureRule(TraceModel, frozen=True):
    """Allow selected JSON Pointer paths for one Tool's public content."""

    tool_name: str = Field(min_length=1, max_length=1024)
    argument_paths: tuple[str, ...] = ()
    result_paths: tuple[str, ...] = ()
    include_review_description: bool = False

    @field_validator("argument_paths", "result_paths")
    @classmethod
    def paths_are_canonical_json_pointers(
        cls, paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Require unique RFC 6901 pointer syntax without retaining whole payloads."""

        if any(not _is_canonical_pointer(path) for path in paths):
            raise ValueError("Tool capture paths must be canonical JSON Pointers")
        if len(set(paths)) != len(paths):
            raise ValueError("Tool capture paths must be unique")
        if "" in paths and len(paths) != 1:
            raise ValueError("The root Tool capture path must be used alone")
        return paths


class ToolTraceCapture(TraceModel, frozen=True):
    """Define how one Tool contributes content and lifecycle facts to Trace."""

    mode: Literal[
        "full_content",
        "metadata_only",
        "selected_content",
        "disabled",
    ]
    argument_paths: tuple[str, ...] = ()
    result_paths: tuple[str, ...] = ()
    include_review_description: bool = False

    @classmethod
    def full_content(cls) -> Self:
        """Trace lifecycle plus complete sanitized arguments, results, and review text."""

        return cls(mode="full_content", include_review_description=True)

    @classmethod
    def metadata_only(cls) -> Self:
        """Trace Tool lifecycle while retaining no argument or result content."""

        return cls(mode="metadata_only")

    @classmethod
    def selected_content(
        cls,
        *,
        argument_paths: tuple[str, ...] = (),
        result_paths: tuple[str, ...] = (),
        include_review_description: bool = False,
    ) -> Self:
        """Trace lifecycle plus explicitly selected RFC 6901 content paths.

        Args:
            argument_paths: Canonical JSON Pointers selected from Tool arguments.
            result_paths: Canonical JSON Pointers selected from Tool results.
            include_review_description: Whether a public review description may be
                retained when selected arguments are available.

        Returns:
            An immutable selected-content setting for one Tool.
        """

        return cls(
            mode="selected_content",
            argument_paths=argument_paths,
            result_paths=result_paths,
            include_review_description=include_review_description,
        )

    @classmethod
    def disabled(cls) -> Self:
        """Suppress Tool lifecycle and content facts from Trace."""

        return cls(mode="disabled")

    @field_validator("argument_paths", "result_paths")
    @classmethod
    def paths_are_canonical_json_pointers(
        cls, paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Require unique canonical pointers and one unambiguous root selection."""

        if any(not _is_canonical_pointer(path) for path in paths):
            raise ValueError("Tool capture paths must be canonical JSON Pointers")
        if len(set(paths)) != len(paths):
            raise ValueError("Tool capture paths must be unique")
        if "" in paths and len(paths) != 1:
            raise ValueError("The root Tool capture path must be used alone")
        return paths

    @model_validator(mode="after")
    def paths_match_mode(self) -> ToolTraceCapture:
        """Keep path and review settings exclusive to content-bearing modes."""

        has_paths = bool(self.argument_paths or self.result_paths)
        if self.mode != "selected_content" and has_paths:
            raise ValueError("only selected_content accepts Tool capture paths")
        if self.mode in {"metadata_only", "disabled"} and (
            self.include_review_description
        ):
            raise ValueError(
                "metadata-only or disabled Tool capture cannot retain review text"
            )
        return self


class ReasoningCapturePolicy(TraceModel, frozen=True):
    """Control provider reasoning retention independently from public messages."""

    mode: Literal["omit", "content"] = "omit"

    @classmethod
    def omitted(cls) -> ReasoningCapturePolicy:
        """Return the safe default that records no reasoning content."""

        return cls(mode="omit")

    @classmethod
    def content(cls) -> ReasoningCapturePolicy:
        """Return an explicit policy that retains bounded extracted content."""

        return cls(mode="content")


class CapturePolicy(TraceModel, frozen=True):
    """Define public-safe retention for messages, state, and Tools."""

    include_error_messages: bool = False
    default_tool_capture: ToolTraceCapture = Field(
        default_factory=ToolTraceCapture.metadata_only
    )
    tool_overrides: tuple[tuple[str, ToolTraceCapture], ...] = ()
    tool_rules: tuple[ToolCaptureRule, ...] = ()

    @classmethod
    def public_safe(
        cls,
        *,
        tool_rules: tuple[ToolCaptureRule, ...] = (),
        include_error_messages: bool = False,
    ) -> CapturePolicy:
        """Return a metadata-only Tool policy for public diagnostics."""

        return cls(
            include_error_messages=include_error_messages,
            default_tool_capture=ToolTraceCapture.metadata_only(),
            tool_rules=tool_rules,
        )

    @classmethod
    def public_history(
        cls,
        *,
        tool_overrides: Mapping[str, ToolTraceCapture] | None = None,
        include_error_messages: bool = False,
    ) -> CapturePolicy:
        """Capture every Tool's sanitized public content with exact-name overrides.

        Args:
            tool_overrides: Optional settings for Tools whose content or lifecycle
                retention differs from the full-content default.
            include_error_messages: Whether bounded exception messages may be retained.

        Returns:
            A policy that automatically covers newly observed Tools.

        Raises:
            TypeError: An override key or value has the wrong type.
        """

        if tool_overrides is None:
            overrides: tuple[tuple[str, ToolTraceCapture], ...] = ()
        else:
            if not isinstance(tool_overrides, Mapping):
                raise TypeError("tool_overrides must be a mapping or None")
            normalized: list[tuple[str, ToolTraceCapture]] = []
            for tool_name, capture in tool_overrides.items():
                if not isinstance(tool_name, str):
                    raise TypeError("tool_overrides keys must be Tool name strings")
                if not isinstance(capture, ToolTraceCapture):
                    raise TypeError(
                        "tool_overrides values must be ToolTraceCapture values"
                    )
                normalized.append((tool_name, capture))
            overrides = tuple(normalized)
        return cls(
            include_error_messages=include_error_messages,
            default_tool_capture=ToolTraceCapture.full_content(),
            tool_overrides=overrides,
        )

    @field_validator("tool_rules")
    @classmethod
    def tool_names_are_unique(
        cls, rules: tuple[ToolCaptureRule, ...]
    ) -> tuple[ToolCaptureRule, ...]:
        """Ensure one deterministic capture rule owns each Tool name."""

        names = [rule.tool_name for rule in rules]
        if len(set(names)) != len(names):
            raise ValueError("Tool capture rules must use unique Tool names")
        return rules

    @field_validator("tool_overrides")
    @classmethod
    def override_names_are_unique(
        cls, overrides: tuple[tuple[str, ToolTraceCapture], ...]
    ) -> tuple[tuple[str, ToolTraceCapture], ...]:
        """Reject ambiguous duplicate per-Tool settings."""

        names = [tool_name for tool_name, _capture in overrides]
        if any(not tool_name or len(tool_name) > 1024 for tool_name in names):
            raise ValueError("Tool trace override names must be non-empty and bounded")
        if len(set(names)) != len(names):
            raise ValueError("Tool trace overrides must use unique Tool names")
        return overrides

    @model_validator(mode="after")
    def tool_rules_do_not_conflict_with_overrides(self) -> CapturePolicy:
        """Keep low-level selected paths and high-level overrides unambiguous."""

        rule_names = {rule.tool_name for rule in self.tool_rules}
        override_names = {tool_name for tool_name, _capture in self.tool_overrides}
        if rule_names.intersection(override_names):
            raise ValueError(
                "a Tool cannot use both a capture rule and a trace override"
            )
        return self

    def tool_capture(self, tool_name: str) -> ToolTraceCapture:
        """Resolve one Tool's effective capture setting by exact stable name."""

        override = next(
            (
                candidate
                for candidate_name, candidate in self.tool_overrides
                if candidate_name == tool_name
            ),
            None,
        )
        if override is not None:
            return override
        rule = next(
            (
                candidate
                for candidate in self.tool_rules
                if candidate.tool_name == tool_name
            ),
            None,
        )
        if rule is not None:
            return ToolTraceCapture.selected_content(
                argument_paths=rule.argument_paths,
                result_paths=rule.result_paths,
                include_review_description=rule.include_review_description,
            )
        return self.default_tool_capture

    def traces_tool(self, tool_name: str) -> bool:
        """Return whether one Tool contributes lifecycle facts to Trace."""

        return self.tool_capture(tool_name).mode != "disabled"

    def captures_review_description(self, tool_name: str) -> bool:
        """Return whether one Tool may retain its public review description."""

        return self.tool_capture(tool_name).include_review_description


class TraceCapturePipeline:
    """Apply one safe redaction and retention path for every Trace payload."""

    __slots__ = ("_policy", "_reasoning_policy", "_redactor")

    def __init__(
        self,
        *,
        policy: CapturePolicy,
        reasoning_policy: ReasoningCapturePolicy,
        redactor: TraceRedactor | None,
    ) -> None:
        self._policy = policy
        self._reasoning_policy = reasoning_policy
        self._redactor = redactor

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        """Return one detached safe value before retention decisions."""

        safe = secure_redact(value, context=context, redactor=self._redactor)
        _validate_context_shape(value, safe, context=context)
        return safe

    def capture(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
        max_bytes: int,
    ) -> CapturedValue:
        """Redact and retain one ordinary value within its exact byte budget."""

        return self.bound(
            self.redact(value, context=context),
            max_bytes=max_bytes,
        )

    def capture_reasoning(
        self,
        value: JsonValue,
        *,
        component_name: str | None,
        max_bytes: int,
    ) -> CapturedValue:
        """Apply the independent reasoning authorization after mandatory redaction."""

        _validate_max_bytes(max_bytes)
        safe = self.redact(
            value,
            context=RedactionContext(
                content_kind="model_response",
                component_name=component_name,
            ),
        )
        encoded = _encode(safe)
        if self._reasoning_policy.mode == "omit":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=len(encoded),
                reason="reasoning_capture_disabled",
            )
        return self.bound(safe, max_bytes=max_bytes)

    def capture_tool(
        self,
        *,
        tool_name: str,
        value: JsonValue,
        target: Literal["arguments", "result"],
        max_bytes: int,
    ) -> CapturedValue:
        """Redact complete Tool content before applying its retention selection."""

        _validate_max_bytes(max_bytes)
        safe = self.redact(
            value,
            context=RedactionContext(
                content_kind=(
                    "tool_arguments" if target == "arguments" else "tool_result"
                ),
                component_name=tool_name,
            ),
        )
        encoded_size = len(_encode(safe))
        capture = self._policy.tool_capture(tool_name)
        if capture.mode == "full_content":
            return self.bound(safe, max_bytes=max_bytes)
        if capture.mode == "metadata_only":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=encoded_size,
                reason="tool_content_metadata_only",
            )
        if capture.mode == "disabled":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=encoded_size,
                reason="tool_tracing_disabled",
            )
        paths = (
            capture.argument_paths if target == "arguments" else capture.result_paths
        )
        selected: dict[str, JsonValue] = {}
        for path in paths:
            found, selected_value = _resolve_pointer(safe, path)
            if found:
                selected[path] = selected_value
        return self.bound(selected, max_bytes=max_bytes)

    def capture_metadata_only(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
        reason: str,
    ) -> CapturedValue:
        """Redact and measure content while retaining no value bytes."""

        if not isinstance(reason, str) or not reason or len(reason) > 1024:
            raise ValueError("metadata-only capture requires a bounded reason")
        safe = self.redact(value, context=context)
        return CapturedValue(
            disposition="omitted",
            safe_size_bytes=len(_encode(safe)),
            reason=reason,
        )

    def capture_structure(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
        max_bytes: int,
    ) -> CapturedValue:
        """Redact source values before retaining bounded structural metadata."""

        _validate_max_bytes(max_bytes)
        safe = self.redact(value, context=context)
        summary: dict[str, JsonValue] = {
            "$type": "structural_metadata",
            "dataType": _json_type(safe),
            "sourceSafeSizeBytes": len(_encode(safe)),
        }
        if isinstance(safe, dict):
            summary["topLevelKeys"] = list[JsonValue](sorted(safe))
        elif isinstance(safe, list):
            summary["itemCount"] = len(safe)
        return self.bound(summary, max_bytes=max_bytes)

    @staticmethod
    def bound(value: JsonValue, *, max_bytes: int) -> CapturedValue:
        """Apply size retention to an already redacted framework-owned value."""

        _validate_max_bytes(max_bytes)
        safe = _normalize_json(value)
        encoded = _encode(safe)
        if len(encoded) > max_bytes:
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=len(encoded),
                reason="payload_too_large",
            )
        return CapturedValue(
            disposition="inline",
            safe_size_bytes=len(encoded),
            value=safe,
        )


def _validate_context_shape(
    source: JsonValue,
    safe: JsonValue,
    *,
    context: RedactionContext,
) -> None:
    """Protect only structures required for deterministic Trace interpretation."""

    if context.content_kind == "state" and not isinstance(safe, dict):
        raise TraceCaptureRejected("Trace state redaction must return an object")
    if context.content_kind == "model_request":
        _validate_model_request_shape(source, safe)
    if context.content_kind == "interaction":
        _validate_interaction_shape(source, safe)


def _validate_model_request_shape(source: JsonValue, safe: JsonValue) -> None:
    if not isinstance(source, dict) or not isinstance(safe, dict):
        raise TraceCaptureRejected("Model request redaction must return an object")
    source_messages = source.get("messages")
    safe_messages = safe.get("messages")
    if not isinstance(source_messages, list) or not isinstance(safe_messages, list):
        raise TraceCaptureRejected("Model request redaction must preserve messages")
    if len(source_messages) != len(safe_messages):
        raise TraceCaptureRejected("Model request redaction changed message order")
    for source_message, safe_message in zip(
        source_messages, safe_messages, strict=True
    ):
        if not isinstance(source_message, dict) or not isinstance(safe_message, dict):
            raise TraceCaptureRejected(
                "Model request redaction must preserve message objects"
            )
        if safe_message.get("messageType") != source_message.get("messageType"):
            raise TraceCaptureRejected("Model request redaction changed a message type")


def _validate_interaction_shape(source: JsonValue, safe: JsonValue) -> None:
    if not isinstance(source, dict):
        return
    if not isinstance(safe, dict):
        raise TraceCaptureRejected("Interaction redaction must return an object")
    source_reviews = source.get("review_configs")
    if not isinstance(source_reviews, list):
        return
    safe_reviews = safe.get("review_configs")
    if not isinstance(safe_reviews, list) or len(source_reviews) != len(safe_reviews):
        raise TraceCaptureRejected(
            "Interaction redaction changed review configuration order"
        )
    for source_review, safe_review in zip(source_reviews, safe_reviews, strict=True):
        if not isinstance(source_review, dict) or not isinstance(safe_review, dict):
            raise TraceCaptureRejected(
                "Interaction redaction must preserve review configurations"
            )
        for key in ("action_name", "allowed_decisions"):
            if safe_review.get(key) != source_review.get(key):
                raise TraceCaptureRejected(
                    "Interaction redaction changed review configuration semantics"
                )


def _encode(value: JsonValue) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise TraceCaptureRejected(
            "Trace capture requires a finite JSON value",
            diagnostic_context={"error_type": type(error).__name__},
            cause=error,
        ) from error


def _validate_max_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_bytes must be an integer")
    if value < 1:
        raise ValueError("max_bytes must be positive")


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


def _json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _resolve_pointer(value: JsonValue, path: str) -> tuple[bool, JsonValue]:
    current = value
    for raw_component in path.split("/")[1:]:
        component = raw_component.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if component not in current:
                return False, None
            current = current[component]
            continue
        valid_index = component == "0" or (
            bool(component)
            and component.isascii()
            and component[0] in "123456789"
            and component.isdecimal()
        )
        if isinstance(current, list) and valid_index:
            index = int(component)
            if index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


__all__ = [
    "CapturePolicy",
    "CapturedValue",
    "ReasoningCapturePolicy",
    "ToolCaptureRule",
    "ToolTraceCapture",
]
