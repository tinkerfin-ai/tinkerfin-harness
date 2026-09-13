"""Attachment projection composed with borrowed filesystem middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, ParamSpec, TypeVar, cast
from uuid import uuid4

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage

from tinkerfin_contracts.media import Attachment, attachment_from_block

from .media import AttachmentSupport

_MessageT = TypeVar("_MessageT", bound=BaseMessage)
_HookParams = ParamSpec("_HookParams")


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
            attachment = attachment_from_block(block)
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


class _AttachmentFilesystem(AgentMiddleware):
    """Protect attachment references while borrowing complete filesystem behavior.

    The generated class inherits the original middleware's hooks. Instance access
    delegates to the borrowed middleware so tools, mutable hook state, trace policy,
    and custom settings retain their original owner. Only model projection is local.
    Eviction updates are restored before they can reach checkpoint state.
    """

    _filesystem: AgentMiddleware[Any, Any, Any]
    _support: AttachmentSupport

    def __init__(
        self, filesystem: AgentMiddleware[Any, Any, Any], support: AttachmentSupport
    ) -> None:
        object.__setattr__(self, "_filesystem", filesystem)
        object.__setattr__(self, "_support", support)

    def __getattribute__(self, name: str) -> Any:
        # Attribute access is the untyped third-party object boundary. Its public
        # AgentMiddleware fields keep their upstream types at all consumers.
        if name in {
            "_filesystem",
            "_support",
            "__class__",
            "wrap_model_call",
            "awrap_model_call",
        }:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_filesystem"), name)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse | AIMessage:
        raise NotImplementedError("Attachment access requires async model invocation")

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse | AIMessage:
        token = uuid4().hex

        binding = self._support._reference_token.set(token)
        try:
            result = await self._filesystem.awrap_model_call(
                request.override(
                    messages=_reference_messages(
                        request.messages, shield=True, token=token
                    )
                ),
                handler,
            )
        finally:
            self._support._reference_token.reset(binding)
        if isinstance(result, ExtendedModelResponse) and result.command is not None:
            update = result.command.update
            if isinstance(update, Mapping):
                update_mapping = cast(Mapping[object, object], update)
                if isinstance(update_mapping.get("messages"), list):
                    restored = _reference_messages(
                        cast(Sequence[BaseMessage], update_mapping["messages"]),
                        shield=False,
                        token=token,
                    )
                    result = replace(
                        result,
                        command=replace(
                            result.command,
                            update={**update_mapping, "messages": restored},
                        ),
                    )
        return result


def attachment_filesystem(
    filesystem: AgentMiddleware[Any, Any, Any], support: AttachmentSupport
) -> AgentMiddleware[Any, Any, Any]:
    """Add attachment projection without changing native hooks or trace policy.

    Class inheritance preserves hook discovery, including metadata on custom
    hooks. The original instance continues to own tool closures and mutable state.
    No native method or constructor is copied or replaced.
    """
    decorated = type(
        "AttachmentFilesystem", (_AttachmentFilesystem, type(filesystem)), {}
    )
    return decorated(filesystem, support)


class _AttachmentMiddleware(AgentMiddleware):
    """Project attachments at the final destination model boundary."""

    def __init__(self, support: AttachmentSupport) -> None:
        self._support = support

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(
            request.override(
                messages=await self._support._prepare_messages(
                    request.messages, model=request.model
                )
            )
        )


__all__ = ["_AttachmentMiddleware", "attachment_filesystem"]
