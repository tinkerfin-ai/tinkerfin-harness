# Approvals and resume

[AG-UI events](events.md) · [中文](../../cn/agui/interrupts-and-resume.md)

An agent can pause before running a tool, wait for a decision, and continue within the same namespace and thread.

## Configure tool approval

This example uses an application-defined `delete_order` tool:

```python
from langgraph.checkpoint.memory import InMemorySaver
from tinkerfin import TinkerFin
from tinkerfin.coordination import InMemoryRunCoordinator

runtime = (
    TinkerFin(
        checkpointer=InMemorySaver(),
        run_coordinator=InMemoryRunCoordinator(),
    )
    .with_namespace("company-a")
    .build(
        model="openai:gpt-5.4",
        tools=[delete_order],
        interrupt_on={"delete_order": {"allowed_decisions": ["approve", "reject"]}},
    )
)
```

Resume requires a checkpointer. In-memory storage and coordination suit one process; deployments with multiple processes need shared persistent storage and coordination.

## Submit decisions

Each decision identifies an interrupt from the terminal event. The request must cover every pending item in the batch:

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"type": "approve"}
}
```

| Field | Purpose |
| --- | --- |
| `interruptId` | Interrupt ID published by the server |
| `status` | `resolved` submits a decision; `cancelled` abandons it |
| `payload` | A decision allowed by the interrupt response Schema; omitted for cancellation |

The application owns authentication and approval permissions. Clients submit decisions, not trusted interrupt content, Tool correlation, or checkpoint coordinates.

## Continue execution

```python
from contextlib import aclosing
from tinkerfin import AgUiResumeRequest

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="approval-1",
        resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    )
) as events:
    async for event in events:
        await send_event(event)
```

The framework validates the pending review, Tool correlation, and decision coverage. Unknown IDs, incomplete coverage, stale sources, invalid payloads, and disallowed decisions fail before continuation.

| Decision set | Behavior |
| --- | --- |
| All `resolved` | Save the request and continue execution |
| All `cancelled` | Abandon the request and emit a cancelled terminal without running the Graph |
| Mixed Tool decisions | Execute resolved Tools and return unexecuted results for cancelled Tools |

Cancellation does not become rejection. Mixed Tool decisions require TinkerFin approval support in the affected Tools. Plan and Tool approvals cannot share one batch.

## Retries, concurrency, and callbacks

- Give each new approval request a new `run_id`. Retry the original request with the same `run_id`, `thread_id`, and decisions.
- Serialize writes within each namespace and thread. Use shared coordination or enforce this in the host, covering both Native and AG-UI calls.
- Retries use the original approval batch. If a subagent reaches another review, an old request cannot approve the new question.
- Completed parallel tasks stay complete; recovery submits only original decisions that have not been consumed.
- External effects before an interrupt in the same Graph node still need idempotency. A checkpoint does not make external writes transactional.

Applications that settle business approvals can provide two asynchronous callbacks:

| Parameter | When it runs |
| --- | --- |
| `on_resume_saved` | The request is durably saved; receives `AgUiResumeCheckpoint`, delivered again on retry |
| `on_resume_not_saved` | The request failed or was cancelled before being saved; releases an application claim |

Both callbacks must be idempotent. `on_resume_saved` confirms request persistence, not Tool success. Do not settle approvals from `RUN_STARTED`.

AG-UI does not accept a new review round when a custom Graph repeatedly interrupts within the same checkpoint/task. Put each round in a new Graph step. If another Native invocation leaves a global resume value, the framework rejects continuation to prevent it from answering an unapproved question.

## Plan clarification and review

Build the Runtime with `.with_plan(enabled=True)` and select `mode="plan"` for execution. Resume still uses `open_agui_run(resume=...)`:

| Reason | Response requirements |
| --- | --- |
| `tinkerfin:plan_clarification` | Submit answers with `type="respond"` and `answers` covering every question ID; start discussion with `type="discuss"` and a non-empty `message`, without `answers` |
| `tinkerfin:plan_review` | An allowed action with the current `baseRevision`; when `respond` is allowed, that action and a non-empty `message` start discussion |

Plan interrupts have no `toolCallId`. Clients must not return Forms, labels, or other trusted form content; the framework restores them from the checkpoint. Unknown options, skipped required questions, and stale `baseRevision` values fail validation. After abandoning a Plan, a later ordinary input can use `mode="default"`.

A discussion request ends the pending card. That card can no longer be submitted or approved. The framework preserves its trusted form or draft as conversation context without treating unsubmitted form content as answers. The model may then reply, ask new questions, or produce a new draft; a new draft still requires approval.

The planning model generates ordinary replies directly through the normal message stream, with no separate action-selection call. Replies persist in history; completion waits for a new user message rather than automatically starting another planning turn.

To close a card without sending a discussion message, use `type="dismiss"` for clarification, or `type="dismiss"` with the current `baseRevision` for draft review. Dismissal does not submit unfinished answers, invoke the model, or authorize execution. Planning mode remains active and waits for a new message; the trusted form or draft remains available as context.

Discussing a clarification still requires only one resume call. Here, `runtime` has Plan enabled and `interrupt_id` comes from the current server-issued card:

```python
from ag_ui.core.types import ResumeEntry

async with aclosing(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="discussion-1",
        mode="plan",
        resume=AgUiResumeRequest(entries=(ResumeEntry(
            interrupt_id=interrupt_id,
            status="resolved",
            payload={"type": "discuss", "message": "What does the third question mean?"},
        ),)),
    )
) as events:
    async for event in events:
        await send_event(event)
```

The host still owns authentication and transport. The framework owns card validation, discussion message identity, context persistence, resume deduplication, and cleanup. Do not cancel the card and start a second run to emulate discussion.

## Use the Adapter independently

Integrations that own a complete trusted event log can call `AgUiResumeBinding.from_agui(entries=..., interrupts=...)`. Integrations that own native checkpoints can use `ResumeMapper.map()` with complete Tool messages and Graph locations. Ordinary Runtime callers do not translate native Commands.

Next: [Use the converter directly](adapter-extensions.md)
