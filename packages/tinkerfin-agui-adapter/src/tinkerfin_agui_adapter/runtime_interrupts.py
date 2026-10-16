"""Generic runtime interrupts that map losslessly to AG-UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from ag_ui.core import Interrupt as AgUiInterrupt
from pydantic import Field, JsonValue, field_validator

from tinkerfin_native_stream import (
    RUNTIME_INTERRUPT_SCHEMA,
)
from tinkerfin_native_stream import (
    RuntimeInterruptEnvelope as NativeRuntimeInterruptEnvelope,
)

from ._json_schema import SchemaError, require_valid_schema
from .models import AgentRuntimeInterrupt, RuntimeModel
from .reasoning import sanitize_public_data

_RUNTIME_INTERRUPT_SCHEMA_FAMILY = f"{RUNTIME_INTERRUPT_SCHEMA}."


class RuntimeInterruptEnvelope(NativeRuntimeInterruptEnvelope):
    """Validate a Native human-input request before AG-UI publication."""

    @field_validator("response_schema")
    @classmethod
    def response_schema_is_valid(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Reject malformed response contracts before publishing an interrupt."""

        try:
            require_valid_schema(value)
        except SchemaError as error:
            raise ValueError("response_schema is not valid") from error
        return value


class PersistedRuntimeInterrupt(RuntimeModel):
    """Public correlation persisted in an AG-UI interrupt for a later request."""

    schema_id: Literal["tinkerfin.runtime-interrupt"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Current persisted runtime correlation contract",
    )
    native_interrupt_id: str = Field(
        min_length=1,
        description="Original LangGraph interrupt ID",
    )
    envelope: RuntimeInterruptEnvelope = Field(
        description="Runtime interrupt with provider-private metadata removed"
    )


def parse_runtime_interrupt(value: JsonValue) -> RuntimeInterruptEnvelope | None:
    """Parse a declared runtime envelope while leaving unrelated values untouched."""

    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    schema = mapping.get("schema")
    if not isinstance(schema, str) or (
        schema != RUNTIME_INTERRUPT_SCHEMA
        and not schema.startswith(_RUNTIME_INTERRUPT_SCHEMA_FAMILY)
    ):
        return None
    return RuntimeInterruptEnvelope.model_validate(value)


def prepare_runtime_ag_ui_interrupt(
    native: AgentRuntimeInterrupt,
    envelope: RuntimeInterruptEnvelope,
    *,
    source: Mapping[str, JsonValue],
) -> AgUiInterrupt:
    """Project one validated runtime envelope to one resumable AG-UI interrupt."""

    published = envelope.model_copy(deep=True)
    require_valid_schema(published.response_schema)
    public_value = sanitize_public_data(
        published.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    if not isinstance(public_value, dict):
        raise TypeError("runtime interrupt envelope must serialize to an object")
    correlation = PersistedRuntimeInterrupt(
        native_interrupt_id=native.id,
        envelope=published,
    )
    # Both serialized copies reach clients and durable AG-UI replay. The resume
    # correlation must obey the same privacy boundary as the displayed envelope.
    # test_interleaved_public_stream.py verifies SSE, persistence, and resume together.
    metadata = sanitize_public_data(
        {
            "langgraphValue": public_value,
            "source": dict(source),
            "runtimeInterrupt": correlation.model_dump(
                mode="json", by_alias=True, exclude_none=False
            ),
        }
    )
    if not isinstance(metadata, dict):
        raise TypeError("runtime interrupt metadata must serialize to an object")
    return AgUiInterrupt(
        id=native.id,
        reason=published.kind,
        message=published.message,
        response_schema=published.response_schema,
        metadata=metadata,
    )


def persisted_runtime_interrupt(
    interrupt: AgUiInterrupt,
) -> PersistedRuntimeInterrupt | None:
    """Read trusted runtime correlation from a persisted AG-UI interrupt."""

    metadata = interrupt.metadata
    if not isinstance(metadata, Mapping):
        return None
    raw = cast(Mapping[object, object], metadata).get("runtimeInterrupt")
    if raw is None:
        return None
    persisted = PersistedRuntimeInterrupt.model_validate(raw)
    if interrupt.id != persisted.native_interrupt_id:
        raise ValueError("persisted runtime interrupt ID does not match AG-UI ID")
    if interrupt.reason != persisted.envelope.kind:
        raise ValueError("persisted runtime interrupt kind does not match AG-UI reason")
    return persisted


__all__ = [
    "RUNTIME_INTERRUPT_SCHEMA",
    "PersistedRuntimeInterrupt",
    "RuntimeInterruptEnvelope",
]
