# tinkerfin-sandbox

## What it is

`tinkerfin-sandbox` provides asynchronous Sandbox access, persistent bindings,
workspace files, commands, pause/resume, warm capacity, and cleanup. Its OpenSandbox
integration uses SDK 0.1.16 and Server 0.2.3.

The default [Sandbox image](https://github.com/tinkerfin-ai/sandbox-runtime) includes
Playwright and headless Chromium. Select another image with `OpenSandboxConfig(image=...)`.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin-sandbox
```

For persistent state, install the SQL extra and your database's asynchronous driver:

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

Use `asyncpg` for PostgreSQL or `asyncmy` for MySQL. Connection settings and Engine
ownership remain with your application.

## Quick Start

Start an OpenSandbox server, then create a manager and choose a Sandbox key:

```python
import asyncio

from opensandbox.config import ConnectionConfig
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig, OpenSandboxManager


async def main() -> None:
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
        config=OpenSandboxConfig(workspace_root="/workspace"),
    )
    async with OpenSandboxManager(client=client) as sandboxes:
        backend = await sandboxes.get("projects/project-1")
        result = await backend.aexecute("pwd")
        print(result.output)


asyncio.run(main())
```

A manager owns its client and State. Handles are borrowed and remain valid until the
manager closes or their Sandbox is destroyed. A caller-supplied HTTP transport remains
caller-owned.

## Keys and Runtime workspaces

Strings work directly as keys. Use user, session, project, or other business identifiers
to choose which calls share a Sandbox. Custom objects require `key_resolver`:

```python
sandboxes = OpenSandboxManager(client=client, key_resolver=lambda project: project.id)
```

With `tinkerfin` installed, a Runtime can prepare and release workspace access for each
run:

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin()
    .with_namespace("company-a")
    .build(
        model=model,
        backend=sandboxes.workspace("users/user-7"),
    )
)
```

The Runtime namespace and resolved key together select the Sandbox. Direct manager
calls select the same resource with `await sandboxes.get(key, namespace="company-a")`.
A workspace is borrowed for the run; finishing a run does not destroy a persistent
Sandbox or close the manager.

## Lifecycle

| Task | Method |
| --- | --- |
| Create, reconnect, or reuse | `get(key)` |
| Connect an existing binding | `reconnect(key)` |
| Replace an instance | `recreate(key)` |
| Clear workspace contents | `reset(key)` |
| Pause after active work settles | `pause(key, timeout=30.0)` |
| Resume the same instance | `resume(key, timeout=30.0)` |
| Destroy and remove the binding | `destroy(key)` |
| Inspect status | `get_details(key)` |
| Read provider diagnostics | `get_diagnostic_logs(key)`, `get_diagnostic_events(key)` |
| Check warm capacity | `check_ready()` |
| Close local resources | `aclose()` |

Recovery preserves the existing binding by default. Use
`OpenSandboxRecoveryPolicy(on_failure="recreate")` only when replacing the workspace
is acceptable; files are not copied. Commands, writes, and reset operations are never
replayed. Resource operations retain cleanup ownership when cancelled. Cancelling a
manager-close waiter leaves close running; call `aclose()` again to await its result.

The default remote TTL is two hours. `OpenSandboxConfig(ttl=None)` keeps newly created
instances until explicit destruction. In-memory State destroys its remote instances
on manager close; persistent State retains them. Use volumes and backups for files
that must survive instance loss. Pause does not stop or extend a finite TTL.

Pause waits for every registered holder to acknowledge idle. An unreachable holder
without idle evidence prevents pause. Diagnostic results are trusted operational data;
your application controls access to them.

Optional `observers=[observer]` receive lifecycle notifications through
`async on_sandbox_event(event)`. Notifications are ordered per observer and best effort;
callbacks must be asynchronous, propagate cancellation, and not reenter their manager.
See the [lifecycle guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/lifecycle.md)
for recovery and notification settings.

## Files and commands

`workspace_root="/workspace"` makes file-tool `/` refer to that directory. File tools
reject paths and links escaping the root; `reset()` clears its children and requires
a safe configured root. Shell commands are a separate unrestricted Sandbox capability,
so control command access through your tool and approval policy.

Use `await backend.aread_bytes("/report.pdf", max_bytes=10 * 1024 * 1024)` for a complete
bounded binary read. Oversized files raise `OpenSandboxFileTooLargeError`. Remote file
and command operations require asynchronous APIs.

Rooted transfers require Python 3, Linux procfs, and shared process visibility in the
Sandbox image. See [rooted files and commands](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/rooted-filesystem.md)
for permissions, transfer limits, and direct Deep Agents integration.

## Persistent state

SQLite, MySQL, and PostgreSQL use the same State API:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_sandbox import SQLAlchemyOpenSandboxState

engine = create_async_engine("sqlite+aiosqlite:////var/lib/app/sandboxes.db")
state = SQLAlchemyOpenSandboxState(engine=engine, namespace="production")
sandboxes = OpenSandboxManager(client=client, state=state)

try:
    async with sandboxes:
        backend = await sandboxes.get("projects/project-1")
finally:
    await engine.dispose()
```

State stores bindings, leases, warm slots, availability, and pending cleanup. It does
not store container files. Its `namespace` separates deployments sharing a database;
all workers in that deployment must agree on warm capacity.

Startup creates an empty schema or validates the existing tables, indexes, and database
comments. The first startup needs DDL permissions. Generate the complete schema with
`get_sqlalchemy_opensandbox_state_schema(dialect="postgresql")`; the descriptor exposes
`ddl` and `table_names`. `mysql` and `sqlite` are also accepted.

SQLite requires exclusive connection checkouts. For an in-memory database, use
`AsyncAdaptedQueuePool(pool_size=1, max_overflow=0)` instead of `StaticPool`.
Configure database connection and statement timeouts on the Engine. State options are
described in the [persistence guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/persistence-and-extensions.md).

## Documentation

- [Sandbox guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/index.md)
- [Persistent state and extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/persistence-and-extensions.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
