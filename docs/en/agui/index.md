# AG-UI

[Documentation](../index.md) · [中文](../../cn/agui/index.md)

AG-UI represents agent text, tool calls, state, approvals, and outcomes as frontend
events. `AgentRuntime` owns conversion and the main event lifecycle.

## Installation

```bash
pip install "tinkerfin[agui]" langchain-openai
```

Replace the model provider package when using another provider.

## Open an event stream

```python
from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("customer-1")
    .build(model="openai:gpt-5.4", tools=tools)
)

events = runtime.open_agui_run(
    thread_id="conversation-1",
    run_id="request-1",
    messages=[{"id": "message-1", "role": "user", "content": "Hello"}],
)

async with aclosing(events):
    async for event in events:
        await send_event(event)
```

The application chooses the Runtime namespace and authorizes the thread and run IDs.
Messages must have final, distinct IDs. When a checkpointer already contains the thread
history, submit only new user messages.

`open_agui_run()` accepts exactly one of `messages`, native `input`, or `resume`.
Preparation is lazy: no model, Graph, Sandbox, or observer session opens until the
stream is preflighted or consumed.

## HTTP and SSE

The HTTP layer authenticates the user, selects a namespace, validates request IDs, and
maps product settings such as model or mode. Client tool descriptions never grant tool
execution permission.

For direct SSE:

```python
from starlette.responses import StreamingResponse

return StreamingResponse(events.to_sse(), media_type="text/event-stream")
```

For durable delivery and reconnect replay, pass the event stream to Messaging:

```python
body = await channel.open_sse(events, after=last_event_id)
```

The body contains UTF-8 SSE bytes. See [Streams and SSE](../runtime/streams-and-sse.md)
for EventSourceResponse usage and HTTP cleanup requirements.

## Resume approval

Send only client decisions in `AgUiResumeRequest`. The Runtime reloads pending
interrupts from the authoritative checkpoint and verifies the complete batch before
continuing.

```python
from tinkerfin import AgUiResumeRequest

events = runtime.open_agui_run(
    thread_id="conversation-1",
    run_id="request-2",
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    on_resume_saved=record_receipt_idempotently,
    on_resume_not_saved=release_claim_idempotently,
)
```

`on_resume_saved` receives an immutable `AgUiResumeReceipt` with the saved public
responses. Retries deliver an equal receipt, so settlement must use its opaque
`receipt_id` idempotently. `on_resume_not_saved` is used only when the request has not
been durably saved. Cancelling the complete pending batch runs no reviewed tools and
does not call `on_resume_saved`.

## Event identity

Main lifecycle events use the supplied `thread_id` and `run_id`. Subagent and tool IDs
include their full Graph position. `graph_namespace` describes that execution position;
the Runtime namespace remains the business isolation scope.

Every run emits one main start and one main terminal outcome. Closing or cancelling a
stream also closes any open child text, reasoning, and tool lifecycles.

## Next steps

- [Events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Adapter extensions](adapter-extensions.md)
- [AG-UI API](api-reference.md)
