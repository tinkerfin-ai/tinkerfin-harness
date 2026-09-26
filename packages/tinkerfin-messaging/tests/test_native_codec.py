"""Canonical TinkerFin Native replay codec contracts."""

from __future__ import annotations

import json
from typing import Any, get_type_hints

import pytest
from pydantic import JsonValue, ValidationError

from tinkerfin import NativeStreamPart
from tinkerfin_messaging import NativeStreamPartCodec
from tinkerfin_native_stream import NativeStreamMode


def _part(
    *,
    mode: NativeStreamMode = "values",
    data: JsonValue | None = None,
) -> NativeStreamPart:
    return NativeStreamPart(
        type=mode,
        ns=("child:task-1",),
        data={"value": 1} if data is None else data,
    )


def test_native_codec_accepts_only_the_canonical_replay_model() -> None:
    hints = get_type_hints(NativeStreamPartCodec.encode)

    assert hints["item"] is NativeStreamPart
    assert NativeStreamPartCodec.messaging_source_type is NativeStreamPart
    assert NativeStreamPartCodec.messaging_replay_type is NativeStreamPart


def test_native_codec_exposes_the_current_tinkerfin_schema_id() -> None:
    assert NativeStreamPartCodec.codec_id == "tinkerfin.native-stream"


@pytest.mark.parametrize(
    "mode",
    [
        "messages",
        "tasks",
        "values",
        "updates",
        "checkpoints",
        "debug",
        "custom",
    ],
)
def test_native_codec_round_trips_every_canonical_mode(
    mode: NativeStreamMode,
) -> None:
    codec = NativeStreamPartCodec()
    part = _part(mode=mode, data={"mode": mode})

    decoded = codec.decode(codec.encode(part))

    assert decoded == part
    assert decoded.mode == mode
    assert decoded.graph_namespace == ("child:task-1",)
    assert decoded.data == {"mode": mode}
    assert decoded.interrupts == ()


def test_native_codec_does_not_accept_a_live_upstream_mapping() -> None:
    codec = NativeStreamPartCodec()
    raw: Any = {
        "type": "values",
        "ns": (),
        "data": {"answer": 42},
    }

    with pytest.raises(TypeError, match="NativeStreamPart"):
        codec.encode(raw)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"type":"unknown","ns":[],"data":null,"interrupts":[]}',
        b'{"type":"values","ns":[],"data":null,"interrupts":[],"extra":1}',
        b'{"type":"values","ns":[],"data":NaN,"interrupts":[]}',
    ],
)
def test_native_codec_rejects_invalid_persisted_payloads(payload: bytes) -> None:
    codec = NativeStreamPartCodec()

    with pytest.raises(ValidationError):
        codec.decode(payload)


def test_native_sse_renderer_uses_sequence_and_current_json() -> None:
    codec = NativeStreamPartCodec()
    decoded = codec.decode(
        codec.encode(
            NativeStreamPart(
                type="values",
                ns=(),
                data={"answer": 42},
            )
        )
    )

    frame = codec.render(seq=7, payload=decoded)

    assert frame.startswith(b"id: 7\nevent: stream-part\ndata: ")
    assert frame.endswith(b"\n\n")
    data = json.loads(frame.split(b"data: ", maxsplit=1)[1])
    assert data == {
        "type": "values",
        "ns": [],
        "data": {"answer": 42},
        "interrupts": [],
        "graphOrigin": {"parentTask": None, "subagentRequest": None},
        "subagentRequests": [],
    }
