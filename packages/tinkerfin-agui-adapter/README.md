# TinkerFin AG-UI Adapter

## What it is

`tinkerfin-agui-adapter` converts live Deep Agents or LangGraph v2 stream parts and
recorded TinkerFin `NativeStreamPart` values into validated AG-UI 0.1.19 events.
It does not create a graph, invoke a model,
query checkpoints, authenticate requests, or provide an HTTP server.

Use it when an application already owns Graph execution and needs only the conversion
boundary. Applications that need managed Graph construction, checkpoint resume,
Observation, and stream cleanup can use
[`AgentRuntime.open_agui_run()`](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/index.md).

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin-agui-adapter
```

## Quick Start

The converter accepts an asynchronous iterable of live v2 parts or decoded
`NativeStreamPart` records. This example supplies live parts:

<!-- adapter-quick-start:start -->
```python
import asyncio
from contextlib import aclosing

from langchain_core.messages import AIMessageChunk

from tinkerfin_agui_adapter import RunIdentity, astream_events, encode_sse


async def parts():
    yield {
        "type": "messages",
        "ns": (),
        "data": (
            AIMessageChunk(id="ai-1", content="Hello", chunk_position="last"),
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }
    yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}


async def main():
    identity = RunIdentity(namespace="company-a", threadId="thread-1", runId="run-1")
    async with aclosing(astream_events(parts(), identity=identity)) as events:
        async for event in events:
            print(encode_sse(event), end="")


asyncio.run(main())
```
<!-- adapter-quick-start:end -->

For a live graph, supply `messages`, `tasks`, and `values` with `version="v2"`
and `subgraphs=True`. One conversion exclusively consumes and closes the supplied
iterator. Close the event iterator explicitly or use `aclosing` when leaving early;
cancellation during a pull closes its upstream.

`astream_events()` owns the main event lifecycle. Fully consuming it emits one
`RUN_STARTED` and exactly one main terminal, closing text, reasoning, and tool
lifecycles before completion. Do not add a second main lifecycle around it.
`encode_sse()` renders one event; the host owns transport, persistence, and delivery.

## Preserve event identity and privacy

Preserve complete scoped IDs, graph namespaces, and event order when storing or
replaying output. Arrival order does not identify concurrent messages, tools, or
subagents. State and message snapshots precede an interrupt terminal. Recorded
`NativeStreamPart` values retain the origins needed for delegated retry and resume;
raw SDK parts must provide sufficient task-start evidence or conversion rejects
ambiguous ancestry.

Provider-private reasoning is removed from public payloads.
`expose_reasoning_events=True` enables only verified reasoning event sources.
`private_state_keys` hides named top-level state channels at public projection
boundaries; it does not hide nested fields with the same name. Standalone hosts
must identify their private channels. Managed TinkerFin Plan Runtimes supply theirs.

## Resume human-input requests

Applications using TinkerFin should submit `AgUiResumeRequest` to
`runtime.open_agui_run(resume=...)`; the Runtime owns checkpoint lookup and
translation. Standalone integrations can use `ResumeMapper` with complete trusted
checkpoint evidence or persisted AG-UI interrupts. Never pass client-supplied
interrupt payloads to `ResumeMapper.map_agui()`.

Resume entries must cover every pending interrupt. Edited tool arguments and generic
runtime responses are validated against their persisted JSON Schemas. Cancellation
means abandonment, not rejection. Mixed tool decisions require a cancellation-aware
executor; stock Deep Agents cannot execute that translation. Preserve the complete
persisted interrupts, scoped tool IDs, and provenance rather than reconstructing
correlation from display text. See the approval guide for the complete flow and
standalone integration contracts.

To read recorded conversations, use
[AgUiHistory](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/api-reference.md#recorded-ag-ui-conversations)
from `tinkerfin.agui`. Its snapshots and updates use the same references as live
messages and pending interactions.

## Attachment content

Durable attachment inputs use `source.value="attachment:<id>"` with a validated
Attachment descriptor in metadata. Hosts authorize references before invoking an
agent. Descriptors contain identity, name, MIME type, and size; keep file bytes,
signed download URLs, and credentials out of them.

Tool results expose an `attachments` array. Assistant additions use the
`tinkerfin.message.attachments` CUSTOM event: merge additions by attachment ID;
authoritative snapshot arrays replace prior descriptors. Use
`parse_attachment_output_event()` from `tinkerfin_agui_adapter.media` to validate
decoded tool results and message snapshots. The adapter extensions guide covers
input conversion, event models, and packaged JSON Schemas.

## Documentation

- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/index.md)
- [Events and identity](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/events.md)
- [Interrupts and resume](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/interrupts-and-resume.md)
- [Adapter extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/adapter-extensions.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/api-reference.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
