"""Business isolation and graph positions remain distinct on every JSON boundary."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    RunIdentity,
)
from tinkerfin_native_stream import NativeStreamPart
from tinkerfin_tracing import (
    CanonicalTracePayloadCodec,
    ModelCallFact,
    TraceGraphFilter,
    TraceStoreProtocolError,
)


def test_graph_position_round_trip_preserves_business_identity() -> None:
    identity = RunIdentity(namespace="company-a", thread_id="thread", run_id="run")
    observation = NativeMessageObservation(
        identity=identity,
        observed_at=datetime(2026, 9, 11, tzinfo=UTC),
        monotonic_ns=1,
        graph_namespace=("tools:child",),
        message=NativeMessageRecord(message_type="assistant", content="hello"),
    )
    payload = observation.model_dump(mode="json", by_alias=True)
    assert payload["identity"]["namespace"] == "company-a"
    assert payload["graphNamespace"] == ["tools:child"]
    assert "namespace" not in payload
    assert (
        NativeMessageObservation.model_validate_json(json.dumps(payload)) == observation
    )
    payload["namespace"] = payload.pop("graphNamespace")
    with pytest.raises(ValidationError, match="Extra inputs"):
        NativeMessageObservation.model_validate_json(json.dumps(payload))


def test_trace_codec_requires_current_graph_position_field() -> None:
    fact = ModelCallFact(
        source_observation_id="observed",
        identity=RunIdentity(namespace="company-a", thread_id="thread", run_id="run"),
        occurred_at=datetime(2026, 9, 11, tzinfo=UTC),
        monotonic_ns=1,
        graph_namespace=("tools:child",),
        phase="completed",
        call_id="call",
        system_message_positions=(),
        output_message_ids=(),
    )
    codec = CanonicalTracePayloadCodec()
    encoded = codec.encode_fact(fact)
    assert codec.decode_fact(encoded.data) == fact
    assert b'"graphNamespace":["tools:child"]' in encoded.data
    payload = fact.model_dump(mode="json", by_alias=True)
    payload["namespace"] = payload.pop("graphNamespace")
    with pytest.raises(TraceStoreProtocolError):
        codec.decode_fact(codec.encode_json(payload).data)


def test_graph_filter_and_native_wire_use_their_own_contracts() -> None:
    query = TraceGraphFilter(graph_namespaces={(), ("tools:child",)})
    payload = query.model_dump(mode="json", by_alias=True)
    assert "graphNamespaces" in payload
    assert "namespaces" not in payload
    assert TraceGraphFilter.model_validate_json(json.dumps(payload)) == query
    with pytest.raises(ValidationError, match="Extra inputs"):
        TraceGraphFilter.model_validate({"namespaces": [[]]})

    frame = NativeStreamPart(type="values", ns=("tools:child",), data={})
    wire = frame.model_dump(mode="json", by_alias=True)
    assert wire["ns"] == ["tools:child"]
    assert NativeStreamPart.model_validate(wire).graph_namespace == ("tools:child",)
    with pytest.raises(ValidationError, match="Extra inputs"):
        NativeStreamPart.model_validate(
            {"type": "values", "ns": [], "data": {}, "namespace": []}
        )
