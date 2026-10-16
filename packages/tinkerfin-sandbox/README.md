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
`project.open()` stops that access's processes and network activity while retaining
project files. With the default in-memory State, closing the manager destroys its
remote Sandboxes. A caller-supplied HTTP transport remains caller-owned.

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

## Files and changes

An isolated workspace exposes its project files at file-tool `/`. Each run has
private processes, temporary files, devices, and networking. HOME, caches, and
installed dependencies remain available to later runs of that project. Public HTTP
and HTTPS use a managed proxy; processes and network activity end with the run.

All remote file and command operations are asynchronous. Use
`await files.aread_bytes("/report.pdf", max_bytes=10 * 1024 * 1024)` for a bounded
binary read. Oversized files raise `OpenSandboxFileTooLargeError`.

For existing files, `project.list_directory()`, `project.get_file_info(path)`, and
`project.read_text(path, max_bytes=..., max_lines=...)` provide bounded browsing
without creating, initializing, or resuming a workspace. Symbolic links are never
followed. Missing workspaces raise `OpenSandboxWorkspaceNotInitializedError`, and
binary text reads raise `OpenSandboxNotTextError`.

Subscribe before reading an existing running project's initial file state:

```python
async with project.watch() as changes:
    print(await project.list_directory("/"))
    async for change in changes:
        print(await project.list_directory("/"))
```

`WorkspaceChange.FILES_CHANGED` and `ResyncRequired` are hints to reread authoritative
state; they contain no file contents or operation history and can be combined.
Pausing, deleting, or losing the instance ends the watch with a disconnected resync.
Enter a new watch after the project becomes available. Watching creates or resumes
no resources and requires a compatible runtime collector. See the
[file guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/rooted-filesystem.md)
for transfer limits, permissions, and change coverage.

## Cancellation and data lifetime

- `await project.delete()` stops that project's runs and deletes its files, HOME,
  caches, and dependencies while leaving other projects and the Sandbox intact.
- `await sandboxes.pause(key)` pauses the owner's entire Sandbox after active work
  settles. Use `resume(key)` explicitly; `project.open()` does not resume it.
- `await sandboxes.destroy(key)` destroys the owner's Sandbox and every project in it.

The default remote TTL is two hours. `OpenSandboxConfig(ttl=None)` keeps newly
created instances until explicit destruction. In-memory State destroys remote
instances when the manager closes; persistent State retains them. Pause does not
stop or extend a finite TTL. Back up files that must survive instance loss.

Recovery preserves existing bindings by default.
`OpenSandboxRecoveryPolicy(on_failure="recreate")` permits loss of all data in the
physical Sandbox; files are not copied. Commands, writes, and reset operations are
never replayed. Cancellation retains cleanup ownership. If a manager-close waiter
is cancelled, call `aclose()` again to await the continuing close operation before
releasing borrowed resources.

Raw command Sandboxes are available through `get(key)` and related manager methods.
They cannot share an owner already used for isolated workspaces. Their
`workspace_root` maps file paths but does not restrict shell commands. See the
[lifecycle guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/lifecycle.md)
for raw commands, recovery, pause/resume, diagnostics, and observers. Applications
control access to trusted diagnostic output.

## Persistent state

For SQLite, MySQL, or PostgreSQL, create
`SQLAlchemyOpenSandboxState(engine=engine, namespace="production")` and pass it as
`state` to `OpenSandboxManager`. The manager owns State; the host owns the
`AsyncEngine` and disposes it after the manager closes. State saves bindings and
coordination, not project file bytes.

State `namespace` separates deployments sharing a database. Workers in one deployment
must agree on warm capacity. Warm instances serve raw command Sandboxes; isolated
workspaces create their own instances. Startup creates or validates the schema and
requires DDL permissions when creating it. Configure connection and statement
timeouts on the Engine. The persistence guide provides complete SQL setup examples.

## Documentation

- [Sandbox guide](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/index.md)
- [Files and commands](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/rooted-filesystem.md)
- [Lifecycle and recovery](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/lifecycle.md)
- [Persistent state and extensions](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/persistence-and-extensions.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/sandbox/api-reference.md)

## License

[Apache License 2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
