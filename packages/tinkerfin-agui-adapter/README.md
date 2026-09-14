# TinkerFin AG-UI Adapter

## What it is

`tinkerfin-agui-adapter` converts caller-supplied Deep Agents or LangGraph v2 stream
parts into validated AG-UI 0.1.19 events. It does not create a graph, invoke a model,
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

The converter accepts an asynchronous iterable of live v2 parts:

<!-- adapter-quick-start:start -->
```python
import asyncio

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
    identity = RunIdentity(threadId="thread-1", runId="run-1")
    async for event in astream_events(
        parts(),
        identity=identity,
    ):
        frame = encode_sse(event)
        print(frame, end="")


asyncio.run(main())
```
<!-- adapter-quick-start:end -->

Production graph integration supplies `messages`, `tasks`, and `values` with
`version="v2"` and `subgraphs=True`. `RUN_STARTED.input` is omitted; transport input and
Graph input remain caller-owned boundaries rather than duplicated event payloads.
One conversion exclusively consumes and closes the supplied iterator.
`astream_events()` already creates the main lifecycle, including its unique terminal;
`AgUiLifecycleEventFactory` is for custom orchestrators that do not use this stream
facade. `encode_sse()` only renders an event and does not take lifecycle or transport
ownership.

## Event contract

- A fully consumed conversion emits one `RUN_STARTED` and exactly one main terminal.
- Text, reasoning, and tool lifecycles close before state, interrupt, error, or
  completion boundaries.
- Full graph namespaces and IDs correlate concurrent messages, tool calls, results, and
  subagents; arrival order is not identity.
- Every `tasks/start` establishes a possible compiled-graph scope. Only a scope also
  correlated with a Deep Agents `task` Tool call receives `deep_agent_subagent`
  identity; ordinary graphs use `compiled_subgraph` provenance.
- Task and non-root values diagnostics use `source="langgraph.tasks"` and
  `source="langgraph.values"`. A LangGraph task is not automatically a subagent, and
  subgraph provenance never uses AG-UI `parentRunId`.
- Verified Deep Agents delegates publish `SubagentProvenance` with schema
  `tinkerfin.subagent-provenance`. `subagentInvocationId` is derived from the
  thread and complete scoped parent `task` Tool ID, so it remains stable when a new
  request run resumes the same checkpointed invocation.
- Delegate provenance uses the effective Deep Agents `task` fields `description` and
  `subagent_type`. The sanitized task RAW event retains the complete native
  pre-validation input, but additional model-produced arguments do not change the
  delegate identity or invalidate an otherwise executable task.
- Provider-private reasoning is removed from task, state, raw, message, and terminal
  payloads. `expose_reasoning_events=True` enables only verified event sources.
- `expose_subagent_events=False` suppresses public subgraph events while preserving
  validation.
- Child interrupts remain buffered until root `values` propagates an identical full ID
  and value. The terminal boundary publishes root state, then a root-first
  graph-scoped message snapshot, then one interrupt outcome. A replay of the same
  child interrupt set must carry the identical message snapshot for that graph namespace.
- Declared `RuntimeInterruptEnvelope` values map to AG-UI interrupts without a Tool
  ID. Their trusted envelope, response schema, and native ID are persisted for generic
  resume translation; one batch cannot mix runtime and Tool interrupts.
- `prior_tool_call_ids` accepts complete `tf:tool:...` scoped IDs and lets a resumed
  host publish a child Tool result without synthesizing another start/args/end.
- Tool reviews publish strict `metadata.deepagents` schema
  `tinkerfin.deepagents.tool-review`. Use `parse_tool_review_interrupt()` on the
  complete persisted interrupt; missing, unknown, or inconsistent fields fail closed.
- `private_state_keys` removes only named top-level channels at known state projection
  boundaries. Nested same-named business fields remain visible. TinkerFin Plan
  Runtimes supply their private channels automatically.
- Conversion pulls with bounded lookahead. Close the event iterator explicitly or use
  `aclosing` when leaving iteration early; cancellation during a pull closes its upstream.

`ResumeMapper.map()` translates complete AG-UI resume entries from native checkpoint
interrupts and messages grouped by full graph namespace. Resolved reviews require those
messages so Tool calls can be correlated safely. Identical actions in different
Graphs require `interrupt_graph_namespaces` from trusted checkpoint evidence. `ResumeMapper.map_agui()` instead
accepts complete AG-UI interrupts that the host persisted from an earlier terminal. It
reuses their already verified scoped `toolCallId` values and does not query a graph or
checkpointer. Never pass client-supplied interrupt payloads to that method.

Both paths distinguish resolved, abandoned, and mixed decisions and never convert
cancellation into rejection. `ResumeTranslation.kind` distinguishes Tool review from a
generic runtime interrupt. A `custom` Tool translation preserves every native group and
cancelled slot for a cancellation-aware executor; stock Deep Agents cannot execute that
shape. Persisted AG-UI Tool reviews also retain every scoped Tool ID and verified
subagent source name needed by that executor. A `custom` generic runtime translation is
not a Tool decision and must not be sent through Tool cancellation middleware.

Edited Tool arguments are validated against the persisted `args_schema` using JSON
Schema Draft 2020-12 before native resume data is produced. Custom runtime reasons must
be a core reason or a namespaced extension such as `tinkerfin:plan_review` or
`vendor:approval`; unknown unnamespaced reasons fail validation. Generic LangGraph
interrupts use `langgraph:interrupt`.

`encode_sse(event, event_id=...)` encodes one event; delivery, persistence, retries,
custom mixed execution, and transport cancellation remain caller-owned.

`RuntimeInterruptEnvelope` is intended for framework workflows such as Plan review.
The envelope rejects malformed Draft 2020-12 response Schemas before publication.
`ResumeMapper` validates trusted persisted correlation, full resume coverage, and the
resolved JSON object with format checking before producing native `Command(resume=...)`
data. `require_valid_schema(...)` and `validate_json_schema_instance(...)` expose the
same generic validation boundary to framework integrations. The graph that emitted the
envelope remains responsible for domain validation such as revision checks.

## Exporting retained human-input requests

`project_interrupt(native, tool_call_ids=..., source=...)` converts a retained
native interrupt into the same public action contract used by live conversion.
For Tool approval, pass the complete scoped Tool IDs already correlated in native
action order. Runtime and generic interrupts do not take Tool IDs. The function
preserves positional review policies and response schemas; it does not correlate
messages, access checkpoints, or authorize a response.

To read recorded conversations, use
[AgUiHistory](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/runtime/api-reference.md#recorded-ag-ui-conversations)
from `tinkerfin.agui`. Its snapshots and updates include the references used by live
AG-UI messages and pending interactions.

## Durable attachment content

`user_message_to_langchain()` from `tinkerfin_agui_adapter.media` converts text and
attachment inputs into LangChain messages. A durable attachment uses the AG-UI
`source.value` URI `attachment:<id>` and a validated Attachment descriptor in `metadata`.
Hosts authorize the reference before invoking an agent. Snapshot conversion preserves
these typed content fragments rather than serializing them into answer text.

Tool results expose an `attachments` array alongside the standard text `content`.
Assistant attachment blocks emit `CUSTOM` with name `tinkerfin.message.attachments`
and value `{ "messageId": "...", "attachments": [...] }`, within a balanced message
Start/End lifecycle. Consumers merge additions by attachment ID. Authoritative assistant
and Tool snapshots carry the same descriptors in `attachments`; snapshot arrays replace
prior descriptors. IDs and raw-event graph namespaces retain the ordinary scoped correlation
contract. Attachment descriptors have `id`, `name`, `mime_type`, and `size_bytes` fields;
`Attachment.model_json_schema()` in `tinkerfin_contracts.media` provides their schema.
File bytes, signed download URLs, and credentials are not attachment descriptors.

`MessageAttachments` in `tinkerfin_agui_adapter.media` validates the CUSTOM event
payload. `AttachmentToolCallResultEvent`, `AttachmentAssistantMessage`, and
`AttachmentToolMessage` declare the `attachments` field. The adapter publishes
`AttachmentMessagesSnapshotEvent` to validate and serialize these messages together
with standard AG-UI user, system, developer, activity, and reasoning messages.
`parse_attachment_output_event()` validates decoded tool results or message snapshots,
including events returned by the generic `AgUiCodec`, and returns these public types.

The packaged `contracts/message-attachments.schema.json`, `tool-call-result.schema.json`,
`assistant-message.schema.json`, `tool-message.schema.json`, and
`messages-snapshot.schema.json` describe the corresponding wire contracts.
Native messages use standard `image` or `file` blocks with
`file_id`, `mime_type`, and `extras.attachment`; hosts resolve these IDs with
`AttachmentMiddleware` before the model request.

## Documentation

- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/index.md)
- [Adapter extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/agui/adapter-extensions.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
