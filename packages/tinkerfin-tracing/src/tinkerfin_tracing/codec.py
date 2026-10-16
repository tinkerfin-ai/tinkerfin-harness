"""Canonical opaque payload encoding shared by every durable Trace Store."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter, ValidationError

from .errors import TraceStoreProtocolError
from .facts import TRACE_FACT_ADAPTER, TraceSemanticFact

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class EncodedTracePayload:
    """Pair opaque Store bytes with their canonical pre-transform digest.

    Attributes:
        data: Bytes persisted by the Store. The base codec emits finite canonical JSON;
            a reversible storage-transform codec may emit protected bytes instead.
        digest: Lowercase SHA-256 hex digest of the finite canonical JSON before an
            optional reversible storage transform.
    """

    data: bytes
    digest: str


class CanonicalTracePayloadCodec:
    """Encode current Trace facts and cache state as canonical finite JSON.

    The digest covers canonical pre-transform bytes and remains independent of
    searchable event metadata. Reads re-encode decoded data and compare both canonical
    bytes and digest before returning a fact.

    A reversible storage-transform subclass must preserve the digest returned by the
    base encoder, reverse its transform in both decode methods, and override
    :meth:`digest` to hash the recovered canonical bytes.
    """

    def encode_fact(self, fact: TraceSemanticFact) -> EncodedTracePayload:
        """Encode one validated semantic fact without Python repr or aliases.

        Args:
            fact: Current immutable Trace fact.

        Returns:
            Canonical bytes and their pre-storage digest.

        Raises:
            TraceStoreProtocolError: The fact cannot produce finite canonical JSON.
        """

        return self.encode_json(fact.model_dump(mode="json", by_alias=True))

    def decode_fact(self, payload: bytes) -> TraceSemanticFact:
        """Decode and strictly validate one canonical semantic fact payload.

        Args:
            payload: Non-empty canonical bytes read from a Store.

        Returns:
            A newly validated immutable semantic fact.

        Raises:
            TypeError: ``payload`` is not bytes.
            ValueError: ``payload`` is empty.
            TraceStoreProtocolError: JSON, fact validation, or canonical form is invalid.
        """

        data = _required_bytes(payload)
        try:
            fact = TRACE_FACT_ADAPTER.validate_json(data, strict=True)
        except ValidationError as error:
            raise TraceStoreProtocolError(
                "Trace fact payload is invalid",
                cause=error,
            ) from error
        if self.encode_fact(fact).data != data:
            raise TraceStoreProtocolError("Trace fact payload is not canonical JSON")
        return fact

    def encode_json(self, value: JsonValue) -> EncodedTracePayload:
        """Encode one finite JSON graph with deterministic keys and whitespace.

        Args:
            value: Strict JSON value, normally a disposable Projection state.

        Returns:
            Canonical UTF-8 bytes and their pre-storage digest.

        Raises:
            TraceStoreProtocolError: The graph contains a non-JSON or non-finite value.
        """

        try:
            validated = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
            data = json.dumps(
                validated,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, ValidationError) as error:
            raise TraceStoreProtocolError(
                "Trace payload cannot be represented as finite canonical JSON",
                cause=error,
            ) from error
        return EncodedTracePayload(
            data=data,
            digest=hashlib.sha256(data).hexdigest(),
        )

    def decode_json(self, payload: bytes) -> JsonValue:
        """Decode one canonical cache payload into a fresh defensive graph.

        Args:
            payload: Non-empty canonical bytes read from a Store.

        Returns:
            A newly allocated strict finite JSON graph.

        Raises:
            TypeError: ``payload`` is not bytes.
            ValueError: ``payload`` is empty.
            TraceStoreProtocolError: JSON or canonical representation is invalid.
        """

        data = _required_bytes(payload)
        try:
            value = _JSON_VALUE_ADAPTER.validate_python(
                json.loads(
                    data,
                    parse_constant=lambda token: _raise_non_finite(token),
                ),
                strict=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise TraceStoreProtocolError(
                "Trace JSON payload is invalid",
                cause=error,
            ) from error
        if self.encode_json(value).data != data:
            raise TraceStoreProtocolError("Trace JSON payload is not canonical")
        return value

    @staticmethod
    def digest(payload: bytes) -> str:
        """Return SHA-256 for canonical bytes emitted by the base codec.

        Reversible storage-transform subclasses must override this method and hash the
        recovered canonical bytes so the digest remains independent of stored form.

        Args:
            payload: Non-empty bytes from this codec boundary.

        Returns:
            Lowercase SHA-256 hexadecimal text.

        Raises:
            TypeError: ``payload`` is not bytes.
            ValueError: ``payload`` is empty.
        """

        return hashlib.sha256(_required_bytes(payload)).hexdigest()


def _required_bytes(value: bytes) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError("payload must be bytes")
    if not value:
        raise ValueError("payload must not be empty")
    return value


def _raise_non_finite(token: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {token}")


__all__ = ["CanonicalTracePayloadCodec", "EncodedTracePayload"]
