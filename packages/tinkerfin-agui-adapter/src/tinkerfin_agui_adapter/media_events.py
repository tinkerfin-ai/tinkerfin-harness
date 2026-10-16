"""Typed durable attachment fields on AG-UI tool results and message snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypeAlias, cast

from ag_ui.core import (
    ActivityMessage,
    AssistantMessage,
    DeveloperMessage,
    Message,
    MessagesSnapshotEvent,
    ReasoningMessage,
    SystemMessage,
    ToolCallResultEvent,
    ToolMessage,
    UserMessage,
)
from pydantic import (
    Field,
    SerializeAsAny,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from tinkerfin_contracts.media import Attachment


class AttachmentToolCallResultEvent(ToolCallResultEvent):
    """Publish durable files with the text result of a completed tool call."""

    attachments: list[Attachment] = Field(default_factory=list[Attachment])


class AttachmentAssistantMessage(AssistantMessage):
    """Preserve an assistant's durable files in an authoritative message snapshot."""

    attachments: list[Attachment] = Field(default_factory=list[Attachment])


class AttachmentToolMessage(ToolMessage):
    """Preserve a tool result's durable files in an authoritative message snapshot."""

    attachments: list[Attachment] = Field(default_factory=list[Attachment])


AttachmentSnapshotMessage: TypeAlias = Annotated[
    DeveloperMessage
    | SystemMessage
    | AttachmentAssistantMessage
    | UserMessage
    | AttachmentToolMessage
    | ActivityMessage
    | ReasoningMessage,
    Field(discriminator="role"),
]
_SNAPSHOT_MESSAGES: TypeAdapter[list[AttachmentSnapshotMessage]] = TypeAdapter(
    list[AttachmentSnapshotMessage]
)


class AttachmentMessagesSnapshotEvent(MessagesSnapshotEvent):
    """Validate and serialize attachment-bearing assistant and tool messages.

    AG-UI 0.1.19 annotates snapshots with its base message union. Pydantic otherwise
    omits declared subclass fields at that boundary. Runtime serialization and the
    input schema therefore both include the public attachment message types.
    """

    messages: list[SerializeAsAny[Message]]

    @field_validator(
        "messages",
        mode="before",
        json_schema_input_type=list[AttachmentSnapshotMessage],
    )
    @classmethod
    def validate_messages(cls, value: object) -> list[Message]:
        """Validate replayed wire messages against their declared attachment fields."""
        # AG-UI replay decodes base models with extras; dump them before validating
        # so the same descriptor checks apply to live models and stored JSON.
        if isinstance(value, list):
            value = [
                item.model_dump(mode="python", by_alias=True)
                if isinstance(item, _MESSAGE_TYPES)
                else item
                for item in cast(Sequence[object], value)
            ]
        return list(_SNAPSHOT_MESSAGES.validate_python(value))

    @field_serializer("messages")
    def serialize_messages(
        self, value: list[Message]
    ) -> list[AttachmentSnapshotMessage]:
        """Publish the same typed message contract in serialization schemas."""
        return _SNAPSHOT_MESSAGES.validate_python(value)


_MESSAGE_TYPES = (
    DeveloperMessage,
    SystemMessage,
    AssistantMessage,
    UserMessage,
    ToolMessage,
    ActivityMessage,
    ReasoningMessage,
)

AttachmentOutputEvent: TypeAlias = Annotated[
    AttachmentToolCallResultEvent | AttachmentMessagesSnapshotEvent,
    Field(discriminator="type"),
]
_ATTACHMENT_OUTPUT: TypeAdapter[AttachmentOutputEvent] = TypeAdapter(
    AttachmentOutputEvent
)


def parse_attachment_output_event(value: object) -> AttachmentOutputEvent:
    """Validate a tool result or message snapshot from JSON data or AG-UI replay.

    Args:
        value: Decoded JSON object or an AG-UI tool result or snapshot event.

    Returns:
        A typed event whose attachment descriptors have been validated.

    Raises:
        pydantic.ValidationError: The event kind or attachment descriptor is invalid.
    """
    if isinstance(value, ToolCallResultEvent | MessagesSnapshotEvent):
        value = value.model_dump(mode="python", by_alias=True, serialize_as_any=True)
    return _ATTACHMENT_OUTPUT.validate_python(value)
