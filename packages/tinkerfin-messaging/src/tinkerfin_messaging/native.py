"""Optional durable codec for the canonical TinkerFin native stream schema."""

from __future__ import annotations

from typing import ClassVar

from tinkerfin_native_stream import NativeStreamPart as NativeStreamPart

from .protocols import MessageCodec, SseRenderer


class NativeStreamPartCodec(
    MessageCodec[NativeStreamPart, NativeStreamPart],
    SseRenderer[NativeStreamPart],
):
    """Persist a Driver-owned canonical TinkerFin Native replay envelope.

    The codec intentionally accepts only ``NativeStreamPart``. Live Deep Agents or
    LangGraph mappings must first pass through the selected Runtime Profile; parsing
    them here would bind Messaging to one upstream stream version.
    """

    codec_id: ClassVar[str] = "tinkerfin.native-stream"
    messaging_source_type: ClassVar[type[NativeStreamPart]] = NativeStreamPart
    messaging_replay_type: ClassVar[type[NativeStreamPart]] = NativeStreamPart

    def encode(self, item: NativeStreamPart) -> bytes:
        """Encode one immutable finite frame without reparsing an upstream object.

        Args:
            item: Canonical replay model produced by a Runtime Profile.

        Returns:
            UTF-8 JSON bytes in the current Native replay shape.

        Raises:
            TypeError: ``item`` is not a ``NativeStreamPart``.
        """

        if not isinstance(item, NativeStreamPart):
            raise TypeError("Native codec input must be a NativeStreamPart")
        return item.model_dump_json(by_alias=True).encode()

    def decode(self, payload: bytes) -> NativeStreamPart:
        """Validate one persisted native stream representation.

        Args:
            payload: UTF-8 JSON bytes previously committed under this codec ID.

        Returns:
            A new immutable canonical replay model.

        Raises:
            ValidationError: JSON or the current Native replay contract is invalid.
        """

        return NativeStreamPart.model_validate_json(payload)

    def render(self, *, seq: int, payload: NativeStreamPart) -> bytes:
        """Render one replay part as a typed SSE event.

        Args:
            seq: Positive durable Messaging sequence used as the SSE ID.
            payload: Validated canonical Native replay model.

        Returns:
            One complete UTF-8 SSE frame with ``event: stream-part``.
        """

        encoded = payload.model_dump_json(by_alias=True).encode()
        return (
            b"id: "
            + str(seq).encode()
            + b"\nevent: stream-part\ndata: "
            + encoded
            + b"\n\n"
        )
