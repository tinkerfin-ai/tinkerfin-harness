"""Request media projection composed with borrowed Deep Agents middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, TypeVar, cast
from uuid import uuid4

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from tinkerfin_contracts.media import Attachment

from .media import AttachmentSupport, _content_attachment, _media_input

_MessageT = TypeVar("_MessageT", bound=BaseMessage)


def _reference_messages(
    messages: Sequence[_MessageT], *, shield: bool, token: str
) -> list[_MessageT]:
    """Keep references outside native media scrubbing and text eviction payloads."""
    result: list[_MessageT] = []
    for message in messages:
        if isinstance(message.content, str):
            result.append(message)
            continue
        blocks: list[str | dict[Any, Any]] = []
        for block in message.content:
            if not shield and isinstance(block, dict):
                extras = block.get("extras")
                if (
                    isinstance(extras, dict)
                    and cast(Mapping[object, object], extras).get(
                        "tinkerfin_attachment_caption"
                    )
                    == token
                ):
                    continue
            attachment = _content_attachment(block)
            if (
                not shield
                and isinstance(block, dict)
                and block.get("type") == "non_standard"
            ):
                value = block.get("value")
                if (
                    isinstance(value, dict)
                    and cast(Mapping[object, object], value).get("token") == token
                    and "tinkerfin_attachment" in value
                ):
                    attachment = Attachment.model_validate(
                        value["tinkerfin_attachment"]
                    )
            if attachment is None:
                blocks.append(block)
            elif shield:
                blocks.append(
                    {
                        "type": "text",
                        "text": f"Attachment: {attachment.name}; id={attachment.id}; mime_type={attachment.mime_type}",
                        "extras": {"tinkerfin_attachment_caption": token},
                    }
                )
                blocks.append(
                    {
                        "type": "non_standard",
                        "value": {
                            "token": token,
                            "tinkerfin_attachment": attachment.model_dump(mode="json"),
                        },
                    }
                )
            else:
                blocks.append(attachment.content_block())
        result.append(message.model_copy(update={"content": blocks}))
    return result


def _native_messages(
    messages: Sequence[_MessageT],
    *,
    shield: bool,
    media: dict[str, dict[str, Any]],
    token: str,
) -> list[_MessageT]:
    """Hide native media only while one middleware prepares its request.

    Deep Agents 0.7.13's system-prompt helpers normalize source formats, and its
    filesystem scrubs before downstream model routing. Keep original blocks in
    this call's local mapping, never inside placeholders. Restore only surviving
    placeholders, carrying prompt-cache changes made during preparation.

    Args:
        messages: Messages entering or leaving one middleware's preparation.
        shield: Whether to replace media with local references or restore it.
        media: Original blocks owned by this model-wrapper invocation.
        token: Identifier separating this invocation's references from content.

    Returns:
        Copies containing protected or restored media without modifying history.
    """
    result: list[_MessageT] = []
    for message in messages:
        if isinstance(message.content, str):
            result.append(message)
            continue
        blocks: list[str | dict[str, Any]] = []
        for block in message.content:
            if shield:
                if _media_input(block) is not None:
                    assert isinstance(block, dict)
                    key = f"{token}:{len(media)}"
                    media[key] = block
                    blocks.append(
                        {
                            "type": "non_standard",
                            "value": {"tinkerfin_native_media": key},
                            **(
                                {"cache_control": block["cache_control"]}
                                if "cache_control" in block
                                else {}
                            ),
                        }
                    )
                    continue
            elif isinstance(block, dict) and block.get("type") == "non_standard":
                value = block.get("value")
                if isinstance(value, dict):
                    key = cast(Mapping[object, object], value).get(
                        "tinkerfin_native_media"
                    )
                    if isinstance(key, str) and key in media:
                        # Core drops top-level metadata from non_standard blocks.
                        # Absence after prompt preparation therefore cannot mean
                        # that the original cache control was intentionally removed.
                        restored = dict(media[key])
                        if "cache_control" in block:
                            restored["cache_control"] = block["cache_control"]
                        blocks.append(restored)
                        continue
            blocks.append(block)
        result.append(message.model_copy(update={"content": blocks}))
    return result


class _MediaPreparation(AgentMiddleware):
    """Preserve media formats while borrowing a middleware's complete behavior.

    The generated class inherits the original middleware's hooks. Instance access
    delegates to the borrowed middleware so tools, mutable hook state, trace policy,
    and custom settings retain their original owner. Only model projection is local.
    Eviction updates are restored before they can reach checkpoint state.
    """

    _middleware: AgentMiddleware[Any, Any, Any]
    _support: AttachmentSupport
    _protect_messages: bool

    def __init__(
        self,
        middleware: AgentMiddleware[Any, Any, Any],
        support: AttachmentSupport,
        *,
        protect_messages: bool,
    ) -> None:
        object.__setattr__(self, "_middleware", middleware)
        object.__setattr__(self, "_support", support)
        object.__setattr__(self, "_protect_messages", protect_messages)

    def __getattribute__(self, name: str) -> Any:
        # Attribute access is the untyped third-party object boundary. Its public
        # AgentMiddleware fields keep their upstream types at all consumers.
        if name in {
            "_middleware",
            "_support",
            "_protect_messages",
            "__class__",
            "wrap_model_call",
            "awrap_model_call",
        }:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_middleware"), name)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse | AIMessage:
        raise NotImplementedError("Media projection requires async model invocation")

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse | AIMessage:
        token = uuid4().hex
        media: dict[str, dict[str, Any]] = {}

        def system_message(
            value: SystemMessage | None, *, shield: bool
        ) -> SystemMessage | None:
            if value is None:
                return None
            return _native_messages([value], shield=shield, media=media, token=token)[0]

        async def restore_native(prepared: ModelRequest) -> ModelResponse:
            return await handler(
                prepared.override(
                    system_message=system_message(
                        prepared.system_message, shield=False
                    ),
                    messages=_native_messages(
                        prepared.messages, shield=False, media=media, token=token
                    )
                    if self._protect_messages
                    else prepared.messages,
                )
            )

        binding = (
            self._support._reference_token.set(token)
            if self._protect_messages
            else None
        )
        try:
            result = await self._middleware.awrap_model_call(
                request.override(
                    system_message=system_message(request.system_message, shield=True),
                    messages=_native_messages(
                        _reference_messages(request.messages, shield=True, token=token),
                        shield=True,
                        media=media,
                        token=token,
                    )
                    if self._protect_messages
                    else request.messages,
                ),
                restore_native,
            )
            if isinstance(result, ExtendedModelResponse) and result.command is not None:
                update = result.command.update
                if isinstance(update, Mapping):
                    update_mapping = cast(Mapping[object, object], update)
                    if isinstance(update_mapping.get("messages"), list):
                        restored = _native_messages(
                            cast(Sequence[BaseMessage], update_mapping["messages"]),
                            shield=False,
                            media=media,
                            token=token,
                        )
                        if self._protect_messages:
                            restored = _reference_messages(
                                restored, shield=False, token=token
                            )
                        result = replace(
                            result,
                            command=replace(
                                result.command,
                                update={**update_mapping, "messages": restored},
                            ),
                        )
            return result
        finally:
            if binding is not None:
                self._support._reference_token.reset(binding)
            media.clear()


def preserve_media(
    middleware: AgentMiddleware[Any, Any, Any],
    support: AttachmentSupport,
    *,
    protect_messages: bool = False,
) -> AgentMiddleware[Any, Any, Any]:
    """Preserve request media without changing native hooks or trace policy.

    Class inheritance preserves hook discovery, including metadata on custom
    hooks. The original instance continues to own tool closures and mutable state.
    No native method or constructor is copied or replaced.

    Args:
        middleware: Borrowed Deep Agents middleware that prepares system content.
        support: Attachment policy shared with the final model projection.
        protect_messages: Also protect history media and attachment references
            while filesystem middleware scrubs or evicts message content.

    Returns:
        Middleware retaining the borrowed instance's hooks and ownership.
    """
    decorated = type("MediaPreparation", (_MediaPreparation, type(middleware)), {})
    return decorated(middleware, support, protect_messages=protect_messages)


class _AttachmentMiddleware(AgentMiddleware):
    """Project message and system media at the final destination model boundary."""

    def __init__(self, support: AttachmentSupport) -> None:
        self._support = support

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        source = (
            request.messages
            if request.system_message is None
            else [request.system_message, *request.messages]
        )
        messages = await self._support._prepare_messages(source, model=request.model)
        if request.system_message is None:
            return await handler(request.override(messages=messages))
        system_message = messages[0]
        if not isinstance(system_message, SystemMessage):
            raise TypeError("media projection must preserve the system message")
        return await handler(
            request.override(system_message=system_message, messages=messages[1:])
        )


__all__ = ["_AttachmentMiddleware", "preserve_media"]
