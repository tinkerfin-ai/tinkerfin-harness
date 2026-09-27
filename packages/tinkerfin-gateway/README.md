# TinkerFin Gateway

Run commands, durable output subscriptions, and resource-change notifications for
applications using TinkerFin.

## Installation

```bash
pip install tinkerfin-gateway
```

## Run a command

```python
from tinkerfin import TinkerFin
from tinkerfin_gateway import Gateway, StartRun
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notifications


async def answer(model):
    runtime = TinkerFin().with_namespace("authorized-account").build(model)
    async with Messaging() as messaging, Notifications() as notifications:
        gateway = Gateway(messaging=messaging, notifications=notifications)
        run = await gateway.start(
            runtime,
            StartRun(
                thread_id="conversation",
                run_id="request-id",
                messages=({"id": "question", "role": "user", "content": "Hello"},),
            ),
        )
        async with run.subscribe() as replies:
            async for reply in replies:
                print(reply.envelope.seq, reply.data.type)
```

Keep Messaging and Notifications open for the application lifetime. Gateway borrows
both resources. The default backends keep data within one process; use their
shared storage and Redis integrations for multiple application workers. The `name`
argument selects a stable Messaging delivery space across restarts.

`start`, `resume`, and `compact` accept execution without requiring anyone to read
the output. `stream` returns the original admission subscription for immediate
delivery. Close subscriptions even if you never consume them. Closing a reader
does not cancel execution; use `await run.cancel()` explicitly.

Reuse a run ID only for the same complete command. Messages, decisions, parent,
mode, and JSON execution parameters are bound atomically to the retained run.
Changed content raises `tinkerfin_messaging.RunRequestConflict`. Record deletion
or retention expiry ends this deduplication window. Hosts still authorize input,
model/tool choices, namespaces, and access to existing run identities.

Use `ResumeRun(resume=AgUiResumeRequest(...), ...)` for complete interrupt decisions
and `CompactRun(thread_id=..., run_id=...)` for context compression. Read or cancel
an existing run with `gateway.run(authorized_identity)` without building a Runtime.
`delivery_status()` reports message delivery state, not the Agent's business result.

## Host business state

The optional `registration` implements `RunRegistration.confirm(acceptance)` and
`release()`. Confirmation distinguishes prepared new execution from attachment to
an existing command. Release can only remove the current submission's reservation;
another concurrent submission may already own accepted business state.

Resume decision storage has its own `ResumeSettlement.saved(receipt)` and
`not_saved()` methods. Admission never proves that decisions were saved. Receipts
may repeat; an unknown outcome or unused retry source may report neither method.
Use short transactions over application resources because these operations can
outlive the originating HTTP request.

`on_committed` observes the main start and terminal events after output storage.
Replay does not repeat it and observation failures do not undo execution. Use
`RunPresentation(start_attributes=..., cancelled_message=...)` for static display
fields; protocol identity and lifecycle fields cannot be overridden.

## Browser delivery

Install `tinkerfin-gateway[starlette]` for the optional HTTP response integration.
The host supplies its own routes and authentication:

```python
from tinkerfin_gateway.starlette import sse_response


async def send_command(gateway, authorized_runtime, command):
    return await sse_response(gateway.stream(authorized_runtime, command))
```

For changes to resource lists, use `await gateway.notifications(scopes=...)` and
pass the result to `sse_response`. Derive every `NotificationScope` from host
authorization. An owner-specific scope only matches that owner; an ownerless
scope selects the entire namespace. Supply the session's fixed `expires_at` and
an async `authorize` check to close revoked access. Checks should use short,
bounded operations without retaining a request database session.

The `ready` frame establishes subscriptions before the browser loads its baseline.
`change` carries a resource invalidation and `resync` requests fresh authoritative
reads. Keep a periodic repair read because hints are not durable. Hidden tabs can
disconnect and load a new baseline when visible. This stream carries no Agent
message content; use run output subscriptions for replies.

The response closes its readers on completion, send failure, cancellation, and
disconnect. If a prepared response will not be sent, call `await response.aclose()`.

## Documentation

[TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/gateway/index.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
