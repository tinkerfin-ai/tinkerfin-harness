# tinkerfin-native-stream

`tinkerfin-native-stream` owns the current validated Deep Agents and LangGraph native stream
boundary shared by the TinkerFin Runtime, the AG-UI adapter, and native Messaging
codecs. It does not create Agents, convert AG-UI events, persist messages, or own
application resources.

## Installation

```bash
pip install tinkerfin-native-stream
```

## Current stream contract

The package exposes the one current `NativeStreamFrame` contract shared by every
downstream consumer. A concrete Runtime Profile owns third-party graph invocation and
maps its live objects into that frame; downstream consumers do not detect or negotiate
an upstream version.

`NativeStreamFrame.canonical` retains the validated live object for protocol conversion,
`observations` contains ordered protocol-neutral Runtime facts, and `replay` is the
detached finite `NativeStreamPart` used by Native SSE and Messaging. A replay codec must
consume that model directly rather than parse the provider object again.

The frame prevents reassignment of its fields. Its canonical envelope and messages
remain mutable: consumers must treat them as read-only. Messages are borrowed unless
normalization requires a copy, and input messages are never changed. Upstream changes
can still affect borrowed objects; the detached replay representation does not follow
those changes.

`RuntimeInterruptEnvelope` defines the protocol-neutral JSON value emitted by Native
workflows that request human input. Producers validate their response Schema before
emission; protocol adapters apply their own publication and resume validation. The
package does not contain AG-UI event models or conversion lifecycle code.

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
