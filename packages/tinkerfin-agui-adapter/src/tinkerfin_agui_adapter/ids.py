"""Reversible graph-scoped IDs shared by events and snapshots."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Literal, TypeAlias, cast

ScopedIdKind: TypeAlias = Literal[
    "message",
    "tool",
    "reasoning",
    "reasoning-message",
]

_KINDS = frozenset({"message", "tool", "reasoning", "reasoning-message"})
_PREFIX = "tf"


class ScopedIdCodec:
    """Encode complete graph namespaces and native IDs without collisions."""

    def encode(
        self,
        kind: ScopedIdKind,
        graph_namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Return an event ID safe from delimiters and cross-scope reuse."""

        self._validate(kind, graph_namespace, raw_id)
        payload = json.dumps(
            [list(graph_namespace), raw_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{_PREFIX}:{kind}:{token}"

    def decode(self, value: str) -> tuple[ScopedIdKind, tuple[str, ...], str]:
        """Decode and validate an ID produced by this codec."""

        if not isinstance(value, str):
            raise TypeError("scoped ID must be a string")
        try:
            prefix, raw_kind, token = value.split(":", 2)
        except ValueError as error:
            raise ValueError("invalid scoped ID") from error
        if prefix != _PREFIX or raw_kind not in _KINDS or not token:
            raise ValueError("invalid scoped ID")
        padding = "=" * (-len(token) % 4)
        try:
            decoded = base64.b64decode(
                token + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(decoded.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid scoped ID") from error
        if not isinstance(payload, list):
            raise ValueError(  # noqa: TRY004 - preserve the decode error contract
                "invalid scoped ID"
            )
        payload_items = cast(list[object], payload)
        if (
            len(payload_items) != 2
            or not isinstance(payload_items[0], list)
            or not isinstance(payload_items[1], str)
        ):
            raise ValueError("invalid scoped ID")
        graph_namespace = cast(
            tuple[str, ...], tuple(cast(list[object], payload_items[0]))
        )
        kind = cast(ScopedIdKind, raw_kind)
        raw_id = payload_items[1]
        self._validate(kind, graph_namespace, raw_id)
        return kind, graph_namespace, raw_id

    @staticmethod
    def _validate(
        kind: ScopedIdKind,
        graph_namespace: tuple[str, ...],
        raw_id: str,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError("unsupported scoped ID kind")
        if not isinstance(graph_namespace, tuple) or any(
            not isinstance(segment, str) or not segment for segment in graph_namespace
        ):
            raise ValueError("graph_namespace must contain only non-empty strings")
        if not isinstance(raw_id, str) or not raw_id:
            raise ValueError("raw ID must be a non-empty string")
