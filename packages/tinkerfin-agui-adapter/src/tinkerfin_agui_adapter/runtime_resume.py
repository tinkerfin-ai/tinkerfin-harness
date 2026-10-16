"""Resume translation for generic runtime interrupts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Never, cast

from ag_ui.core import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from pydantic import JsonValue, ValidationError

from ._json_schema import (
    SchemaError,
    validate_json_schema_instance,
)
from ._json_schema import (
    ValidationError as JsonSchemaValidationError,
)
from .errors import AgUiAdapterErrorCode
from .models import AgentRuntimeInterrupt, JsonObject
from .reasoning import json_values_equal, normalize_operational_data
from .runtime_interrupts import (
    RuntimeInterruptEnvelope,
    parse_runtime_interrupt,
    persisted_runtime_interrupt,
)

RuntimeBatchKind = Literal["none", "runtime", "mixed"]


def classify_native_runtime_interrupts(
    interrupts: Sequence[AgentRuntimeInterrupt],
) -> RuntimeBatchKind:
    """Classify whether a native batch uses the generic runtime contract."""

    found = 0
    for item in interrupts:
        try:
            if parse_runtime_interrupt(item.value) is not None:
                found += 1
        except ValidationError as error:
            _unsupported(
                f"native runtime interrupt is invalid: {item.id}",
                cause=error,
            )
    if found == 0:
        return "none"
    return "runtime" if found == len(interrupts) else "mixed"


def classify_ag_ui_runtime_interrupts(
    interrupts: Sequence[AgUiInterrupt],
) -> RuntimeBatchKind:
    """Classify trusted persisted AG-UI runtime interrupts."""

    found = 0
    for item in interrupts:
        try:
            if persisted_runtime_interrupt(item) is not None:
                found += 1
        except (TypeError, ValueError, ValidationError) as error:
            _unsupported(
                f"persisted runtime interrupt is invalid: {item.id}",
                cause=error,
            )
    if found == 0:
        return "none"
    return "runtime" if found == len(interrupts) else "mixed"


def translate_native_runtime_resume(
    *,
    entries: Sequence[ResumeEntry],
    interrupts: Sequence[AgentRuntimeInterrupt],
):
    """Translate native generic interrupts without requiring checkpoint messages."""

    pending: dict[str, tuple[str, RuntimeInterruptEnvelope]] = {}
    for item in interrupts:
        try:
            envelope = parse_runtime_interrupt(item.value)
        except ValidationError as error:
            _unsupported(
                f"native runtime interrupt is invalid: {item.id}",
                cause=error,
            )
        if envelope is None:
            _unsupported("runtime and non-runtime interrupts cannot share a batch")
        if item.id in pending:
            _duplicate_pending(item.id)
        pending[item.id] = (item.id, envelope)
    return _translate(entries=entries, pending=pending)


def translate_ag_ui_runtime_resume(
    *,
    entries: Sequence[ResumeEntry],
    interrupts: Sequence[AgUiInterrupt],
):
    """Translate trusted persisted AG-UI generic interrupts."""

    pending: dict[str, tuple[str, RuntimeInterruptEnvelope]] = {}
    for item in interrupts:
        try:
            persisted = persisted_runtime_interrupt(item)
        except (TypeError, ValueError, ValidationError) as error:
            _unsupported(
                f"persisted runtime interrupt is invalid: {item.id}",
                cause=error,
            )
        if persisted is None:
            _unsupported("runtime and non-runtime interrupts cannot share a batch")
        envelope = persisted.envelope
        metadata = item.metadata
        if not isinstance(metadata, Mapping):
            _unsupported(f"persisted runtime interrupt has no metadata: {item.id}")
        native_value = normalize_operational_data(
            cast(Mapping[object, object], metadata).get("langgraphValue")
        )
        expected = envelope.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        if not json_values_equal(native_value, expected):
            _unsupported(f"persisted runtime interrupt value changed: {item.id}")
        if not json_values_equal(item.response_schema, envelope.response_schema):
            _unsupported(f"persisted runtime response schema changed: {item.id}")
        if item.id in pending:
            _duplicate_pending(item.id)
        pending[item.id] = (persisted.native_interrupt_id, envelope)
    return _translate(entries=entries, pending=pending)


def _translate(
    *,
    entries: Sequence[ResumeEntry],
    pending: Mapping[str, tuple[str, RuntimeInterruptEnvelope]],
):
    from .resume import ResumeMappingError, ResumeTranslation

    if not pending:
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_NO_PENDING_INTERRUPT,
            "the thread has no pending runtime interrupt to resume",
        )
    received: set[str] = set()
    cancelled: list[str] = []
    resolved: dict[str, JsonObject] = {}
    for entry in entries:
        public_id = entry.interrupt_id
        if public_id in received:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_DUPLICATE_INTERRUPT_ID,
                f"resume contains duplicate interruptId: {public_id}",
            )
        if public_id not in pending:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_UNKNOWN_INTERRUPT_ID,
                f"resume contains unknown interruptId: {public_id}",
            )
        received.add(public_id)
        if entry.status == "cancelled":
            if entry.payload is not None:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                    f"cancelled interruptId={public_id} cannot include payload",
                )
            cancelled.append(public_id)
            continue
        try:
            payload = JsonObject.model_validate(entry.payload)
            validate_json_schema_instance(
                payload.root,
                pending[public_id][1].response_schema,
            )
            resolved[public_id] = payload
        except (ValidationError, SchemaError, JsonSchemaValidationError) as error:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                f"interruptId={public_id} requires a JSON object payload",
                cause=error,
            ) from error

    missing = sorted(set(pending) - received)
    if missing:
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_INCOMPLETE,
            "resume must cover every pending interrupt; missing: " + ", ".join(missing),
        )

    native_payloads: dict[str, dict[str, JsonValue]] = {}
    decisions_by_interrupt: dict[
        str,
        tuple[dict[str, object] | None, ...],
    ] = {}
    for public_id, (native_id, _envelope) in pending.items():
        if native_id in decisions_by_interrupt:
            _unsupported(f"runtime interrupt reuses native ID: {native_id}")
        payload = None if public_id in cancelled else resolved[public_id].root
        decisions_by_interrupt[native_id] = (
            None if payload is None else cast(dict[str, object], payload),
        )
        if payload is not None:
            native_payloads[native_id] = payload

    if cancelled and len(cancelled) == len(pending):
        return ResumeTranslation(
            mode="abandon",
            kind="runtime",
            resume_data=None,
            cancelled_interrupt_ids=tuple(cancelled),
            decisions_by_interrupt=decisions_by_interrupt,
        )
    if cancelled:
        return ResumeTranslation(
            mode="custom",
            kind="runtime",
            resume_data=None,
            cancelled_interrupt_ids=tuple(cancelled),
            decisions_by_interrupt=decisions_by_interrupt,
        )

    resume_data = JsonObject.model_validate(
        next(iter(native_payloads.values()))
        if len(native_payloads) == 1
        else native_payloads
    )
    return ResumeTranslation(
        mode="command",
        kind="runtime",
        resume_data=resume_data,
        decisions_by_interrupt=decisions_by_interrupt,
    )


def _unsupported(
    message: str,
    *,
    cause: BaseException | None = None,
) -> Never:
    from .resume import ResumeMappingError

    raise ResumeMappingError(
        AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
        message,
        cause=cause,
    )


def _duplicate_pending(interrupt_id: str) -> Never:
    from .resume import ResumeMappingError

    raise ResumeMappingError(
        AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
        f"duplicate pending interruptId: {interrupt_id}",
    )


__all__ = [
    "classify_ag_ui_runtime_interrupts",
    "classify_native_runtime_interrupts",
    "translate_ag_ui_runtime_resume",
    "translate_native_runtime_resume",
]
