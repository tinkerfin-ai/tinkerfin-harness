# TinkerFin Automation

## What it is

TinkerFin Automation executes registered application targets immediately or from saved
schedules through a bounded asynchronous worker. It provides task CRUD, one-time and
recurring schedules, execution history, command idempotency, explicit retries,
cancellation, queue and execution deadlines, and cross-worker database coordination.

The host keeps ownership of authorization, executable targets, Runtime configuration,
credentials, and user interaction. Automation records graph interrupts but never
approves, rejects, or resumes them.

## Installation

Install Automation with its in-memory defaults, TinkerFin target, and Agent task
tools:

```bash
pip install tinkerfin-automation
```

For SQL storage, install the SQL extra and your asynchronous driver:

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
# PostgreSQL: asyncpg; MySQL: asyncmy
```

Python 3.11 or newer is required. Scheduling, TinkerFin execution, and Agent task
management need no extra. SQL storage supports SQLite, MySQL, and PostgreSQL. The
SQL extra does not select a driver.

## Quick Start

The default Service uses a process-local Store and Scheduler:

```python
import asyncio

from tinkerfin_automation import (
    AutomationEngine,
    AutomationService,
    FunctionTarget,
)


async def create_summary(request):
    project_id = request.execution.input["project_id"]
    return {"project_id": project_id, "status": "complete"}


async def main() -> None:
    async with AutomationService(namespace="my-application") as automation:
        async with AutomationEngine(
            automation,
            targets={"project_summary": FunctionTarget(create_summary)},
        ) as worker:
            execution = await automation.execute_once(
                owner_id="user-123",
                target="project_summary",
                input={"project_id": "project-42"},
                request_id="summarize-project-once",
            )
            await worker.wait_until_idle()
            result = await automation.get_execution(
                owner_id="user-123",
                execution_id=execution.execution_id,
            )
            print(result.status, result.result)


asyncio.run(main())
```

`owner_id` must come from trusted host authentication. It is a lookup scope, not a
credential. A saved task or immediate execution stores a registered target name and
JSON input. Input and result snapshots are independent of saved records and require
finite JSON numbers; the host must omit credentials and secrets.
Automation does not use input strings to load code or authenticate users.

Use the API that matches the intended lifetime:

| Operation | Saved task | Observable result |
| --- | --- | --- |
| `execute_once(...)` | No | Queue one immediate execution with `task_id=None` |
| `run_task_now(task_id=...)` | Already exists | Queue an additional execution without changing its schedule |
| `create_task(schedule=OnceSchedule(...))` | Yes | Save a task that runs at one future instant |

To execute a registered TinkerFin Agent, use the included target:

```python
from tinkerfin import TinkerFin
from tinkerfin_automation import TinkerFinTarget

runtime = TinkerFin().with_namespace("app").build(model=model, tools=tools)
target = TinkerFinTarget(runtime)
```

The Runtime and Automation service must use the same namespace. Task input cannot
replace the Runtime's model, tools, or namespace.
The target records completion or interrupt IDs, not the Agent's messages or complete
state. Read those through the Runtime's configured Trace or application storage.

## Core concepts

### Schedules

- `OnceSchedule(at=...)` creates one future opportunity.
- `IntervalSchedule(every_seconds=..., start_at=...)` uses a fixed-rate UTC anchor.
- `CronSchedule(expression=..., timezone=...)` uses five fields and an explicit IANA
  timezone. Weekdays use `mon` through `sun` names.

All schedules accept optional timezone-aware `active_from` (inclusive) and
`active_until` (exclusive). Scheduled occurrences, previews, and misfire catch-up
stay inside this interval. `run_task_now()` remains available outside it. To clear
a period, pass a complete replacement schedule with the corresponding bound unset.

Cron skips nonexistent local times during a DST gap. When a local time occurs twice,
only the earlier UTC occurrence runs. `MisfirePolicy` selects `skip`, `latest`, or
bounded `catch_up`; `latest` is the default.

### Limits and execution identity

`ExecutionLimits` defaults to one concurrent run, ten queued runs, a 30-minute
execution deadline, and a 24-hour queue deadline. Limits are shared per task for saved
task executions and per owner for taskless `execute_once()` executions.
`AutomationEngine` defaults to 16 global concurrent executions.

`AutomationEngine.close()` stops new work, allows running targets to finish within
`drain_timeout`, then cancels and joins the remaining tasks. Concurrent close calls
share cleanup; cancelling a caller waits for cleanup before propagating cancellation.
Use `await worker.check_ready()` in host readiness checks; it validates worker health
without dispatching work. A storage or renewal failure stops supervision and is reported by the Engine. Work
whose external completion is unconfirmed retains its concurrency reservation.


Every execution receives a `RunIdentity(namespace, thread_id, run_id)` before target invocation.
Infrastructure retries reuse the persisted execution. `retry_execution()` creates a
new business attempt and identity linked through `retry_of`.
Namespace, owner, name, target, and request IDs must be non-empty, trimmed UTF-8
strings without NUL bytes. Their limits are 128, 191, 255, 191,
and 128 Unicode characters respectively.

### Task commands and retries

`get_task`, `list_tasks`, `update_task`, `pause_task`, `enable_task`, and `delete_task`
manage task definitions. Updates, pause, enable, and delete require the task's
`expected_revision`. Pause affects future wakeups; deletion cancels queued executions
and retains history. `get_execution` and `list_executions` read execution records.

Use `cancel_execution` to request cancellation, `retry_execution` to create a new
attempt after failure, timeout, or cancellation, and `resolve_execution` to record a
host-confirmed outcome for `needs_attention`. A supplied `request_id` identifies the
same command and input within its namespace and owner. Reuse it when retrying a
request; conflicting input raises `RequestConflictError`.

`run_task_now` optionally accepts `expected_revision` when execution must use a
specific task revision. New queue admission checks it atomically; an existing
idempotent result is returned even if the task has since changed.

### Filter tasks and execution history

Pass `TaskFilter(name_contains=..., statuses=(... ,))` to `list_tasks(filters=...)`.
Use `ExecutionFilter` with the same name/status options and optional `queued_from`
and `queued_until` for execution history. Text is a literal Unicode-casefold
substring; `%` and `_` are ordinary characters. Queue time bounds are aware instants,
with an inclusive start and exclusive end. Empty statuses mean every status.
Execution names are captured when queued, survive task deletion, and do not follow
later renames. Taskless or unknown historical names may be `None`.

Both list methods return `items` and `next_cursor`. Pass that opaque cursor unchanged
with the same owner and filters (and task ID for execution queries); start without a
cursor when changing the query. Pages are ordered by creation time and ID, newest
first, and remain usable if the preceding row is deleted. They are live queries,
not a frozen snapshot. `summarize_tasks()` and `summarize_executions()` accept the same
filters and count every matching record by status, independently of pagination.

The [active-period example](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-automation/examples/active_period.py)
shows a complete local task, manual execution, filtering, and aggregation without
network services or model credentials.

### Interrupts

A non-empty TinkerFin `__interrupt__` result produces an `interrupted` execution. The
engine does not submit a LangGraph resume command. An optional callback may classify
the unfinished run:

```python
from tinkerfin_automation import ExecutionFailure, InterruptedExecution


async def on_interrupt(
    execution: InterruptedExecution,
) -> ExecutionFailure | None:
    if "blocked-interaction" in execution.interrupt_ids:
        return ExecutionFailure(
            code="application.interrupt_rejected",
            message="The application will not continue this execution",
        )
    return None


worker = AutomationEngine(automation, targets=targets, on_interrupt=on_interrupt)
```

Returning `None`, or omitting the callback, keeps the execution unfinished under its
original deadline. Returning `ExecutionFailure` fails it. Callback exceptions fail the
execution, and a callback that reaches the original deadline is cancelled and timed
out. The callback cannot approve, reject, or resume the graph.

### SQL storage

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import AutomationService, SqlAlchemyAutomationStore

engine = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(engine)  # borrows the engine
automation = AutomationService(namespace="my-application", store=store)
```

Close the worker before the service, then the borrowed Store and database Engine.
Use `try/finally` for the two caller-owned resources, including initialization errors:

```python
try:
    async with automation:
        async with AutomationEngine(automation, targets=targets) as worker:
            execution = await automation.execute_once(
                owner_id=authenticated_user_id,
                target="project_summary",
                input={"project_id": "project-42"},
                request_id="summary-request",
            )
            await worker.wait_until_idle()
finally:
    try:
        await store.close()
    finally:
        await engine.dispose()
```

The first Service operation, or `AutomationEngine.start()`, prepares the Store before
using it. Deployment readiness checks and direct Store users may call
`await store.setup()` explicitly. Setup creates empty storage or verifies its complete
current structure, rejecting partial or incompatible storage without changing it.

`SqlAlchemyAutomationStore(engine)` borrows the Engine; close the Store before
calling `await engine.dispose()`. `close()` rejects new work and waits for accepted
operations, including when the close waiter is cancelled. Cancelling a write before
COMMIT rolls it back after its current statement finishes; a lost commit
acknowledgement is never replayed automatically. Repeat the same command ID and input
to read its committed result.

Each operation uses a separate connection and transaction. Pass an AsyncEngine with
exclusive pool checkouts, not a Session or active transaction. Empty databases need
DDL permission; pre-provisioned tables must match the schema. Schema setup uses a
30-second lock wait. The Engine owner sets other connection, statement, and lock-wait
timeouts.

The default Store belongs to the Service; an explicitly supplied Store remains
borrowed. A supplied Scheduler gives its wakeup lifecycle to the Service and Engine.
Service and Scheduler shutdown wait for accepted cleanup even if a close waiter is
cancelled, and retain failures on repeated close calls. Call shutdown from outside
active scheduler callbacks; reentrant shutdown raises
`AutomationLifecycleError`. Workers sharing a namespace
must register the targets they can claim: work is not routed by target name, and an
unregistered target fails that execution.

### Agent tools

```python
from tinkerfin_automation import create_automation_tools

tools = create_automation_tools(
    automation,
    owner_id=authenticated_user_id,
    allowed_targets={"project_summary"},
)
```

The host binds `owner_id` and an allowed target set before exposing the tools. Models
can select a permitted target and provide JSON input, but cannot change those
permissions or the service namespace. The returned set includes `execute_automation_once` for taskless execution
and `run_automation_task_now` for an existing task. These tools are included in the
default installation.

Existing-task updates, enable, and run-now operations also check the resulting
target. Concurrent edits cannot change an authorized task into another target during
the operation. Read, pause, and delete remain scoped to the bound owner.

### Extensions

Use the root imports for Service, Engine, schedules, results, ready-made Stores,
targets, and tools. Implement specialized integrations through their public modules:

| Goal | Public module |
| --- | --- |
| Implement the atomic storage contract | `tinkerfin_automation.store` |
| Provide task wakeups | `tinkerfin_automation.scheduler` |
| Supply or control time | `tinkerfin_automation.clock` |
| Materialize schedule occurrences | `tinkerfin_automation.schedules` |
| Inspect SQL tables and DDL | `tinkerfin_automation.sql_schema` |

`AutomationTarget` has an async `run(request)` method and a
`cancellation_is_final` property. The property must describe whether settled
coroutine cancellation proves external work stopped; it defaults to false in
`FunctionTarget` and remains false in `TinkerFinTarget`.

The [Automation guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/automation/index.md)
describes Scheduler and Store ownership, atomic operations, and contract validation.

## Documentation

- [Automation guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/automation/index.md)
- [TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)
- [Source repository](https://github.com/tinkerfin-ai/tinkerfin-harness)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
