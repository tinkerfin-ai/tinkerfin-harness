"""Validated models for the current Deep Agents and LangGraph v2 stream."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, cast

from langchain_core.messages import BaseMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from .errors import NativeError, NativeStreamContractError
from .json import to_json_value

NativeStreamMode = Literal[
    "values",
    "updates",
    "checkpoints",
    "tasks",
    "debug",
    "messages",
    "custom",
]
RUNTIME_INTERRUPT_SCHEMA = "tinkerfin.runtime-interrupt"


class NativeBoundaryModel(BaseModel):
    """Provide strict validation at the current upstream boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class NativeRuntimeInterrupt(NativeBoundaryModel):
    """Project one framework interrupt into stable ID and finite JSON value."""

    id: str = Field(min_length=1)
    value: JsonValue

    @model_validator(mode="before")
    @classmethod
    def normalize_interrupt(cls, value: object) -> object:
        """Extract stable fields from a mapping or framework interrupt object."""

        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            interrupt_id = mapping.get("id")
            interrupt_value = mapping.get("value")
        else:
            interrupt_id = getattr(value, "id", None)
            interrupt_value = getattr(value, "value", None)
        return {"id": interrupt_id, "value": to_json_value(interrupt_value)}


class RuntimeInterruptEnvelope(NativeBoundaryModel):
    """Describe one protocol-neutral human-input request in Native state.

    The model fixes the current serializable envelope. Producers and public
    protocol adapters remain responsible for validating ``response_schema`` with
    the JSON Schema implementation they declare as a dependency.
    """

    schema_id: Literal["tinkerfin.runtime-interrupt"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Current runtime interrupt envelope contract",
    )
    kind: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            pattern=(
                r"^(?:tool_call|input_required|confirmation|"
                r"[a-z][a-z0-9._-]*:[a-z][a-z0-9._-]*)$"
            ),
        ),
    ] = Field(description="Stable protocol-neutral interrupt reason")
    message: str | None = Field(
        default=None,
        description="Human-readable request shown by an interrupt client",
    )
    response_schema: dict[str, JsonValue] = Field(
        description="JSON Schema for the resolved response payload"
    )
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Trusted workflow metadata needed to validate a response",
    )


class NativeStreamMetadata(NativeBoundaryModel):
    """Metadata fields used to classify a Native message part."""

    model_config = ConfigDict(extra="allow")

    lc_agent_name: str | None = None
    langgraph_node: str | None = None


class NativeMessageData(NativeBoundaryModel):
    """Validated payload of a v2 messages stream part."""

    message: BaseMessage
    metadata: NativeStreamMetadata

    @field_validator("message", mode="before")
    @classmethod
    def require_live_message(cls, value: object) -> object:
        """Reject serialized data where a live LangChain message is required."""

        # LangGraph's messages handler also emits state updates from middleware,
        # including HumanMessage and RemoveMessage. These are live messages, not
        # assistant content; consumers project their own supported semantics.
        if not isinstance(value, BaseMessage):
            raise PydanticCustomError(
                "messages_native_object",
                "messages stream data must contain a live LangChain BaseMessage",
            )
        return value


class NativeMessageStreamPart(NativeBoundaryModel):
    """Validated v2 messages envelope."""

    type: Literal["messages"]
    ns: tuple[str, ...] = Field(strict=True)
    data: NativeMessageData

    @field_validator("data", mode="before")
    @classmethod
    def name_message_pair(cls, value: object) -> object:
        """Name and validate the native message-and-metadata tuple."""

        if not isinstance(value, tuple):
            raise PydanticCustomError(
                "messages_data_tuple",
                "messages stream data must be a two-item tuple",
            )
        pair = cast(tuple[object, ...], value)
        if len(pair) != 2:
            raise ValueError("messages stream data must contain message and metadata")
        return {"message": pair[0], "metadata": pair[1]}


class NativeValuesStreamPart(NativeBoundaryModel):
    """Validated v2 values envelope."""

    type: Literal["values"]
    ns: tuple[str, ...] = Field(strict=True)
    data: dict[str, object]
    interrupts: tuple[NativeRuntimeInterrupt, ...] = Field(default=(), strict=True)

    @field_validator("data", mode="before")
    @classmethod
    def require_state_mapping(cls, value: object) -> object:
        """Preserve native values while requiring a mapping boundary."""

        if not isinstance(value, Mapping):
            raise TypeError("values stream data must be a mapping")
        return dict(cast(Mapping[object, object], value))


class NativeTaskStartPayload(NativeBoundaryModel):
    """Validated start payload from the v2 tasks stream."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: object
    triggers: tuple[str, ...] = Field(strict=True)
    metadata: NativeStreamMetadata | None = None


class NativeTaskResultPayload(NativeBoundaryModel):
    """Validated result payload from the v2 tasks stream."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    error: object | None = None
    interrupts: list[object] = Field(strict=True)
    result: dict[str, object]


NativeTaskPayload = NativeTaskStartPayload | NativeTaskResultPayload


class NativeTasksStreamPart(NativeBoundaryModel):
    """Validated v2 tasks envelope."""

    type: Literal["tasks"]
    ns: tuple[str, ...] = Field(strict=True)
    data: NativeTaskPayload


class NativeExtraStreamPart(NativeBoundaryModel):
    """Validated v2 envelope for safe structural extra modes."""

    type: Literal["checkpoints", "debug", "custom"]
    ns: tuple[str, ...] = Field(strict=True)
    data: object


class NativeUpdatesStreamPart(NativeBoundaryModel):
    """Validated node-to-state update mapping."""

    type: Literal["updates"]
    ns: tuple[str, ...] = Field(strict=True)
    data: dict[str, object]

    @field_validator("data", mode="before")
    @classmethod
    def require_node_mapping(cls, value: object) -> object:
        """Require documented non-empty node-name keys."""

        if not isinstance(value, Mapping):
            raise TypeError("updates stream data must be a node mapping")
        mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) or not key for key in mapping):
            raise TypeError("updates stream node names must be non-empty strings")
        return dict(mapping)


NativeValidatedStreamPart = Annotated[
    NativeMessageStreamPart
    | NativeTasksStreamPart
    | NativeValuesStreamPart
    | NativeUpdatesStreamPart
    | NativeExtraStreamPart,
    Field(discriminator="type"),
]


class NativeStreamPartEnvelope(RootModel[NativeValidatedStreamPart]):
    """Discriminated validation boundary for one untrusted current stream part."""


def validate_native_stream_part(part: object) -> NativeValidatedStreamPart:
    """Validate one live current StreamPart before consumer state changes.

    Args:
        part: Live upstream envelope.

    Returns:
        The mode-specific validated envelope. Its fields remain mutable, and message
        objects are borrowed from the input. Consumers must treat both as read-only
        while processing the part; validation does not freeze upstream objects.

    Raises:
        NativeStreamContractError: The envelope or payload is malformed.
    """

    try:
        return NativeStreamPartEnvelope.model_validate(part).root
    except NativeError:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        mode: object | None = None
        if isinstance(part, Mapping):
            mode = cast(Mapping[object, object], part).get("type")
        context = {"mode": mode} if isinstance(mode, str) else None
        if isinstance(error, ValidationError):
            details = error.errors(include_input=False)
            message = (
                str(details[0]["msg"])
                if details
                else "Native StreamPart validation failed"
            )
        else:
            message = str(error)
        translated = NativeStreamContractError(
            message,
            context=context,
            diagnostic_context={"error_type": type(error).__name__},
            cause=error,
        )
        raise translated from error


__all__ = [
    "RUNTIME_INTERRUPT_SCHEMA",
    "NativeExtraStreamPart",
    "NativeMessageStreamPart",
    "NativeRuntimeInterrupt",
    "NativeStreamMetadata",
    "NativeStreamMode",
    "NativeTaskResultPayload",
    "NativeTaskStartPayload",
    "NativeTasksStreamPart",
    "NativeUpdatesStreamPart",
    "NativeValidatedStreamPart",
    "NativeValuesStreamPart",
    "RuntimeInterruptEnvelope",
    "validate_native_stream_part",
]
