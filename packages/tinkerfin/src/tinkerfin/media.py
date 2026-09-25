"""Present media to the selected model without rewriting conversation history."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, TypeVar, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage

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
_MEDIA_TYPES = frozenset({"image", "audio", "video", "file"})
_MEDIA_INPUT_TYPES = _MEDIA_TYPES | {
    "image_url",
    "input_image",
    "input_audio",
    "input_file",
    "document",
}


@dataclass(frozen=True, slots=True)
class _NativeMedia:
    content: dict[str, Any] | None
    mime_type: str


def _media_input(block: str | dict[str, Any]) -> dict[str, Any] | None:
    # Core 1.6.1's BaseMessage.content_blocks unwraps non_standard.value before
    # its provider translators. Inspect that declared wrapper only, never domain
    # payloads, and use the same boundary for attachment authorization and media.
    if not isinstance(block, dict):
        return None
    source = block
    if block.get("type") == "non_standard":
        value = block.get("value")
        if not isinstance(value, dict):
            return None
        source = cast(dict[str, Any], value)
    kind = source.get("type")
    if not isinstance(kind, str) or kind not in _MEDIA_INPUT_TYPES:
        return None
    if source is not block and "cache_control" in block:
        return {**source, "cache_control": block["cache_control"]}
    return source


def _content_attachment(block: str | dict[str, Any]) -> Attachment | None:
    return attachment_from_block(_media_input(block))


def _data_mime(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    separator = value.find(",")
    if separator == -1:
        return None
    mime_type = value[5:separator].partition(";")[0]
    return mime_type if "/" in mime_type else None


def _source_mime(block: dict[str, Any]) -> str | None:
    for field in ("url", "file_data", "image_url"):
        if mime_type := _data_mime(block.get(field)):
            return mime_type
    for field in ("file", "image_url", "source"):
        source = block.get(field)
        if not isinstance(source, dict):
            continue
        source_mapping = cast(Mapping[object, object], source)
        for name in ("url", "file_data"):
            if mime_type := _data_mime(source_mapping.get(name)):
                return mime_type
        media_type = source_mapping.get("media_type")
        if isinstance(media_type, str) and media_type:
            return media_type
    mime_type = block.get("mime_type")
    return mime_type if isinstance(mime_type, str) and mime_type else None


def _native_media(block: str | dict[str, Any]) -> _NativeMedia | None:
    """Recognize standard media and the provider input forms Core translates.

    Keep unrelated domain blocks unchanged. Core's content translators normalize
    data URLs and provider-specific sources without fetching content. An opaque
    file does not establish a PDF MIME type: Core 1.6.1's OpenAI file translator
    labels every inline file as PDF. Retain the source MIME instead. Missing
    image/audio/video formats use a media range.

    Args:
        block: A standard or provider media block, optionally inside Core's
            non_standard.value wrapper.

    Returns:
        The request content and its source MIME type, or None for unrelated
        blocks. A recognized block without usable media has content=None.
    """
    source = _media_input(block)
    if source is None:
        return None
    block = source
    kind = block.get("type")
    translated = block
    if kind == "input_image" and isinstance(block.get("image_url"), str):
        translated = {
            "type": "image_url",
            "image_url": {
                "url": block["image_url"],
                **({"detail": block["detail"]} if "detail" in block else {}),
            },
        }
    elif kind == "input_image" and isinstance(block.get("file_id"), str):
        translated = {**block, "type": "image"}
    elif kind == "input_file":
        translated = {
            "type": "file",
            "file": {
                name: block[name]
                for name in ("file_id", "file_data", "filename")
                if name in block
            },
        }
        if isinstance(block.get("file_url"), str):
            translated = {"type": "file", "url": block["file_url"]}
    normalized = HumanMessage(content=[translated]).content_blocks
    content = next((item for item in normalized if item["type"] in _MEDIA_TYPES), None)
    if content is None:
        return _NativeMedia(None, "application/octet-stream")
    native = cast(dict[str, Any], content)
    if not any(
        isinstance(native.get(field), str) and native[field]
        for field in ("base64", "url", "file_id")
    ):
        return _NativeMedia(None, "application/octet-stream")
    mime_type = _source_mime(block)
    if mime_type is None and native["type"] != "file":
        mime_type = native.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = (
            "application/octet-stream"
            if native["type"] == "file"
            else f"{native['type']}/*"
        )
    return _NativeMedia({**native, "mime_type": mime_type}, mime_type)


def _native_caption(message: BaseMessage, media: _NativeMedia) -> str:
    path = message.additional_kwargs.get("read_file_path")
    if not isinstance(path, str) and media.content is not None:
        path = media.content.get("filename")
        extras = media.content.get("extras")
        if not isinstance(path, str) and isinstance(extras, dict):
            path = cast(Mapping[object, object], extras).get("filename")
    label = path if isinstance(path, str) and path else "inline content"
    return f"Media: {label}; mime_type={media.mime_type}; source={message.type}"


def _unsupported_content(caption: str) -> dict[str, str]:
    return {
        "type": "text",
        "text": caption
        + ". Content omitted: direct input of this file format is not enabled "
        "for the current model. Use an available file-reading tool "
        "to inspect its contents.",
    }


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
    """Configure model media inputs and optional authorized attachment reads.

    File reads are borrowed from the host, which owns storage and authorizes each
    read. Content is resolved only for the actual destination model. Unsupported
    files remain explicit references for file-reading tools. This class does not
    parse documents, transcribe audio, or extract video frames.

    The same capability check applies to native media in messages and tool results.
    Media in non-user messages is presented in a request-only user message after
    the complete history, preserving assistant/tool pairing. Original messages
    remain unchanged; cancellation and authorization failures propagate.
    """

    def __init__(
        self,
        *,
        read_content: Callable[[Attachment], Awaitable[AttachmentContent]]
        | None = None,
        supports_content: Callable[[BaseChatModel, str], bool] | None = None,
        max_attachments: int = 5,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """Configure authorized request-time file access.

        Args:
            read_content: Optional authorized reader for persistent Attachment IDs.
                Native media needs no reader. Without one, Attachment IDs remain
                descriptions for available file tools. Raise FileNotFoundError
                for missing files; other errors propagate. Storage reads must be
                bounded by the host before allocating their result.
            supports_content: Optional check of the actual model and content MIME
                type for both native media and persistent attachments. A native
                image, audio, or video URL without a format uses its media range
                (for example, image/*); an untyped file uses application/octet-stream.
                Defaults to explicit image, audio, video, and PDF input capabilities
                in the model profile; unknown capabilities do not send media.
                Hosts own the accuracy of profile declarations for their endpoints.
                This never grants file access.
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
        if read_content is not None and not callable(read_content):
            raise TypeError("read_content must be callable or None")
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
                    attachment = _content_attachment(block)
                    if attachment is not None and supports(attachment.mime_type):
                        recent.pop(attachment.id, None)
                        recent[attachment.id] = None
        selected = set(list(recent)[-self._max_attachments :])
        messages: list[_MessageT | HumanMessage] = []
        user_media: list[str | dict[str, Any]] = []
        resolved_ids: set[str] = set()
        total_bytes = 0
        for message in source:
            if not isinstance(message.content, list):
                messages.append(message)
                continue
            blocks: list[str | dict[str, Any]] = []
            for block in message.content:
                attachment = _content_attachment(block)
                if attachment is None:
                    media = _native_media(block)
                    if media is None:
                        blocks.append(block)
                        continue
                    caption = _native_caption(message, media)
                    if media.content is None or not supports(media.mime_type):
                        blocks.append(_unsupported_content(caption))
                    else:
                        content = media.content
                        path = message.additional_kwargs.get("read_file_path")
                        if content["type"] == "file" and isinstance(path, str):
                            content = {**content, "filename": PurePosixPath(path).name}
                        if isinstance(message, HumanMessage):
                            blocks.append(content)
                        else:
                            blocks.append({"type": "text", "text": caption})
                            user_media.extend(
                                [{"type": "text", "text": caption}, content]
                            )
                    continue
                caption = (
                    f"Attachment: {attachment.name}; id={attachment.id}; "
                    f"mime_type={attachment.mime_type}"
                )
                if not supports(attachment.mime_type):
                    blocks.append(_unsupported_content(caption))
                    continue
                if self._read_content is None:
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption + ". Content was not loaded: this runtime "
                            "has no authorized attachment reader. Use an available "
                            "file-reading tool to inspect the stored file.",
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
                if isinstance(message, HumanMessage):
                    blocks.append(content)
                else:
                    user_media.extend([{"type": "text", "text": caption}, content])
            messages.append(message.model_copy(update={"content": blocks}))
        if user_media:
            messages.append(HumanMessage(content=user_media))
        return messages


__all__ = ["Attachment", "AttachmentContent", "AttachmentSupport"]
