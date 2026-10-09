# tinkerfin-native-stream

`tinkerfin-native-stream` owns the current validated Deep Agents and LangGraph native stream
boundary shared by the TinkerFin Runtime, the AG-UI adapter, and native Messaging
codecs. It does not create Agents, convert AG-UI events, persist messages, or own
application resources.

## Installation

```bash
pip install tinkerfin-native-stream
```

## Decode recorded output

Use `NativeStreamPart` to validate a stored JSON record and obtain the frame consumed
by Native protocol integrations:

```python
from tinkerfin_native_stream import NativeStreamPart

record = '{"type":"custom","ns":[],"data":{"progress":0.5}}'
part = NativeStreamPart.model_validate_json(record)
frame = part.to_frame()
print(frame.replay.data)
```

## Live and recorded values

A Runtime Profile maps live graph output into `NativeStreamFrame`.

`NativeStreamFrame.canonical` retains the validated live object for protocol conversion,
`observations` contains ordered protocol-neutral Runtime facts, and `replay` is the
detached finite `NativeStreamPart` used by Native SSE and Messaging. A replay codec must
consume that model directly rather than parse the provider object again.

Public replay payloads use plain JSON: tuples become arrays, Pydantic models and
dataclasses use their JSON serializers, and bytes default to URL-safe base64 strings.
Business fields
such as `$type` remain ordinary data. The Driver preserves established omissions for
opaque host resources; additional stream modes require JSON-compatible payloads.
Recorded parts retain the graph origins needed for delegated retry and resume.

The frame prevents reassignment of its fields. Its canonical envelope and messages
remain mutable: consumers must treat them as read-only. Messages are borrowed unless
normalization requires a copy, and input messages are never changed. Upstream changes
can still affect borrowed objects; the detached replay representation does not follow
those changes.

`RuntimeInterruptEnvelope` defines the protocol-neutral JSON value emitted by Native
workflows that request human input. Producers validate their response Schema before
emission; protocol adapters apply their own publication and resume validation. The
package does not contain AG-UI event models or conversion lifecycle code.

## Documentation

[Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md).

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
