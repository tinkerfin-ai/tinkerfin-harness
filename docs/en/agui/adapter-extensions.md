# Use the AG-UI converter directly

[Interrupts and resume](interrupts-and-resume.md) · [中文](../../cn/agui/adapter-extensions.md)

Use `tinkerfin-agui-adapter` by itself when your application already creates and runs LangGraph and only needs native-v2-to-AG-UI conversion.

## Installation

```bash
pip install tinkerfin-agui-adapter
```

The converter does not create a Graph, invoke a model, read checkpoints, or provide an HTTP server.

## The simplest conversion path

```python
from contextlib import aclosing
from tinkerfin_agui_adapter import RunIdentity, astream_events


parts = graph.astream(
    graph_input,
    config,
    stream_mode=("messages", "tasks", "values"),
    version="v2",
    subgraphs=True,
)

events = astream_events(
    parts,
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
)

async with aclosing(events):
    async for event in events:
        await send_event(event)
```

Fully consuming `astream_events()` delivers one `RUN_STARTED` and one main terminal around the converted events. The async context closes the source even on early exit. Do not share `parts` with another consumer.

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `parts` | required | Async LangGraph v2 stream |
| `identity` | required | Main thread and run identity emitted by this conversion |
| `expose_reasoning_events` | `False` | Emits supported reasoning events |
| `expose_subagent_events` | `True` | Delivers subagent events |
| `prior_tool_call_ids` | `frozenset()` | Complete scoped tool IDs emitted before resume |
| `private_state_keys` | `frozenset()` | Top-level state channels omitted at known public projection boundaries |

`private_state_keys` is explicit for the standalone converter because it cannot infer
which host fields are private. It never deletes nested same-named fields. TinkerFin
Plan Runtimes provide their internal keys automatically.

## Durable attachments

`Attachment` from `tinkerfin_contracts.media` contains `id`, `name`, `mime_type`,
and `size_bytes`. Its `content_block()` produces a LangChain `image` or `file`
block with `file_id`, `mime_type`, and `extras.attachment`. Store the reference in
messages. To resolve it, the host authorizes access and supplies bounded file bytes through
`TinkerFin().with_attachments(AttachmentSupport(read_content=...))` when making model
requests. The optional asynchronous reader returns
`AttachmentContent(data=..., mime_type=...)`; without it, stored attachments remain
references for file-reading tools. Native media needs no reader: framework-created
graphs check it against the destination model's declared image, audio, video, and PDF
capabilities by default. `supports_content(model, mime_type)` can customize this check
without a reader. Unsupported content or unknown support produces descriptive text
for the model; original messages and stored references remain unchanged.
This does not parse files or convert video into frames.

With a reader, `max_attachments` defaults to 5 recent supported files per request.
`max_bytes` defaults to 20 MiB of total resolved content before base64 encoding; exceeding it
raises `ValueError` before provider invocation. The host must also bound storage
reads before returning bytes. This policy applies to framework-created graphs;
externally compiled and remote agents choose their own file-input mechanism.

Ordinary callers pass standard user messages to `runtime.open_agui_run(messages=...)`;
the Runtime owns validation and conversion. `AgUiUserInput` provides ID-free text and
attachment inspection plus authorized descriptor replacement for hosts assigning IDs.

For custom adapter integrations, `user_message_to_langchain()` from
`tinkerfin_agui_adapter.media` converts AG-UI user messages to LangChain;
`user_content_to_agui()` converts LangChain user content to AG-UI. Images, audio,
and video use `type: "image"`, `"audio"`, and `"video"`; other files use `"document"`.
Attachment fragments carry `source: {"type": "url", "value": "attachment:<id>"}`
and the descriptor in `metadata`. User-message snapshots preserve this shape.

Tool results and assistant snapshots expose descriptors in an `attachments`
array. Assistant streaming uses CUSTOM `tinkerfin.message.attachments`, with
`{"messageId": "<scoped-id>", "attachments": [...]}`, between the matching
TEXT_MESSAGE_START and TEXT_MESSAGE_END. Merge these additions by attachment ID;
snapshot arrays replace the prior descriptors. Use `rawEvent.source` to place
subagent output with its owning invocation.

`MessageAttachments` in `tinkerfin_agui_adapter.media` validates this CUSTOM
payload. The package includes `contracts/message-attachments.schema.json`. Descriptors
contain no bytes, credentials, or temporary download URLs.

`AttachmentToolCallResultEvent`, `AttachmentAssistantMessage`, and
`AttachmentToolMessage` declare typed `attachments` fields.
`AttachmentMessagesSnapshotEvent` validates and serializes attachment-bearing history.
After generic AG-UI replay, call `parse_attachment_output_event(event)` to validate a
tool result or snapshot and obtain its public attachment types. The package also ships
`tool-call-result.schema.json`, `assistant-message.schema.json`, `tool-message.schema.json`,
and `messages-snapshot.schema.json` under `contracts/`.

## Encode events as SSE

```python
from tinkerfin_agui_adapter import encode_sse


async for event in events:
    frame = encode_sse(event, event_id=next_event_id())
    await send_text(frame)
```

`event_id` may be a string or integer. `encode_sse()` only renders one event; it does not persist, retry, replay, or cancel delivery.

## Control the main lifecycle yourself

Only custom orchestrators normally need `DeepAgentAgUiAdapter` directly:

```python
from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
)


lifecycle = AgUiLifecycleEventFactory()
identity = RunIdentity(threadId="thread-1", runId="run-1")
adapter = DeepAgentAgUiAdapter(identity=identity)

try:
    await send_event(lifecycle.started(identity=identity))
    async for part in parts:
        for event in adapter.process(part):
            await send_event(event)
    for event in adapter.finish():
        await send_event(event)
    await send_event(
        lifecycle.finished(
            identity=identity,
            outcome=adapter.main_outcome(),
        )
    )
except Exception:
    for event in adapter.abort():
        await send_event(event)
    await send_event(
        lifecycle.failed(
            identity=identity,
            message="Agent run failed",
            code="runtime_error",
        )
    )
finally:
    await parts.aclose()
```

`abort()` closes only child text, reasoning, and Tool lifecycles. The custom
orchestrator emits the one main `RUN_ERROR`, as shown above.

Your orchestrator owns one start and one terminal. Prefer `astream_events()` unless you truly need that ownership.

## Reduce very small deltas

`micro_batch()` combines adjacent text and tool-argument deltas without crossing lifecycle boundaries:

```python
from tinkerfin_agui_adapter import micro_batch


batched = micro_batch(events)
async for event in batched:
    await send_event(event)
```

## Preserve scoped IDs

```python
from tinkerfin_agui_adapter import ScopedIdCodec


codec = ScopedIdCodec()
public_id = codec.encode("tool", ("researcher",), "call-7")
kind, graph_namespace, raw_id = codec.decode(public_id)
```

Store the complete scoped ID. Raw tool IDs may repeat in different graph namespaces.

## Parse framework extensions

Tool approval metadata is fixed by `ToolReviewInterruptMetadata` and schema
`tinkerfin.deepagents.tool-review`:

```python
from tinkerfin_agui_adapter import parse_tool_review_interrupt


review = parse_tool_review_interrupt(persisted_interrupt)
print(review.tool_name, review.original_args.root)
```

The parser validates the complete interrupt, native action group, action index,
decision policy, arguments, and scoped Tool ID. Unknown fields and client-supplied
interrupt metadata fail closed.

Deep Agents `task` calls publish `SubagentProvenance` with schema
`tinkerfin.subagent-provenance`. Its `subagentInvocationId` is stable across resume;
`requestRunId` identifies the main request carrying the current event. Parent task
results expose `relatedSubagentInvocationId`. These fields describe Agent nesting;
standard AG-UI `parentRunId` retains branch and time-travel lineage semantics.

Next: [AG-UI usage reference](api-reference.md).
