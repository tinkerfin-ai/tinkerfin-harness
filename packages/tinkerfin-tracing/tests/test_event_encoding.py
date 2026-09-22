"""SQL event reads preserve exact UTF-8 sizes and independent decoded values."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CanonicalTracePayloadCodec,
    CapturedValue,
    MessageFact,
    RunFact,
    TraceEvent,
    TraceGraphFilter,
    TraceSemanticFact,
    TraceStoreProtocolError,
)
from tinkerfin_tracing.codec import EncodedTracePayload
from tinkerfin_tracing.sql_schema import events
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore

pytestmark = pytest.mark.parametrize("trace_sql_engine", ["sqlite"], indirect=True)

_NOW = datetime(2026, 9, 21, tzinfo=UTC)
_IDENTITY = RunIdentity(namespace="encoding", thread_id="thread", run_id="run")


class _CachedProtectedCodec(CanonicalTracePayloadCodec):
    """Retain validated facts while storing a longer reversible representation."""

    def __init__(self) -> None:
        self._decoded: dict[bytes, TraceSemanticFact] = {}

    def encode_json(self, value: JsonValue) -> EncodedTracePayload:
        encoded = super().encode_json(value)
        return EncodedTracePayload(
            data=b"protected:" + base64.b64encode(encoded.data),
            digest=encoded.digest,
        )

    def decode_fact(self, payload: bytes) -> TraceSemanticFact:
        fact = self._decoded.get(payload)
        if fact is None:
            fact = CanonicalTracePayloadCodec().decode_fact(self._unprotect(payload))
            self._decoded[payload] = fact
        return fact

    def decode_json(self, payload: bytes) -> JsonValue:
        return CanonicalTracePayloadCodec().decode_json(self._unprotect(payload))

    @staticmethod
    def digest(payload: bytes) -> str:
        return CanonicalTracePayloadCodec.digest(
            _CachedProtectedCodec._unprotect(payload)
        )

    @staticmethod
    def _unprotect(payload: bytes) -> bytes:
        prefix = b"protected:"
        if not payload.startswith(prefix):
            raise ValueError("Protected payload prefix is missing")
        return base64.b64decode(payload[len(prefix) :], validate=True)


def _started() -> RunFact:
    return RunFact(
        source_observation_id="start",
        identity=_IDENTITY,
        occurred_at=_NOW,
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    )


def _message(padding: str = "") -> MessageFact:
    value: dict[str, JsonValue] = {
        "items": ["中文🙂", 'quotes"\\\n'],
        "padding": padding,
    }
    encoded = CanonicalTracePayloadCodec().encode_json(value)
    return MessageFact(
        source_observation_id="message",
        identity=_IDENTITY,
        occurred_at=_NOW,
        monotonic_ns=2,
        phase="reconciled",
        message_id="message",
        role="assistant",
        content=CapturedValue(
            disposition="inline", value=value, safe_size_bytes=len(encoded.data)
        ),
    )


def _items(event: TraceEvent) -> list[JsonValue]:
    assert isinstance(event.fact, MessageFact)
    assert event.fact.content is not None
    value = event.fact.content.value
    assert isinstance(value, dict)
    items = value["items"]
    assert isinstance(items, list)
    return items


@pytest.mark.parametrize(
    "codec_type",
    [CanonicalTracePayloadCodec, _CachedProtectedCodec],
    ids=["canonical", "cached-protected"],
)
async def test_sql_reads_keep_exact_event_bytes_and_independent_payloads(
    trace_sql_engine: AsyncEngine,
    codec_type: type[CanonicalTracePayloadCodec],
) -> None:
    codec = codec_type()
    store = SqlAlchemyTraceStore(trace_sql_engine, codec=codec)
    writer = await store.open_writer(_IDENTITY)
    fact = _message()
    try:
        committed = await writer.append((_started(), fact))
        await writer.aclose()
        for event in committed:
            assert event.persisted_bytes == len(
                event.model_dump_json(by_alias=True).encode("utf-8")
            )

        forward = await store.read_events(writer.key, after_seq=0, as_of_seq=2, limit=2)
        reverse = await store.read_events_reverse(writer.key, before_seq=3, limit=2)
        graph = await store.query_trace_graph(
            writer.key,
            run_ids=(_IDENTITY.run_id,),
            where=TraceGraphFilter(),
            limit=2,
        )
        assert forward == committed
        assert reverse == tuple(reversed(committed))
        assert len(graph.nodes) == 1
        graph_event = graph.nodes[0].result_event
        assert graph_event == committed[1]
        assert graph_event is not None

        _items(forward[1]).append("reader mutation")
        assert reverse[0] == committed[1]
        assert graph_event == committed[1]
        _items(graph_event).append("graph mutation")
        assert reverse[0] == committed[1]
        assert committed[1].fact == fact
        assert (
            await store.read_events(writer.key, after_seq=0, as_of_seq=2, limit=2)
            == committed
        )
    finally:
        await writer.aclose()


def _event_with_exact_size(template: TraceEvent, size: int) -> TraceEvent:
    padding = 0
    for _ in range(3):
        candidate = template.model_copy(
            update={"fact": _message("x" * padding), "persisted_bytes": size}
        )
        difference = size - len(candidate.model_dump_json(by_alias=True).encode())
        if difference == 0:
            return candidate
        padding += difference
    raise AssertionError("Fixture could not reach its exact event size")


@pytest.mark.parametrize("size", [999, 9999])
async def test_sql_rejects_a_larger_self_consistent_event_size(
    trace_sql_engine: AsyncEngine, size: int
) -> None:
    codec = CanonicalTracePayloadCodec()
    store = SqlAlchemyTraceStore(trace_sql_engine, codec=codec)
    writer = await store.open_writer(_IDENTITY)
    try:
        committed = await writer.append((_started(), _message()))
        await writer.aclose()
        expected = _event_with_exact_size(committed[1], size)
        payload = codec.encode_fact(expected.fact)
        async with trace_sql_engine.begin() as connection:
            await connection.execute(
                update(events)
                .where(events.c.event_id == expected.event_id)
                .values(
                    payload=payload.data,
                    payload_digest=payload.digest,
                    persisted_bytes=size,
                )
            )
        assert await store.read_events(
            writer.key, after_seq=1, as_of_seq=2, limit=1
        ) == (expected,)

        alternative = expected.model_copy(update={"persisted_bytes": size + 1})
        assert len(alternative.model_dump_json(by_alias=True).encode()) == size + 1
        async with trace_sql_engine.begin() as connection:
            await connection.execute(
                update(events)
                .where(events.c.event_id == expected.event_id)
                .values(persisted_bytes=size + 1)
            )
        with pytest.raises(TraceStoreProtocolError, match="size metadata conflicts"):
            await store.read_events(writer.key, after_seq=1, as_of_seq=2, limit=1)
    finally:
        await writer.aclose()
