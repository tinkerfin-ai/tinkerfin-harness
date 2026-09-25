# AG-UI API

[AG-UI](index.md) · [中文](../../cn/agui/api-reference.md)

## Runtime API

| API | Purpose |
| --- | --- |
| `runtime.open_agui_run(...)` | Return a lazy, single-use `AgUiRunStream` |
| `AgUiRunStream.abort()` | Request cancellation and return remaining terminal events |
| `AgUiRunStream.aclose()` | Settle execution and cleanup |
| `AgUiRunStream.to_sse()` | Return a single-use `SseBody[bytes]` of UTF-8 SSE frames |
| `AgUiResumeRequest` | Carry untrusted client decisions for pending interrupts |
| `AgUiResumeReceipt` | Immutable saved-request receipt with `identity`, `parent_run_id`, opaque `receipt_id`, and `responses` |
| `AgUiResumeResponse` | One saved public `interrupt_id` and `status`, without a decision payload |

### `open_agui_run()`

| Parameter | Default | Purpose |
| --- | --- | --- |
| `thread_id`, `run_id` | required | Run identity inside the Runtime namespace |
| `messages` / `input` / `resume` | exactly one | User messages, advanced native state, or resume decisions |
| `parent_run_id` | `None` | Authorized branch or resume lineage |
| `mode` | Runtime default | `default` or configured `plan` |
| `config`, `context` | `None` | Graph settings and typed invocation context |
| `stream_timeout` | `None` | Native stream deadline |
| `cleanup_timeout` | `None` | Caller wait limit for protected cleanup |
| `include_reasoning_events` | `False` | Include verified public reasoning events |
| `include_subagent_events` | `True` | Include subagent events |
| `on_native_part`, `on_agui_event` | `None` | Asynchronous observation before delivery |
| `on_resume_saved`, `on_resume_not_saved` | `None` | Resume-only settlement callbacks |

An `AgUiSettlementTimeoutError` means the stream still owns cleanup. Await `aclose()`
again before disposing shared resources.

## Input helpers

`AgUiUserInput` validates one ID-free user message when a host assigns message IDs after
authorization. `with_attachments(AttachmentSupport(read_content=...))` configures
authorized reads of stored attachment IDs before model calls. Native media requires
no reader and is checked against the destination model's declared capabilities by default.

`AgUiResumeBinding` is the persisted framework-resolved resume value. Ordinary hosts
pass `AgUiResumeRequest` to the Runtime and do not construct bindings.

## Standalone adapter

`tinkerfin-agui-adapter` converts an existing native stream without creating an
`AgentRuntime`:

| API | Purpose |
| --- | --- |
| `astream_events(parts, identity=...)` | Own a complete AG-UI lifecycle around a native stream |
| `DeepAgentAgUiAdapter` | Convert parts when a custom orchestrator owns the main lifecycle |
| `encode_sse(event, event_id=...)` | Encode one event as SSE text (`str`) |
| `micro_batch(events)` | Combine adjacent small deltas without crossing lifecycle boundaries |
| `ScopedIdCodec` | Encode and decode IDs with full Graph position |
| `ResumeMapper` | Translate trusted native or persisted AG-UI interrupt evidence |

The adapter's `RunIdentity` contains AG-UI thread and run IDs. Business namespace
selection belongs to the higher-level Runtime or host.

## Interrupt contracts

| Model | Purpose |
| --- | --- |
| `AgentRuntimeInterrupt` | Stable interrupt ID and JSON value |
| `RuntimeInterruptEnvelope` | Validated non-tool workflow pause |
| `HitlRequest` | Paired tool actions and review policies |
| `ToolReviewInterruptMetadata` | Stable tool review correlation data |
| `SubagentProvenance` | Stable subagent invocation and full Graph position |

Public tool decisions are `approve`, `edit`, `reject`, and `respond`. Edited arguments
are validated against the action schema before native resume translation. See
[Interrupts and resume](interrupts-and-resume.md) for the complete flow.
