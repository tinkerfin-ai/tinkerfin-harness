# Sandbox

[Documentation](../index.md) · [中文](../../cn/sandbox/index.md)

`tinkerfin-sandbox` provides asynchronous files, commands, reusable isolated
environments, persistent bindings, warm capacity, pause, resume, and cleanup.

## Installation

```bash
pip install tinkerfin-sandbox
```

For persistent bindings, install the SQLAlchemy extra and one asynchronous driver:

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

Use `asyncpg` for PostgreSQL or `asyncmy` for MySQL.

Isolated workspaces require the TinkerFin [runtime image](https://github.com/tinkerfin-ai/sandbox-runtime)
with its matching [Server](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-server/README.md#deploy)
and [execd](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-execd/README.md#deploy)
deployment. Applications use the manager API; the framework configures each run's
environment, authentication, and cleanup.

## Use a Sandbox with AgentRuntime

The application chooses the Sandbox owner and project:

- `key` selects the physical Sandbox, for example `"users/7"`.
- `workspace_key` selects a project's files, HOME, caches, and dependencies within it.

The Runtime `namespace` scopes logical persistence. It does not change which physical
Sandbox or project a workspace uses.

```python
from opensandbox.config import ConnectionConfig

from tinkerfin import TinkerFin
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxManager

client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
)

async with OpenSandboxManager(client=client) as sandboxes:
    project = sandboxes.workspace("users/7", workspace_key="project-a")
    runtime = (
        TinkerFin()
        .with_namespace("projects/project-a")
        .build(
            model=model,
            backend=project,
        )
    )
    result = await runtime.ainvoke(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
    )
```

`workspace(...)` returns a `SandboxWorkspace` without performing I/O. The Runtime
opens isolated access only for an admitted run, then stops its processes and network
activity during cleanup. Reusing the project across conversations preserves files;
finishing a run does not destroy the Sandbox.

## Manage a Sandbox directly

Use the same project for access outside an agent run:

```python
project = sandboxes.workspace("users/7", workspace_key="project-a")
async with project.open() as files:
    await files.aupload_files([("/notes.txt", b"hello")])
    result = await files.aexecute("cat notes.txt")
```

When the project is no longer needed, `await project.delete()` stops all its runs and
deletes its files, HOME, caches, and dependencies. Other projects in the same Sandbox
remain available.

| Task | Method |
| --- | --- |
| Open or reuse a raw command Sandbox | `get(key)` |
| Reconnect a raw command Sandbox | `reconnect(key)` |
| Replace a raw command Sandbox | `recreate(key)` |
| Clear a raw command Sandbox's file root | `reset(key)` |
| Pause or resume the owner's entire Sandbox | `pause(key)`, `resume(key)` |
| Destroy the owner's Sandbox and all its projects | `destroy(key)` |
| Inspect | `get_details(key)` |
| Close local resources | `aclose()` |

Raw command methods reject owners used for isolated workspaces. For the example above,
`await sandboxes.pause("users/7")` pauses every project for that owner. Pause and resume
return `None`; `project.open()` does not resume a paused Sandbox automatically.

## Persistence and ownership

`SQLAlchemyOpenSandboxState` supports SQLite, MySQL, and PostgreSQL through a borrowed
SQLAlchemy `AsyncEngine`. The application creates and disposes the Engine. State stores
physical Sandbox bindings and lifecycle coordination. Project files last as long as
that Sandbox; State does not back them up, create project volumes, or impose project
storage quotas.

The manager owns its OpenSandbox client and State. A caller-supplied HTTP transport
remains caller-owned. Persistent State retains remote Sandboxes when the manager closes;
in-memory State destroys the instances it created.

Each project uses the same isolation for Shell commands, file tools, and transfers.
Runs have private processes, temporary files, and networking; their shared base tools
are read-only. Project files, HOME, caches, and dependencies survive between runs.
Public HTTP on port 80 and HTTPS on port 443 use the managed proxy. A server process
started by a run ends with that run.

## Next steps

- [Lifecycle](lifecycle.md)
- [Files and commands](rooted-filesystem.md)
- [Persistent state and extensions](persistence-and-extensions.md)
- [Sandbox API](api-reference.md)
