"""Resolve persistent attachments only for the current model invocation."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from tinkerfin_contracts.media import Attachment, attachment_from_block


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    """File bytes borrowed for one model request, with their actual MIME type.

    The host may return a bounded rendition, such as a resized image. The MIME
    type describes these bytes; the persistent Attachment still describes the
    original file. The host must bound storage reads before returning content.
    """

    data: bytes
    mime_type: str


_MessageT = TypeVar("_MessageT", bound=BaseMessage)


def _model_supports_content(model: BaseChatModel, mime_type: str) -> bool:
    profile = model.profile or {}
    if mime_type.startswith("image/"):
        return profile.get("image_inputs") is True
    if mime_type.startswith("audio/"):
        return profile.get("audio_inputs") is True
    if mime_type.startswith("video/"):
        return profile.get("video_inputs") is True
    return mime_type == "application/pdf" and profile.get("pdf_inputs") is True


def _model_content(
    attachment: Attachment, content: AttachmentContent
) -> dict[str, Any]:
    # LangChain Core's standard media blocks carry base64 and MIME type. Keep
    # attachment metadata on every request-only block so tracing can restore the
    # durable reference before any content is persisted.
    encoded = base64.b64encode(content.data).decode("ascii")
    extras = {"attachment": attachment.model_dump(mode="json")}
    if content.mime_type.startswith("image/"):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{content.mime_type};base64,{encoded}"},
            "extras": extras,
        }
    family = content.mime_type.partition("/")[0]
    return {
        "type": family if family in {"audio", "video"} else "file",
        "base64": encoded,
        "mime_type": content.mime_type,
        "extras": extras
        if family in {"audio", "video"}
        else {**extras, "filename": attachment.name},
    }


class AttachmentSupport:
    """Present authorized file content while keeping durable attachment references.

    File reads are borrowed from the host, which owns storage and authorizes each
    read. Content is resolved only for the actual destination model. Unsupported
    files remain explicit references for file-reading tools. This class does not
    parse documents, transcribe audio, or extract video frames.

    Content from tool results is presented after the complete tool batch in a
    user message, preserving assistant/tool pairing. Original messages remain
    unchanged; cancellation and authorization failures propagate.
    """

    def __init__(
        self,
        *,
        read_content: Callable[[Attachment], Awaitable[AttachmentContent]],
        supports_content: Callable[[BaseChatModel, str], bool] | None = None,
        max_attachments: int = 5,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """Configure authorized request-time file access.

        Args:
            read_content: Authorize and read bounded content. Raise FileNotFoundError
                for missing files; other errors propagate. Storage reads must be
                bounded by the host before allocating their result.
            supports_content: Optional check of the actual model and content MIME
                type. Defaults to explicit image, audio, video, and PDF input
                capabilities in the model profile. This never grants file access.
                Profiles identify media families, not every supported encoding.
                Supply this check when the adapter accepts a narrower set of formats.
                A positive result requires the model adapter to accept that format.
            max_attachments: Maximum distinct recent supported files read per call.
            max_bytes: Maximum total resolved bytes before base64 encoding per call.
                Oversized content raises ValueError before provider invocation.

        Raises:
            TypeError: Readers or capability checks are not callable, or limits
                are not integers.
            ValueError: Limits are not positive.
        """
        if not callable(read_content):
            raise TypeError("read_content must be callable")
        if supports_content is not None and not callable(supports_content):
            raise TypeError("supports_content must be callable or None")
        for name, value in (
            ("max_attachments", max_attachments),
            ("max_bytes", max_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        self._reference_token: ContextVar[str | None] = ContextVar(
            "attachment_reference_token", default=None
        )
        self._max_attachments = max_attachments
        self._max_bytes = max_bytes
        self._read_content = read_content
        self._supports_content = supports_content or _model_supports_content

    async def _prepare_messages(
        self, source: Sequence[_MessageT], *, model: BaseChatModel
    ) -> list[_MessageT | HumanMessage]:
        token = self._reference_token.get()
        if token is not None:
            from ._attachment_agents import _reference_messages

            source = _reference_messages(source, shield=False, token=token)
        capabilities: dict[str, bool] = {}

        def supports(mime_type: str) -> bool:
            if mime_type not in capabilities:
                supported = self._supports_content(model, mime_type)
                if not isinstance(supported, bool):
                    raise TypeError("supports_content must return a boolean")
                capabilities[mime_type] = supported
            return capabilities[mime_type]

        recent: dict[str, None] = {}
        for message in source:
            if isinstance(message.content, list):
                for block in message.content:
                    attachment = attachment_from_block(block)
                    if attachment is not None and supports(attachment.mime_type):
                        recent.pop(attachment.id, None)
                        recent[attachment.id] = None
        selected = set(list(recent)[-self._max_attachments :])
        messages: list[_MessageT | HumanMessage] = []
        tool_content: list[str | dict[str, Any]] = []
        resolved_ids: set[str] = set()
        total_bytes = 0
        for message in source:
            if not isinstance(message.content, list):
                messages.append(message)
                continue
            blocks: list[str | dict[str, Any]] = []
            for block in message.content:
                attachment = attachment_from_block(block)
                if attachment is None:
                    blocks.append(block)
                    continue
                caption = (
                    f"Attachment: {attachment.name}; id={attachment.id}; "
                    f"mime_type={attachment.mime_type}"
                )
                if not supports(attachment.mime_type):
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption
                            + ". Content omitted: the current model does not "
                            "support this file format. Use an available file-reading tool "
                            "to inspect its contents.",
                        }
                    )
                    continue
                if attachment.id not in selected or attachment.id in resolved_ids:
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption + ". File remains stored; use an available "
                            "file-reading tool to load it if needed.",
                        }
                    )
                    continue
                resolved_ids.add(attachment.id)
                try:
                    resolved = await self._read_content(attachment)
                except FileNotFoundError:
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption
                            + ". Original file is unavailable; ask the user "
                            "to upload it again before analyzing it.",
                        }
                    )
                    continue
                total_bytes += len(resolved.data)
                if total_bytes > self._max_bytes:
                    raise ValueError("resolved attachment content exceeds max_bytes")
                if not supports(resolved.mime_type):
                    raise ValueError(
                        "resolved attachment MIME type is not supported by the model"
                    )
                content = _model_content(attachment, resolved)
                blocks.append({"type": "text", "text": caption})
                if isinstance(message, ToolMessage):
                    tool_content.extend([{"type": "text", "text": caption}, content])
                else:
                    blocks.append(content)
            messages.append(message.model_copy(update={"content": blocks}))
        if tool_content:
            messages.append(HumanMessage(content=tool_content))
        return messages


__all__ = ["Attachment", "AttachmentContent", "AttachmentSupport"]
