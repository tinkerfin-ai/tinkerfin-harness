# Understand AG-UI events

[AG-UI basics](index.md) · [中文](../../cn/agui/events.md)

A normal run begins with `RUN_STARTED` and ends with one `RUN_FINISHED` or `RUN_ERROR`. Events in between follow the actual execution order.

## Events you will see most often

| Event | Typical frontend action |
| --- | --- |
| `RUN_STARTED` | Create run state; `input` is omitted because Graph input is explicit |
| `TEXT_MESSAGE_START` | Create a new assistant message |
| `TEXT_MESSAGE_CONTENT` | Append text |
| `TEXT_MESSAGE_END` | Close the message |
| `TOOL_CALL_START` | Show the tool being called |
| `TOOL_CALL_ARGS` | Append streamed arguments |
| `TOOL_CALL_END` | Mark the arguments complete |
| `TOOL_CALL_RESULT` | Show the tool result |
| `STATE_SNAPSHOT` / `STATE_DELTA` | Update frontend state |
| `MESSAGES_SNAPSHOT` | Reconcile the complete message history |
| `RUN_FINISHED` | Mark success or present pending interrupts |
| `RUN_ERROR` | Mark the run failed |

One message or tool call may arrive as many deltas. Group them by their complete IDs, never by arrival order.

## A simple sequence

Without tool calls, a run commonly looks like this:

```text
RUN_STARTED
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT ...
TEXT_MESSAGE_END
STATE_SNAPSHOT / STATE_DELTA
RUN_FINISHED
```

With tools, text and tool events may interleave, and several tools may remain open in parallel. Keep separate UI state for each ID.

Tool argument fragments do not end text from the same model message. That message has
one text start and one text end, including when more text follows a Tool fragment.
Its Tool events and message snapshots use the same parent message identity.

## Root agent and subagents

Subagents run in non-root graph namespaces. The converter preserves full graph namespaces and provenance so messages, tools, and state from different scopes are not mixed.

`expose_subagent_events=True` delivers their public events. With `False`, the converter still validates their input but suppresses corresponding public events.

The framework never uses `parentRunId` for LangGraph subgraphs. High-level Runtime takes
`parent_run_id` explicitly and uses it only for checkpoint branching. A
verified Deep Agents delegate carries `tinkerfin.subagent-provenance` in the task
RAW descriptor. `subagentInvocationId` stays stable across resume, `requestRunId`
identifies the current main request, child event sources repeat the invocation ID, and
the parent task Result carries `relatedSubagentInvocationId`.

## Reasoning events

```python
from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = TinkerFin().with_namespace(namespace).build(model=model)
async with aclosing(
    runtime.open_agui_run(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
        include_reasoning_events=True,
    )
) as events:
    async for event in events:
        await send_event(event)
```

Supported providers may produce `REASONING_START`, `REASONING_MESSAGE_*`, and `REASONING_END`. Not every model emits public reasoning, and an empty text chunk is not automatically a heartbeat.

Provider-private reasoning metadata is removed from normal messages, state, and raw payloads regardless of this setting.

## What `messages`, `tasks`, and `values` contribute

| Native mode | Information used during conversion |
| --- | --- |
| `messages` | Text chunks, tool argument chunks, tool results, and message metadata |
| `tasks` | Graph node and task starts, results, errors, and graph namespace relationships |
| `values` | State snapshots and top-level `interrupts` |

The mode is plural: `tasks`. It is not the Deep Agents delegation tool named `task`. Root and subgraph `values` are separate state scopes; a later subgraph snapshot must not replace root state.

## Terminal events

Each main run has one terminal:

- successful completion: `RUN_FINISHED` with a success outcome;
- paused for review: `RUN_FINISHED` with interrupts;
- failure: `RUN_ERROR`.

Open text, reasoning, and tool lifecycles close before the main terminal. If a client disconnects, cancel or close the server stream instead of inventing a success event.

## Observe events before delivery

```python
async def audit_event(event) -> None:
    await audit_log.write(event.model_dump(mode="json", by_alias=True))


from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = TinkerFin().with_namespace(namespace).build(model=model)
async with aclosing(
    runtime.open_agui_run(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
        on_agui_event=audit_event,
    )
) as events:
    async for event in events:
        await send_event(event)
```

`on_agui_event` is useful for audits and metrics. Keep it asynchronous and lightweight
because it is part of the delivery path.

Next: [Interrupts and resume](interrupts-and-resume.md).
