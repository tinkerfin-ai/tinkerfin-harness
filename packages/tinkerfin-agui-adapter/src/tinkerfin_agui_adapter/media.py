"""AG-UI attachment extensions and multimodal input conversion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from ag_ui.core import UserMessage
from ag_ui.core.types import (
    AudioInputContent,
    DocumentInputContent,
    ImageInputContent,
    VideoInputContent,
)
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tinkerfin_contracts.media import Attachment, attachment_from_block


def _attachment_input_type(
    attachment: Attachment,
) -> Literal["image", "audio", "video", "document"]:
    """Return the AG-UI fragment type for a durable file descriptor.

    AG-UI uses document fragments for general files; this wire representation
    does not assert that the file is a document or that a model can read it.
    """
    family = attachment.mime_type.partition("/")[0]
    if family == "image":
        return "image"
    if family == "audio":
        return "audio"
    if family == "video":
        return "video"
    return "document"


class MessageAttachments(BaseModel):
    """Describe attachment additions within an assistant message lifecycle.

    Emit as CUSTOM ``tinkerfin.message.attachments`` between TEXT_MESSAGE_START
    and TEXT_MESSAGE_END. The scoped message ID and rawEvent provenance match
    the surrounding text events. Repeated file IDs identify the same attachment.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    message_id: str = Field(alias="messageId", min_length=1)
    attachments: list[Attachment] = Field(min_length=1)


def content_attachments(content: object) -> list[dict[str, JsonValue]]:
    """Extract validated attachment descriptors in message order."""
    if not isinstance(content, list):
        return []
    result: list[dict[str, JsonValue]] = []
    for block in cast(Sequence[object], content):
        attachment = attachment_from_block(block)
        if attachment is not None:
            result.append(attachment.model_dump(mode="json"))
    return result


def user_message_to_langchain(message: UserMessage) -> HumanMessage:
    """Convert AG-UI input while retaining durable file references.

    A source URI ``attachment:<id>`` requires an Attachment descriptor in metadata.
    The host must validate access before invoking an agent. Other source URIs are
    standard provider inputs; this conversion never fetches URLs or grants access.
    """
    if isinstance(message.content, str):
        return HumanMessage(content=message.content, id=message.id, name=message.name)
    blocks: list[str | dict[str, JsonValue]] = []
    for part in message.content:
        if part.type == "text":
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(
            part,
            (
                ImageInputContent,
                AudioInputContent,
                VideoInputContent,
                DocumentInputContent,
            ),
        ):
            source = part.source
            if source.type == "url" and source.value.startswith("attachment:"):
                attachment = Attachment.model_validate(part.metadata)
                if (
                    source.value != f"attachment:{attachment.id}"
                    or part.type != _attachment_input_type(attachment)
                ):
                    raise ValueError(
                        "attachment source and metadata must identify the same file"
                    )
                blocks.append(attachment.content_block())
            elif part.type == "image":
                url = (
                    source.value
                    if source.type == "url"
                    else f"data:{source.mime_type};base64,{source.value}"
                )
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            else:
                raise ValueError("documents require a durable attachment reference")
        else:
            raise ValueError(f"unsupported input content: {part.type}")
    return HumanMessage(content=blocks, id=message.id, name=message.name)


def user_content_to_agui(content: object) -> str | list[dict[str, JsonValue]]:
    """Project durable user blocks into standard AG-UI media fragments."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError("user content must be text or a list of content blocks")
    result: list[dict[str, JsonValue]] = []
    for block in cast(Sequence[object], content):
        attachment = attachment_from_block(block)
        mapping: Mapping[object, object] = (
            cast(Mapping[object, object], block) if isinstance(block, dict) else {}
        )
        if attachment is not None:
            result.append(
                {
                    "type": _attachment_input_type(attachment),
                    "source": {
                        "type": "url",
                        "value": f"attachment:{attachment.id}",
                        "mimeType": attachment.mime_type,
                    },
                    "metadata": attachment.model_dump(mode="json"),
                }
            )
        elif isinstance(block, str):
            result.append({"type": "text", "text": block})
        elif (
            isinstance(block, dict)
            and mapping.get("type") == "text"
            and isinstance(text := mapping.get("text"), str)
        ):
            result.append({"type": "text", "text": text})
        elif isinstance(block, dict) and mapping.get("type") == "image_url":
            image_url = mapping.get("image_url")
            url = (
                cast(Mapping[object, object], image_url).get("url")
                if isinstance(image_url, dict)
                else image_url
            )
            if not isinstance(url, str):
                raise ValueError("image_url blocks require a URL")
            result.append({"type": "image", "source": {"type": "url", "value": url}})
        else:
            raise ValueError("user snapshot contains unsupported content")
    return result
