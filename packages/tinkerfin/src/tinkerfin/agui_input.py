"""User input inspection and authorized attachment replacement for AG-UI hosts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from ag_ui.core import UserMessage
from ag_ui.core.types import (
    AudioInputContent,
    DocumentInputContent,
    ImageInputContent,
    InputContentPart,
    VideoInputContent,
)
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from tinkerfin_agui_adapter.media import (
    user_content_to_agui,
    user_message_to_langchain,
)
from tinkerfin_contracts.media import Attachment

__all__ = ["AgUiUserInput", "_user_messages_to_input"]


def _attachment_content(attachment: Attachment) -> dict[str, JsonValue]:
    content = user_content_to_agui([attachment.content_block()])
    assert isinstance(content, list)
    return content[0]


class AgUiUserInput(BaseModel):
    """Inspect a user submission before the host assigns its message identity.

    This model validates AG-UI content without requiring a temporary message ID.
    Attachment IDs are untrusted references. The host must authorize them and replace
    their descriptors with ``with_attachments`` before execution. No method reads files
    or grants access. Names and protocol extension fields survive replacement.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    role: Literal["user"] = "user"
    content: str | list[InputContentPart]
    name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _exclude_identity(cls, value: object) -> object:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            if "id" in mapping:
                raise ValueError("user input must not contain a message ID")
            return mapping
        return value

    @property
    def text(self) -> str:
        """Return visible text in content order without attachment descriptors."""
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content if part.type == "text")

    @property
    def attachment_ids(self) -> tuple[str, ...]:
        """Return durable file references in content order, without authorizing them."""
        if isinstance(self.content, str):
            return ()
        return tuple(
            part.source.value.removeprefix("attachment:")
            for part in self.content
            if isinstance(
                part,
                (
                    ImageInputContent,
                    AudioInputContent,
                    VideoInputContent,
                    DocumentInputContent,
                ),
            )
            and part.source.type == "url"
            and part.source.value.startswith("attachment:")
        )

    @property
    def attachments(self) -> tuple[Attachment, ...]:
        """Validate and return descriptors matching all durable file references.

        Descriptor validation does not prove ownership. Only descriptors replaced by
        a trusted host repository may be used for business binding or file resolution.

        Raises:
            ValueError: A descriptor is missing or disagrees with its source or kind.
        """
        if isinstance(self.content, str):
            return ()
        attachments: list[Attachment] = []
        for part in self.content:
            if (
                isinstance(
                    part,
                    (
                        ImageInputContent,
                        AudioInputContent,
                        VideoInputContent,
                        DocumentInputContent,
                    ),
                )
                and part.source.type == "url"
                and part.source.value.startswith("attachment:")
            ):
                attachment = Attachment.model_validate(part.metadata)
                if (
                    part.source.value != f"attachment:{attachment.id}"
                    or part.type != _attachment_content(attachment)["type"]
                ):
                    raise ValueError(
                        "attachment source and descriptor must identify the same file"
                    )
                attachments.append(attachment)
        return tuple(attachments)

    def with_attachments(self, attachments: Sequence[Attachment]) -> AgUiUserInput:
        """Return a submission containing the host's authorized file descriptors.

        Args:
            attachments: Authorized descriptors covering exactly the referenced IDs.
                Storage, access checks, and tenant isolation remain host-owned.

        Returns:
            A newly validated submission retaining text, ordering, and extension fields.

        Raises:
            ValueError: Descriptors are duplicated or do not cover the submitted IDs.
        """
        by_id = {item.id: item for item in attachments}
        if len(by_id) != len(attachments) or set(by_id) != set(self.attachment_ids):
            raise ValueError(
                "authorized attachments must exactly cover the submitted IDs"
            )
        payload = self.model_dump(mode="json", by_alias=True)
        if not isinstance(self.content, str):
            parts: list[dict[str, JsonValue]] = []
            for part in self.content:
                if (
                    isinstance(
                        part,
                        (
                            ImageInputContent,
                            AudioInputContent,
                            VideoInputContent,
                            DocumentInputContent,
                        ),
                    )
                    and part.source.type == "url"
                    and part.source.value.startswith("attachment:")
                ):
                    attachment = by_id[part.source.value.removeprefix("attachment:")]
                    canonical = _attachment_content(attachment)
                    canonical_source = canonical["source"]
                    assert isinstance(canonical_source, dict)
                    parts.append(
                        {
                            **part.model_dump(mode="json", by_alias=True),
                            **canonical,
                            "source": {
                                **part.source.model_dump(mode="json", by_alias=True),
                                **canonical_source,
                            },
                        }
                    )
                else:
                    parts.append(part.model_dump(mode="json", by_alias=True))
            payload["content"] = parts
        return type(self).model_validate(payload)


def _user_messages_to_input(
    messages: Sequence[UserMessage | Mapping[str, object]],
) -> InputAgentState:
    """Validate authoritative message identities before native graph construction."""
    if isinstance(messages, (str, bytes)) or not messages:
        raise ValueError("messages must contain at least one user message")
    converted: list[AnyMessage | dict[str, Any]] = []
    ids: set[str] = set()
    for value in messages:
        message = UserMessage.model_validate(value)
        if (
            not message.id.strip()
            or message.id != message.id.strip()
            or message.id in ids
        ):
            raise ValueError("message IDs must be nonblank, canonical, and distinct")
        ids.add(message.id)
        converted.append(user_message_to_langchain(message))
    return InputAgentState(messages=converted)
