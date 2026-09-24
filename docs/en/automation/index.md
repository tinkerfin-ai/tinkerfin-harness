# Run and schedule Agent tasks

[Documentation](../index.md) · [中文](../../cn/automation/index.md)

TinkerFin Automation runs host-registered targets immediately or on saved one-time,
interval, and Cron schedules. Owner-bound task and execution handles manage work;
Stores coordinate request idempotency, bounded queues, and cross-worker capacity.
The default Store is process-local; SQL persistence supports SQLite, MySQL, and PostgreSQL.

## Execute once or save a task

Place this fragment inside the host's async entry point. authenticated_user_id is a
string obtained from host authentication.

```python
from tinkerfin_automation import Automation, ExecutionRequest, Schedule


automation = Automation(namespace="my-application")


@automation.target("project_summary")
async def summarize(request: ExecutionRequest) -> dict[str, str]:
    return {"project_id": str(request.input["project_id"])}


async with automation.worker():
    owner = automation.for_owner(authenticated_user_id)
    run = await owner.run(
        "project_summary",
        input={"project_id": "project-42"},
        request_id="summary-once",
    )
    await run.wait(timeout=30.0)
    print(run.status, run.result)
```

`owner.run()` submits taskless work, `owner.create_task()` saves an enabled definition,
and `task.run()` adds an execution to a saved task. Submission returns a RunHandle;
it does not wait for completion. An owner is an isolation scope, not a credential.
The host must authorize targets and omit secrets from persisted JSON input.

## Choose a schedule

Create tasks inside an active worker context:

```python
task = await owner.create_task(
    name="Project summary",
    target="project_summary",
    schedule=Schedule.cron("0 9 * * mon-fri", timezone="Asia/Shanghai"),
    input={"project_id": "project-42"},
    request_id="create-summary",
)
```

- `Schedule.once(at=...)` uses one timezone-aware instant.
- `Schedule.every(minutes=30, start_at=...)` uses a stable anchor. Nonnegative integer
  seconds/minutes/hours/days combine to an interval between one minute and 365 days.
- `Schedule.cron(expression, timezone=...)` uses five fields and an explicit IANA zone,
  with named weekdays from mon to sun.

Factories return the three schedule models; annotate values with `ScheduleSpec`,
the discriminated union. All factories accept inclusive active_from and exclusive
active_until. Clear a bound by supplying a complete replacement schedule. Manual
runs remain available outside the period. Reuse at/start_at when replaying a request.
Cron skips DST gaps and selects the earlier UTC occurrence in an overlap.
MisfirePolicy defaults to latest, with skip and bounded catch_up also available.

ExecutionLimits defaults to one concurrent run, ten queued runs, a 30-minute execution
deadline and a 24-hour queue deadline. Taskless work shares owner-level limits.
The worker defaults to global_concurrency=16.

## Persistence

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
```

The SQL extra supports SQLite, MySQL, and PostgreSQL; choose asyncmy for MySQL or
asyncpg for PostgreSQL. The host owns the async database engine and configures
connection and statement timeouts. host_shutdown below is supplied by the host:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import Automation, SqlAlchemyAutomationStore


database = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(database)
try:
    automation = Automation(namespace="my-application", store=store)
    automation.target("project_summary", summarize)
    async with automation.worker() as worker:
        await worker.check_ready()
        await host_shutdown.wait()
finally:
    try:
        await store.close()
    finally:
        await database.dispose()
```

Entry prepares storage. Worker exit closes Engine then Service; the explicitly supplied
Store and database remain host-owned. Automation owns its default Store and assumes
a supplied Scheduler's lifecycle. Shutdown stops admission, waits for accepted
operations, and joins cleanup even on cancellation. Do not close Automation from
one of its running targets or resource callbacks.

An instance can be entered once. Client and worker contexts cannot be nested.
`async with automation` starts no local Worker; with shared durable storage and a
remote Worker it supports immediate submission, queries, manual task execution,
cancellation, retries, and settlement. Creating, editing, pausing, enabling, and
deleting schedules requires local worker mode. Shared SQL alone does not provide
dynamic remote schedule discovery. Workers in one namespace must support all targets
they can claim; claims are not routed by target name.

Store setup creates empty storage or verifies the complete current structure without
repairing partial tables. Empty storage requires DDL permission. Direct Store users
can call `await store.setup()`. Database pool checkouts must be exclusive.

## Task commands and execution results

Bind identity once with automation.for_owner(); returned objects retain that scope.

| Task | API and behavior |
| --- | --- |
| Read definitions | `owner.task(id)`, `owner.list_tasks()` return TaskHandles |
| Change definitions | `task.update/pause/enable/delete` use the observed or explicit expected_revision |
| Run a saved task | `task.run()` adds an execution without changing the schedule |
| Inspect executions | `owner.get_run(execution_id)`, `owner.list_runs(task_id=...)` |
| Wait for an outcome | `run.wait(timeout=30.0, poll_interval=1.0)` observes only this execution |
| Request cancellation | `run.cancel()`; cancel_requested is not cancelled |
| Try again | `run.retry()` returns a new handle for failed/timed_out/cancelled work |
| Resolve uncertainty | Independently authorized and audited `owner.resolve_run()`; never Graph resume |

Handle properties perform no I/O. snapshot/result values are detached; refresh reads
current state explicitly. Successful mutations update that handle; separate handles
do not refresh each other. Failures do not trigger refresh or retry.
Pause changes future wakeups; enable does not catch up the paused period. Delete
cancels queued work and retains history and the local last snapshot. None leaves an
update field unchanged; input={} clears input.

Preserve a remote form's original revision instead of substituting a newer lookup:

```python
task = await owner.task(task_id)
await task.pause(
    expected_revision=command.expected_revision,
    request_id=command.request_id,
)
```

Replay the same request_id, original revision, input, and schedule anchor. Omitting
request_id does not guarantee deduplication. Different commands sharing a key conflict.
Across requests, replay deletion with
`owner.delete_task(task_id, expected_revision=revision, request_id=request_id)` without
first querying the deleted definition. A response failure after COMMIT does not prove
that no write happened; do not replace the request key and create again.

wait accepts finite positive seconds, covering locks, reads, and intervals. Observation
timeout raises AutomationWaitTimeout; cancelling observation does not cancel execution.
Terminal states and interrupted/needs_attention return; the latter two remain nonterminal.
Read business failure through status and snapshot.failure_code/failure_message; Store
errors propagate. RunHandle.id is the execution record ID; identity.run_id belongs to
the Runtime. A successful result may be None.

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
not a frozen snapshot. `summarize_tasks()` and `summarize_runs()` accept the same
filters and count every matching record by status, independently of pagination.

The [active-period example](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-automation/examples/active_period.py)
shows a complete local task, manual execution, filtering, and aggregation without
network services or model credentials.

## Execute a TinkerFin Agent

The host provides model, tools, and the authenticated identity:

```python
from tinkerfin import TinkerFin
from tinkerfin_automation import Automation, TinkerFinTarget

runtime = TinkerFin().with_namespace("my-application").build(model=model, tools=tools)
automation = Automation(namespace=runtime.namespace)
automation.target("agent", TinkerFinTarget(runtime))
async with automation.worker():
    owner = automation.for_owner(authenticated_user_id)
    run = await owner.run(
        "agent",
        input={"messages": [{"role": "user", "content": "Summarize the project"}]},
        request_id="agent-summary",
    )
    await run.wait()
```

The execution identity must match the Runtime's namespace. New work defaults to the
Automation namespace. To share one scheduler across Runtime scopes, bind
`automation.for_owner(owner_id, execution_namespace=runtime.namespace)` before
creating tasks or submitting one-time work. Tasks and retries retain their saved
Runtime scope. Targets use the assigned execution
identity; input cannot replace model, tools, or namespace. TinkerFinTarget records
success with result=None; obtain messages through configured Trace or business storage.
An ordinary async callable's finite JSON return value becomes its execution result.
Cancellation guarantees default to false. Use an explicit
FunctionTarget(..., cancellation_is_final=True) only when external work is provably stopped.

## Graph interrupts

A nonempty native interrupt becomes interrupted. Automation does not approve or resume
the graph. An async automation.worker(on_interrupt=...) callback returns None to retain
the original execution deadline or ExecutionFailure to fail. Callback errors fail the
execution; timeout uses the original deadline. Interrupts retain capacity. Unconfirmed
external termination becomes needs_attention until separately authorized, audited
owner.resolve_run settlement.

## Agent-created tasks

```python
from tinkerfin_automation import create_automation_tools

tools = create_automation_tools(
    automation.for_owner(authenticated_user_id),
    allowed_targets={"project_summary"},
)
```

The nine tools share the bound owner. Schedule writes require an active worker;
models cannot replace owner, namespace, or target permissions. execute_automation_once
submits taskless work; run_automation_task_now verifies the saved target and observed
revision. Mutations require stable request_id values. Tools do not expose execution
cancellation, retries, settlement, or Graph resume. Hosts select which agents receive
management tools and constrain derived task counts, frequency, and permissions.

## Implement an extension

Use the existing public modules for specialized integration contracts:

| Module | Public contracts |
| --- | --- |
| `service` / `engine` | `AutomationService` / `AutomationEngine` |
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
