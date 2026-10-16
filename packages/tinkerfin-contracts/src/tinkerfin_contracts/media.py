"""Persistent attachment references shared by agents and presentation adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Attachment(BaseModel):
    """Describe a durable file without embedding bytes or storage credentials.

    IDs are opaque within the host's authorization boundary. A host must authorize
    every resolution; the descriptor itself never grants access to its contents.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0, description="Original file size in bytes")

    def content_block(self) -> dict[str, JsonValue]:
        """Return a durable LangChain image/file block with host attachment metadata.

        LangChain Core's TOOL_MESSAGE_BLOCK_TYPES retains these standard types
        when a tool returns a list; unknown types are stringified instead. The
        attachment-aware middleware resolves the host file ID before model use.
        """
        return {
            "type": "image" if self.mime_type.startswith("image/") else "file",
            "file_id": self.id,
            "mime_type": self.mime_type,
            "extras": {"attachment": self.model_dump(mode="json")},
        }


def attachment_from_block(value: object) -> Attachment | None:
    """Validate a declared attachment block, leaving unrelated domain blocks alone."""
    if not isinstance(value, Mapping):
        return None
    block = cast(Mapping[object, object], value)
    if block.get("type") not in {"image", "file"}:
        return None
    extras = block.get("extras")
    if not isinstance(extras, Mapping) or "attachment" not in extras:
        return None
    attachment = Attachment.model_validate(extras["attachment"])
    if (
        block.get("file_id") != attachment.id
        or block.get("mime_type") != attachment.mime_type
        or block.get("type")
        != ("image" if attachment.mime_type.startswith("image/") else "file")
    ):
        raise ValueError("attachment source and descriptor must identify the same file")
    return attachment
