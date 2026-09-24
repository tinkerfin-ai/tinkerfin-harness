# TinkerFin Automation

## What it is

TinkerFin Automation executes trusted application targets immediately or from saved
one-time, interval, and Cron schedules. Owner-bound task and run handles provide
updates, execution history, cancellation, explicit retries, and bounded observation.
Stores coordinate request idempotency, queue limits, and cross-worker capacity.

The host supplies authentication, target permissions, business inputs, and external
resources. Automation records graph interrupts without approving or resuming them.

## Installation

```bash
pip install tinkerfin-automation
```

Python 3.11 or newer is required. In-memory storage, TinkerFin targets, and Agent
management tools are included. For SQL persistence, select an asynchronous driver:

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
# PostgreSQL: asyncpg; MySQL: asyncmy
```

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

Bind the authenticated user's string ID once with `for_owner()`. The owner is an
isolation scope, not a credential. Keep credentials out of persisted JSON input.
The default Store is process-local and closes when the context exits.

| Operation | Result |
| --- | --- |
| `owner.run(target, ...)` | Submit one execution without creating a task |
| `owner.create_task(...)` | Save an enabled scheduled task and return a TaskHandle |
| `task.run(...)` | Submit an extra execution without changing its schedule |
| `run.wait(...)` | Observe only that execution; submission itself does not wait |

## Core concepts

### Schedules

```python
from tinkerfin_automation import Schedule

schedule = Schedule.cron("0 9 * * mon-fri", timezone="Asia/Shanghai")
```

Use `Schedule.once(at=...)` for one aware instant or
`Schedule.every(minutes=30, start_at=...)` for a stable aware interval anchor.
Interval components are nonnegative whole seconds, minutes, hours, and days; the
combined duration must be between one minute and 365 days. Reuse the anchor on
request retries. `ScheduleSpec` is the typed discriminated union of the three
schedule models; factories return those models directly.

All schedules accept aware `active_from` (inclusive) and `active_until` (exclusive).
To clear a bound, provide a complete replacement schedule. Manual runs remain
available outside the active period. Cron uses five fields and named weekdays,
skips DST gaps, and selects the earlier UTC occurrence in an overlap.
`MisfirePolicy` supports skip, latest (default), and bounded catch-up.

### Limits and execution identity

`ExecutionLimits` defaults to one concurrent run, ten queued runs, a 30-minute
execution deadline, and a 24-hour queue deadline. Limits apply per saved task or
per owner for taskless executions. `worker(global_concurrency=16)` limits unfinished
executions across workers.

`RunHandle.id` is the execution record ID; `RunHandle.identity.run_id` belongs to
the Runtime. Retrying creates a new handle and identity linked through `retry_of`.
A successful TinkerFin target may have `result=None`; read Agent messages through
the Runtime's Trace or your business storage.

### Task commands and retries

Obtain a handle with `await owner.task(task_id)`. Use `update`, `pause`, `enable`,
`delete`, and `run` on it. Successful task mutations update that handle's snapshot.
Separate handles do not refresh each other. Snapshot and result properties perform
no I/O and return detached data; call `refresh()` to read current state.

Task writes use the handle's observed revision unless `expected_revision` is supplied.
Always pass a remote form's original revision. Preserve the request ID, original
revision, input, and schedule anchor when retrying a command; a refreshed revision
with an old request ID is a different command. Conflicts are returned without retry.
Omitting `request_id` does not guarantee deduplication.

Pause affects future wakeups. Enable starts from current Store time without paused
catch-up. Delete cancels queued executions and retains history. For deletion retries
across requests, use `await owner.delete_task(task_id, expected_revision=revision,
request_id=request_id)` without first querying the deleted task.

Use `run.cancel()` to request cancellation, `run.retry()` for failed, timed-out, or
cancelled executions, and separately authorized `owner.resolve_run()` to settle
uncertain external work. Cancelling a running target need not stop external effects.

`await run.wait(timeout=30.0, poll_interval=1.0)` returns on success, failure, timeout,
cancellation, interruption, or a need for attention. The last two are not terminal
states. Observation timeout raises `AutomationWaitTimeout`; cancelling observation
does not cancel execution. Waiting retains the last successfully read snapshot,
propagates Store failures, and stops when Automation closes.

### Filter tasks and execution history

`owner.list_tasks(filters=TaskFilter(...))` and
`owner.list_runs(filters=ExecutionFilter(...))` return a `HandlePage` with `items`
and `next_cursor`. Pages default to 50 items and accept 1–100. Keep the owner,
filters, and optional task ID unchanged when following a cursor.
`owner.summarize_tasks()` and `owner.summarize_runs()` count all matching records.

The [active-period example](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-automation/examples/active_period.py)
shows a complete task, manual execution, filtering, and aggregation.

### Interrupts

Register Agent work with `automation.target("agent", TinkerFinTarget(runtime))`.
The execution identity must match the Runtime's namespace. By default, new work
uses the Automation namespace. To share one scheduler across Runtime scopes, bind
`automation.for_owner(owner_id, execution_namespace=runtime.namespace)` before
creating tasks or submitting one-time work. Each task and retry retains its saved
Runtime scope. Task input cannot replace the Runtime's model, tools, or namespace.

`automation.worker(on_interrupt=...)` can classify an unfinished graph execution.
The async callback receives `InterruptedExecution` and returns `None` to retain the
original deadline or `ExecutionFailure` to fail it. It cannot approve or resume the
graph. Unconfirmed external work retains protective capacity until authorized
resolution.

### SQL storage

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import Automation, SqlAlchemyAutomationStore

# Inside the host's async application entry point; targets is host-provided.
database = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(database)
try:
    automation = Automation(namespace="my-application", store=store)
    for name, target in targets.items():
        automation.target(name, target)
    async with automation.worker() as worker:
        await worker.check_ready()
        await host_shutdown.wait()
finally:
    try:
        await store.close()
    finally:
        await database.dispose()
```

Entering the context prepares storage. Worker exit closes Engine then Service;
explicit Stores and database engines remain host-owned. A supplied Scheduler
transfers its lifecycle to Automation. An instance can be entered once; do not
nest client and worker contexts. Close outside running targets and resource callbacks.
Shutdown joins accepted operations and owned cleanup, including on cancellation.

`async with automation` starts a query/submission client without local targets.
It can submit immediate work, query, manually run existing tasks, cancel, retry,
and resolve executions through a shared persistent Store and a remote worker.
Creating, editing, pausing, enabling, or deleting schedules requires a local
`automation.worker()` context; the default Scheduler does not discover remote
dynamic schedule changes. Workers in one namespace must handle every target they
can claim. Configure database connection and statement timeouts on the host engine.

### Agent tools

```python
from tinkerfin_automation import create_automation_tools

tools = create_automation_tools(
    automation.for_owner(authenticated_user_id),
    allowed_targets={"project_summary"},
)
```

The nine tools share the bound owner and require the corresponding active lifecycle.
Models cannot change ownership or target permissions. Existing-task execution checks
the observed target and revision. Tools do not expose cancellation, retry, resolution,
or graph resume. Hosts decide which agents receive management tools.

### Extensions

Common imports include Automation, owner/handle types, Schedule/ScheduleSpec,
results, ready-made Stores, targets, and tools. Specialized contracts live in:

| Goal | Public module |
| --- | --- |
| Manage low-level task commands | `tinkerfin_automation.service` |
| Operate an explicitly assembled worker | `tinkerfin_automation.engine` |
| Implement atomic storage | `tinkerfin_automation.store` |
| Provide task wakeups | `tinkerfin_automation.scheduler` |
| Supply or control time | `tinkerfin_automation.clock` |
| Materialize occurrences | `tinkerfin_automation.schedules` |
| Inspect tables and DDL | `tinkerfin_automation.sql_schema` |

`AutomationTarget.run(ExecutionRequest)` returns a normalized execution outcome.
Its `cancellation_is_final` declaration must reflect whether settled cancellation
proves external work stopped. Ordinary callable registration defaults to false;
use an explicit `FunctionTarget` only when a stronger guarantee is justified.

## Documentation

- [Automation guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/automation/index.md)
- [TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)
- [Source repository](https://github.com/tinkerfin-ai/tinkerfin-harness)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
