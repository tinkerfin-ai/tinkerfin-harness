# Run and schedule Agent tasks

[Documentation](../index.md) · [中文](../../cn/automation/index.md)

TinkerFin Automation runs trusted host targets immediately or from saved one-time,
fixed-rate, and Cron tasks with bounded concurrency. The default Store and Scheduler are
process-local. TinkerFin Agent execution and task-management tools are included; SQLite,
MySQL and PostgreSQL storage are optional.

## Execute once or save a task

```python
from datetime import UTC, datetime, timedelta

from tinkerfin_automation import (
    AutomationEngine,
    AutomationService,
    FunctionTarget,
    IntervalSchedule,
)


async def summarize(request):
    return {"project_id": request.execution.input["project_id"]}


async with AutomationService(namespace="my-application") as automation:
    async with AutomationEngine(
        automation,
        targets={"project_summary": FunctionTarget(summarize)},
    ) as worker:
        execution = await automation.execute_once(
            owner_id=authenticated_user_id,
            target="project_summary",
            input={"project_id": "project-42"},
            request_id="summarize-project-once",
        )
        await worker.wait_until_idle()
        result = await automation.get_execution(
            owner_id=authenticated_user_id,
            execution_id=execution.execution_id,
        )
```

The host supplies `authenticated_user_id` and authorizes the target. JSON input is
copied and persisted with finite JSON numbers; omit credentials and secrets. Automation does not use input strings to load code
or authenticate users. Use `get_task`, `list_tasks`,
`update_task`, `pause_task`, `enable_task`, and `delete_task` for definitions. Use
`execute_once`, `run_task_now`, `get_execution`, `list_executions`,
`cancel_execution`, `retry_execution`, and `resolve_execution` for execution history
and control.

The three one-run operations have different persistence behavior:

| Operation | Behavior |
| --- | --- |
| `execute_once(...)` | Runs immediately without creating a task; the execution has `task_id=None` |
| `run_task_now(task_id=...)` | Adds an immediate execution to an existing task without changing its schedule |
| `create_task(schedule=OnceSchedule(...))` | Saves a task for one future instant |

## Choose a schedule

In an application, create the Service and Engine once and keep their contexts open
for the application lifespan. The following command uses that active Service:

```python
task = await automation.create_task(
    owner_id=authenticated_user_id,
    name="Project summary",
    schedule=IntervalSchedule(
        every_seconds=3600,
        start_at=datetime.now(UTC) + timedelta(hours=1),
    ),
    target="project_summary",
    input={"project_id": "project-42"},
    request_id="create-project-summary",
)
```

- `OnceSchedule(at=...)` uses one aware instant.
- `IntervalSchedule(every_seconds=..., start_at=...)` uses a fixed-rate UTC anchor.
- `CronSchedule(expression=..., timezone=...)` uses five fields and an IANA timezone.

All schedules accept optional timezone-aware `active_from` (inclusive) and
`active_until` (exclusive). Scheduled occurrences, previews, and misfire catch-up
stay inside this interval. `run_task_now()` remains available outside it. To clear
a period, pass a complete replacement schedule with the corresponding bound unset.

Cron weekdays use `mon` through `sun`. A nonexistent local time is skipped; a repeated
local time runs only at the earlier UTC occurrence. `MisfirePolicy` supports `skip`,
`latest`, and bounded `catch_up`.

`ExecutionLimits` queue and concurrency limits apply per saved task. Taskless
`execute_once()` executions share those limits per owner. All executions also consume
the Engine's global concurrency capacity.

Namespace, owner, task name, target, and request IDs must be non-empty, trimmed UTF-8
strings without NUL bytes. Their limits are 128, 191, 255, 191,
and 128 Unicode characters respectively.

`AutomationEngine.close()` stops new work, allows running targets to finish within
`drain_timeout`, then cancels and joins the remaining tasks. Concurrent close calls
share cleanup; cancelling a caller waits for cleanup before propagating cancellation.
Use `await worker.check_ready()` in host readiness checks; it validates worker health
without dispatching work. A storage or renewal failure stops supervision and is reported by the Engine. Work
whose external completion is unconfirmed retains its concurrency reservation.


## Persistence

Install only the database driver you need:

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
```

`SqlAlchemyAutomationStore(engine)` supports SQLite, MySQL, and PostgreSQL through
a borrowed AsyncEngine. Install aiosqlite, asyncmy, or asyncpg separately; the SQL
extra does not include a driver. Close waits for accepted work; the host closes the
Engine. The first Service operation, or
`AutomationEngine.start()`, prepares storage automatically before work or Scheduler
startup. Call `await store.setup()` only for an explicit deployment readiness check or
when using the Store directly.

Setup creates all five tables when no Automation-owned table exists. If it finds any
`tinkerfin_automation_*` table, it validates the complete current Schema and rejects a
partial or incompatible shape without modifying it. Empty databases therefore require
DDL permission; pre-provisioned databases must already match the current Schema.
Workers using the same SQL Store share task ownership and concurrency capacity. The
Scheduler supplies scheduled wakeups.

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import SqlAlchemyAutomationStore

database = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(database)
try:
    async with AutomationService(namespace="my-application", store=store) as automation:
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
        await database.dispose()
```

The default Store belongs to the Service. A supplied Store and database Engine remain
caller-owned; a supplied Scheduler transfers its wakeup lifecycle to the Service and
Engine. Close the Engine before the Service, then the supplied Store and database.
Concurrent shutdown callers share cleanup; a cancelled waiter receives cancellation
after cleanup settles. Shutdown failures remain observable on repeated close calls.
Close from outside a running scheduler callback; waiting for its own owner to close
raises `AutomationLifecycleError`. A completed callback's detached child may close
normally, provided its host owns and joins that child.

Workers sharing a namespace must register the targets they can claim. Claiming does
not filter by target name; an unregistered target fails that execution.

## Task commands and execution results

All Service operations below are asynchronous. Pass a trusted `owner_id` to every
task or execution command. `list_tasks` and `list_executions` return `items` and
`next_cursor`; `limit` defaults to 50 and accepts 1–100.

| Task | API and behavior |
| --- | --- |
| Read definitions | `get_task`, `list_tasks` |
| Change definitions | `update_task`, `pause_task`, `enable_task`, `delete_task` require `expected_revision` |
| Run existing work | `run_task_now` optionally requires `expected_revision` at atomic queue admission |
| Inspect executions | `get_execution`, `list_executions(task_id=...)` |
| Stop an execution | `cancel_execution`; uncertain external work may require explicit resolution |
| Try again | `retry_execution` creates a new attempt after `failed`, `timed_out`, or `cancelled` |
| Resolve uncertainty | `resolve_execution` requires a resolution, reason, and request ID for `needs_attention` |

Pause changes future wakeups; enable starts from the current time without paused
catch-up. Delete cancels queued work and preserves execution history. Optional update
fields set to `None` retain their values; pass `input={}` to clear task input.

A `request_id` identifies one command and input within its namespace and owner.
Reuse it for request retries; a different command or input raises `RequestConflictError`.
An already committed run-now command returns its original result even if the task
revision has changed. A business retry creates another execution identity through
`retry_execution`, with `retry_of` pointing to the original attempt.

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

## Execute a TinkerFin Agent

```python
from tinkerfin import TinkerFin
from tinkerfin_automation import TinkerFinTarget

runtime = TinkerFin().with_namespace("my-application").build(model=model, tools=tools)
targets = {"project_summary": TinkerFinTarget(runtime)}
```

Use this mapping with the Engine examples above. `TinkerFinTarget` takes
`AgentRuntime[None]`, optionally with `mode="default"` or `mode="plan"`, and uses each
execution's assigned thread and run IDs. Task input cannot change the model, tools,
or namespace. Successful execution records completion with `result=None`; use the
Runtime's configured Trace or application storage for messages and Agent output.

Submit graph input to this target through the active Service:

```python
execution = await automation.execute_once(
    owner_id=authenticated_user_id,
    target="project_summary",
    input={"messages": [{"role": "user", "content": "Summarize the project."}]},
    request_id="agent-summary-request",
)
```

For ordinary `FunctionTarget` work, finite JSON returned by the async function becomes
the execution result. Its default `cancellation_is_final=False` avoids claiming that
cancelling a coroutine stopped external work. Set it to true only when that guarantee
holds for the target.

## Graph interrupts

A TinkerFin result with a non-empty `__interrupt__` collection becomes `interrupted`.
Automation never approves, rejects, or resumes it. An optional `on_interrupt` callback
returns `None` to keep waiting under the original execution deadline, or returns
`ExecutionFailure` to fail immediately. Callback failure fails the execution; callback
timeout uses the original execution deadline.

An interrupted run holds its task or taskless-owner concurrency slot without retaining
a coroutine or database connection. If cancellation or timeout cannot prove external
work stopped, the execution enters `needs_attention` and keeps protective capacity.
Only a separately authorized and audited `resolve_execution` releases that uncertainty.

## Agent-created tasks

Call `create_automation_tools()` with the authenticated owner and an allowed target
set. The returned tools are included in the default installation and use the same
Service as application code. Background tasks receive no management tools unless the
host explicitly adds them and enforces task count, frequency, concurrency, and
derivation limits.

`execute_automation_once` queues a taskless execution. `run_automation_task_now`
requires an existing task ID. Models provide task parameters and JSON input; the host
retains control of the owner, namespace, allowed targets, and execution limits.

All nine tools use the bound owner. Create, update, enable, immediate execution, and
run-now check the final target against `allowed_targets`; concurrent target changes
cannot replace the authorized work. Read, pause, and delete remain owner-scoped.
Mutation tools require `request_id`. They do not expose queue/misfire policy changes,
execution cancellation/retry/resolution, or Graph resume commands.

## Implement an extension

Use the existing public modules for specialized integration contracts:

| Module | Public contracts |
| --- | --- |
| `store` | `AutomationStore`, `WorkItemClaim`, `WorkKind`, `StartAuthorization`, `ScheduledExecution`, `MaterializationResult` |
| `scheduler` | `AutomationScheduler`, `MemoryScheduler`, `TaskDue` |
| `clock` | `AutomationClock`, `SystemClock`, `ManualClock` |
| `schedules` | `materialize_schedule`, `MaterializedSchedule`, schedule JSON conversion |
| `sql_schema` | `get_automation_store_schema`, `AutomationStoreSchema`, SQL tables |

`AutomationTarget.run(ExecutionRequest)` returns success, interruption, failure, or an
uncertain outcome. The request contains the persisted execution snapshot and deadline.
The target's `cancellation_is_final` property describes its actual external-stop guarantee.

A Scheduler implements `start(on_task_due)`, `schedule_task`, `remove_task`, and
`close`. It delivers task IDs; the Store remains authoritative for whether an
occurrence may be materialized. The callback must settle before its owner is closed.
A Clock provides `now()` and asynchronous `wait_until()`; `ManualClock.advance()` is
useful for deterministic tests.

Store implementations provide asynchronous operations for commands, queries, and status summaries. Their distinct atomic boundaries
are part of the contract:

| Operations | Required invariant |
| --- | --- |
| Task CRUD | Owner isolation, revision checks, command idempotency |
| Scheduled reads and `materialize_task` | Advance the observed wakeup and insert deduplicated occurrences together |
| `enqueue_execution` | Occurrence/command deduplication, queue capacity, and optional task-revision check in one boundary |
| `claim_work`, `renew_claim` | Reserve shared concurrency and enforce current ownership/fence |
| `authorize_start` | Persist the one-time start grant before target invocation |
| `mark_interrupted`, `finish_execution` | Preserve deadlines and release capacity only for a proven terminal outcome |
| Cancel and resolve | Retain uncertain capacity until authorized resolution |
| Setup, time, reads, close | Current schema validation, authoritative time, stable pagination, and borrowed-resource ownership |

The repository's `test_store_contract.py`, `test_store_atomicity.py`,
`test_sql_queue_admission.py`, and `test_tool_permissions.py` exercise these contracts.
The `store_with_clock` fixture runs shared scenarios against Memory and SQL stores.
Use deterministic clocks/signals and an isolated database when validating another
implementation; do not replay external target side effects to recover a database commit.
