# TinkerFin Automation

TinkerFin Automation executes trusted application targets immediately or from saved
one-time, interval, and Cron schedules. Owner-bound task and run handles provide
updates, execution history, cancellation, and explicit retries. Shared stores
coordinate request idempotency, queue limits, and worker capacity.

The host supplies authentication, target permissions, business inputs, and external
resources. Automation records graph interrupts without approving or resuming them.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin-automation
```

In-memory storage, TinkerFin targets, and Agent management tools are included. For
SQL persistence, select an asynchronous driver:

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
```

Use `asyncpg` for PostgreSQL or `asyncmy` for MySQL.

## Quick Start

```python
import asyncio

from tinkerfin_automation import Automation, ExecutionRequest


async def main() -> None:
    automation = Automation(namespace="my-application")

    @automation.target("project_summary")
    async def create_summary(request: ExecutionRequest) -> dict[str, str]:
        return {"project_id": str(request.input["project_id"])}

    async with automation.worker():
        owner = automation.for_owner("user-123")
        run = await owner.run(
            "project_summary",
            input={"project_id": "project-42"},
            request_id="summarize-project-once",
        )
        await run.wait(timeout=30.0)
        print(run.status, run.result)


asyncio.run(main())
```

Bind the authenticated user's string ID with `for_owner()`. The owner is an isolation
scope, not a credential. Keep credentials out of persisted JSON input. The default
Store is process-local and closes when the worker context exits.

`run.wait()` observes the execution; cancelling the wait does not cancel the target.
Use `run.cancel()` to request execution cancellation. External effects may continue,
and uncertain work can require separately authorized `owner.resolve_run()`.

## Save and manage schedules

Inside an active worker context, use `owner.create_task()` with the same registered
target and input:

```python
from tinkerfin_automation import Schedule

task = await owner.create_task(
    name="Weekday summary",
    target="project_summary",
    input={"project_id": "project-42"},
    schedule=Schedule.cron("0 9 * * mon-fri", timezone="Asia/Shanghai"),
    request_id="weekday-summary",
)
print(task.id)
```

Use `Schedule.once(at=...)` for one timezone-aware instant or
`Schedule.every(minutes=30, start_at=...)` for an interval with a stable aware anchor.
Reuse the anchor on command retries. Intervals range from one minute to 365 days.
Schedules support inclusive `active_from` and exclusive `active_until` bounds;
manual runs remain available outside them. Cron uses five fields, skips DST gaps,
and selects the earlier UTC occurrence during an overlap.

Obtain a saved task with `await owner.task(task_id)`. Use `task.run()` for an extra
execution, `pause()` to stop future wakeups, and `enable()` to resume without paused
catch-up. `delete()` cancels queued executions and retains history. Handle properties
are snapshots: use `refresh()` for current state.

Task writes use the handle's observed revision. For remote forms, send the original
`expected_revision`. Retrying a command must preserve its request ID, revision,
input, and schedule anchor; conflicts are returned without automatic retry.
Omitting `request_id` does not guarantee deduplication. `run.retry()` instead creates
a new execution for failed, timed-out, or cancelled work.

`ExecutionLimits` bounds concurrency, queue size, execution time, and queue lifetime.
`worker(global_concurrency=16)` limits unfinished executions across workers. See the
[complete task example](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-automation/examples/active_period.py)
for active periods, filtering, and aggregation.

## Persistence and worker ownership

For SQL persistence, pass `SqlAlchemyAutomationStore(engine)` to
`Automation(namespace=..., store=store)`. Entering the worker prepares storage.
Explicit Stores and their `AsyncEngine` remain host-owned: after Automation exits,
await `store.close()` and then `engine.dispose()`. A supplied Scheduler transfers
its lifecycle to Automation. Configure connection and statement timeouts on the
Engine.

Each Automation instance can be entered once; do not nest client and worker contexts.
Close it outside running targets and resource callbacks. Shutdown joins accepted
operations and owned cleanup, including on cancellation.

`async with automation` selects a submission/query client for a remote worker.
Declare targets for new work with `automation.remote_target(...)` before entry,
and keep declarations consistent with the worker's authorized execution namespaces.
Creating or modifying schedules remotely requires shared storage and a started
Notifications service using the same Redis channel on clients and workers. Keep
Notifications open until workers and observers exit. Workers sharing a scheduling
namespace must handle every target they can claim. The guide includes complete
local and remote setup examples.

## Execute Agent work

Register `TinkerFinTarget(runtime)` with `automation.target(...)`. Its execution
namespace comes from the Runtime; `for_owner()` controls task ownership. Owners
using one Runtime also share that Runtime's memory and workspace, so select
Runtimes according to application authorization. Task input cannot replace the
Runtime's model, tools, or namespace. A successful Agent run may have `result=None`;
read its messages through Tracing or application storage.

For durable conversation output, install `tinkerfin-automation[gateway]` and use
`TinkerFinTarget(runtime, gateway=gateway)` with a host-owned Gateway whose Messaging
and Notifications resources are open.
Closing a browser subscription does not cancel execution. Automation can cancel a
producer it newly admitted; attaching to existing delivery does not transfer that
producer's cancellation ownership.

An interrupted Agent remains unresolved until the host handles it. The worker's
`on_interrupt` callback can classify it but cannot approve or resume the graph.
Only declare `FunctionTarget(..., cancellation_is_final=True)` when settled
cancellation proves external work has stopped.

Use `create_automation_tools(owner, allowed_targets=...)` to expose authorized task
management to Agents. These tools retain the bound owner and target permissions;
they do not expose cancellation, retries, resolution, or graph resume.

## Documentation

- [Automation guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/automation/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
