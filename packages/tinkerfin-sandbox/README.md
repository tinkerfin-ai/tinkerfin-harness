# tinkerfin-sandbox

## What it is

`tinkerfin-sandbox` provides isolated project workspaces, asynchronous files and
commands, persistent Sandbox bindings, pause/resume, warm capacity, and cleanup. Its OpenSandbox
integration uses SDK 0.1.16 and Server 0.2.3.

The [Sandbox runtime image](https://github.com/tinkerfin-ai/sandbox-runtime) includes
Playwright and headless Chromium. Isolated workspaces require the matching
[Server](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-server/README.md#deploy)
and [execd](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-execd/README.md#deploy)
deployment. Select a runtime image with `OpenSandboxConfig(image=...)`.

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

Deploy the Server and runtime, then choose a Sandbox owner and a project:

```python
import asyncio

from opensandbox.config import ConnectionConfig

from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxManager


async def main() -> None:
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
    )
    async with OpenSandboxManager(client=client) as sandboxes:
        project = sandboxes.workspace("users/7", workspace_key="project-a")
        async with project.open() as files:
            await files.aupload_files([("/hello.txt", b"hello")])
            result = await files.aexecute("cat hello.txt")
            print(result.output)


asyncio.run(main())
```

A manager owns its client and State and must outlive workspace access. Leaving
`project.open()` stops that access's processes and network activity; project files
remain in the Sandbox. A caller-supplied HTTP transport remains caller-owned.

## Keys and Runtime workspaces

The first key selects the physical Sandbox owner, such as a user. `workspace_key`
selects a project inside that Sandbox. Projects with the same owner share one physical
instance while keeping separate files, HOME, caches, and dependencies. String owner
keys work directly; custom objects require `key_resolver`:

```python
sandboxes = OpenSandboxManager(client=client, key_resolver=lambda user: user.id)
```

With `tinkerfin` installed, a Runtime can prepare and release workspace access for each
run:

```python
from tinkerfin import TinkerFin

project = sandboxes.workspace("users/7", workspace_key="project-a")
runtime = (
    TinkerFin()
    .with_namespace("projects/project-a")
    .build(
        model=model,
        backend=project,
    )
)
```

The returned `SandboxWorkspace` is a lazy declaration. The Runtime prepares and closes
isolated access for each run, while the same project retains files across conversations.
The Runtime namespace scopes logical persistence; changing it does not select another
Sandbox or project. Finishing a run does not destroy the Sandbox or close the manager.

## Lifecycle

| Task | Method |
| --- | --- |
| Borrow project files and commands | `async with project.open() as files` |
| Observe an existing project's file-root changes | `async with project.watch() as changes` |
| Stop a project's runs and delete its files, HOME, caches, and dependencies | `await project.delete()` |
| Open or reuse a raw command Sandbox | `get(key)` |
| Reconnect a raw command Sandbox | `reconnect(key)` |
| Replace a raw command Sandbox | `recreate(key)` |
| Clear a raw command Sandbox's configured file root | `reset(key)` |
| Pause the owner's entire Sandbox after active work settles | `pause(key, timeout=30.0)` |
| Resume the owner's entire Sandbox | `resume(key, timeout=30.0)` |
| Destroy the owner's Sandbox and all its projects | `destroy(key)` |
| Inspect status | `get_details(key)` |
| Read provider diagnostics | `get_diagnostic_logs(key)`, `get_diagnostic_events(key)` |
| Check warm capacity | `check_ready()` |
| Close local resources | `aclose()` |

`project.delete()` leaves other projects and the physical Sandbox intact. Raw command
methods reject an owner already used for isolated workspaces. Pause and resume return
`None`; `project.open()` does not automatically resume a paused Sandbox.

Recovery preserves the existing binding by default. Use
`OpenSandboxRecoveryPolicy(on_failure="recreate")` only when losing all data in the
physical Sandbox is acceptable; files are not copied. Commands, writes, and reset operations are never
replayed. Resource operations retain cleanup ownership when cancelled. Cancelling a
manager-close waiter leaves close running; call `aclose()` again to await its result.

The default remote TTL is two hours. `OpenSandboxConfig(ttl=None)` keeps newly created
instances until explicit destruction. In-memory State destroys its remote instances
on manager close; persistent State retains them. Project data lasts only as long as
the physical Sandbox. Back up files that must survive instance loss. Pause does not
stop or extend a finite TTL.

Pause waits for every registered holder to acknowledge idle. An unreachable holder
without idle evidence prevents pause. Diagnostic results are trusted operational data;
your application controls access to them.

Optional `observers=[observer]` receive lifecycle notifications through
`async on_sandbox_event(event)`. Notifications are ordered per observer and best effort;
callbacks must be asynchronous, propagate cancellation, and not reenter their manager.
See the [lifecycle guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/lifecycle.md)
for recovery and notification settings.

## Files and commands

An isolated workspace exposes its project files at file-tool `/`. Shell commands,
uploads, downloads, and output captures use the same project isolation. Each run has
private processes, temporary files, devices, and networking, with shared tools mounted
read-only. Public HTTP on port 80 and HTTPS on port 443 use a managed proxy; processes
and network activity end with the run. HOME, caches, and installed project dependencies
remain available to later runs of that project.

`workspace_root` only configures file-path mapping for raw command Sandboxes obtained
with `get()`. It does not restrict their Shell commands or configure isolated projects.

Use `await backend.aread_bytes("/report.pdf", max_bytes=10 * 1024 * 1024)` for a complete
bounded binary read. Oversized files raise `OpenSandboxFileTooLargeError`. Remote file
and command operations require asynchronous APIs.

See [rooted files and commands](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/rooted-filesystem.md)
for permissions, transfer limits, and direct Deep Agents integration.

Use `project.list_directory()` for paginated existing files,
`project.get_file_info(path)` for metadata, or
`project.read_text(path, max_bytes=100 * 1024, max_lines=200)` for a bounded UTF-8
preview. These methods do not create, initialize, or resume resources. An absent or
deleted workspace raises `OpenSandboxWorkspaceNotInitializedError`; binary files
raise `OpenSandboxNotTextError` on text reads. A file-changed error invalidates the
current read or directory cursor. Symbolic links are never followed.

For an existing running project, subscribe before reading the initial file state:

```python
async with project.watch() as changes:
    print(await project.list_directory("/"))
    async for change in changes:
        print(await project.list_directory("/"))
```

The iterator yields `WorkspaceChange.FILES_CHANGED` from `tinkerfin_sandbox` or
`ResyncRequired` from `tinkerfin_notifications`. These are root-wide hints: read the
current directory or file state after a change, and reread it whenever resynchronization
is required. Hints can be combined and do not contain file contents or operation history.

Watching creates no Sandbox, resumes none, and runs no initialization. Pausing, deleting,
or losing the selected instance yields a disconnected resync and ends that watch; enter
a new context after the project is available. A compatible runtime collector is required.
See [Watch file changes](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/rooted-filesystem.md#watch-file-changes)
for change coverage and [Manager configuration](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/lifecycle.md#manager-configuration)
for cross-worker notifications.

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
        project = sandboxes.workspace("users/7", workspace_key="project-a")
        async with project.open() as files:
            await files.awrite("/notes.txt", "hello")
finally:
    await engine.dispose()
```

State persists Sandbox bindings and lifecycle coordination, not project files.
Its `namespace` separates deployments sharing a database; all workers in that deployment must agree on
warm capacity. Warm instances serve raw command Sandboxes; isolated workspaces create
their own instances.

Startup creates or validates the database structure. The first startup needs DDL
permissions. Configure connection and statement timeouts on the Engine. Schema
generation, SQLite connection requirements, and State options are described in the
[persistence guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/persistence-and-extensions.md).

## Documentation

- [Sandbox guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/index.md)
- [Persistent state and extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/persistence-and-extensions.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE).
