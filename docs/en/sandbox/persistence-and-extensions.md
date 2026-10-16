# Persistent state and extensions

[Rooted files and commands](rooted-filesystem.md) · [中文](../../cn/sandbox/persistence-and-extensions.md)

Use persistent State to share Sandbox bindings between workers and recover them after
restart. It stores physical Sandbox bindings and their purpose, leases, availability,
and cleanup work. Project files, HOME, caches, and dependencies remain inside the
Sandbox and need separate backups. With `OpenSandboxConfig(ttl=None)`, new persistent
Sandboxes remain until explicitly destroyed; closing a manager retains their bindings.

## SQL databases

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

Use `asyncpg` for PostgreSQL or `asyncmy` for MySQL. Supply the Engine you already manage:

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_sandbox import OpenSandboxManager, SQLAlchemyOpenSandboxState

engine = create_async_engine("sqlite+aiosqlite:////var/lib/app/sandboxes.db")
state = SQLAlchemyOpenSandboxState(engine=engine, namespace="production")
manager = OpenSandboxManager(client=client, state=state)

try:
    async with manager:
        project = manager.workspace("users/7", workspace_key="project-a")
        async with project.open() as files:
            await files.awrite("/notes.txt", "hello")
finally:
    await engine.dispose()
```

PostgreSQL URLs use `postgresql+asyncpg://...`; MySQL URLs use `mysql+asyncmy://...`.
The same State API covers all three databases. MariaDB is not supported.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `engine` | required | Borrowed asynchronous SQLAlchemy Engine |
| `namespace` | `""` | Deployment domain shared by cooperating managers |
| `lease_ttl` | `15.0` | Worker and claim lease duration, in seconds |
| `poll_interval` | `0.05` | Claim polling and initial lock retry delay, in seconds |
| `sqlite_retry_timeout` | `5.0` | SQLite lock retry budget, in seconds |

All workers in one deployment namespace must agree on warm capacity. This deployment
domain is separate from the owner key that selects a physical Sandbox and the
`workspace_key` that selects a project. Runtime namespaces scope logical persistence
without changing either selection. See [keys and workspace use](index.md).

Close State before disposing the borrowed Engine. Configure connection and statement
timeouts on the Engine; accepted work and cleanup may extend the caller's wait.

For in-memory SQLite, use `AsyncAdaptedQueuePool` with `pool_size=1, max_overflow=0`;
`StaticPool` is rejected. `sqlite_retry_timeout` bounds SQLite lock retries.

## Generate the database schema

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema

schema = get_sqlalchemy_opensandbox_state_schema(dialect="postgresql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` accepts `postgresql`, `mysql`, or `sqlite`. The descriptor also exposes
`table_names`. Startup creates an empty schema or validates its tables, columns, keys,
and indexes. MySQL and PostgreSQL also validate database comments. Creating the schema
requires DDL permissions; a precreated complete schema can use a DML account.

## Warm capacity

```python
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
    warm_pool_size=2,
)
```

`get()` assigns warm instances to raw command Sandboxes. Isolated workspaces bypass
the warm pool and create instances with the required isolation capability. An
application using only isolated workspaces can set `warm_pool_size=0`.

## Prepare workspaces

```python
project = manager.workspace("users/7", workspace_key="project-a")
async with project.open() as files:
    await files.aupload_files([("/input/data.csv", csv_bytes)])
    result = await files.aexecute("mkdir -p output")
```

Use `project.open()` to seed files or install project dependencies before an agent run.
The context closes its processes and networking while preserving project data.

Client `initializers` are for trusted setup of the entire physical Sandbox. They run
after creation and each connection, must be idempotent, and must preserve existing
projects. Use asynchronous callbacks for I/O and propagate cancellation; synchronous
callbacks must be non-blocking. Connection and initialization share the earlier client
or recovery deadline. Initializer failure raises `OpenSandboxInitializationError`
without authorizing retries or recreation. See the [usage reference](api-reference.md)
for callback and timeout constraints.

## Bring your own state store

Implement `OpenSandboxState` to store bindings and leases in an existing database.

| Capability | Methods |
| --- | --- |
| Lifetime | `start()`, `aclose()` |
| Owner | `acquire_owner()`, `renew_owner()`, `bind_owner()`, `unbind_owner()`, `release_owner()`, `read_binding()` |
| Warm pool | `claim_warm_slot()`, `claim_ready_warm_slot()`, `renew_warm()`, `publish_warm()`, `discard_ready_warm_slot()`, `release_warm()`, `warm_pool_ready()`, `consume_warm()` |
| Cleanup | `enqueue_cleanup()`, `claim_cleanup()`, `renew_cleanup()`, `complete_cleanup()`, `release_cleanup()` |
| Shutdown recovery | `shutdown_sandbox_ids()` |

A custom implementation needs atomic claims, generation fencing, lease renewal, and
idempotent release. `bind_owner(claim, sandbox_id, purpose=...)` atomically commits the
ID, generation, and required `commands` or `workspaces` purpose. Existing bindings keep
their purpose through replacement and remote unavailability; changing it requires
explicit unbinding. Reads and acquired claims return the complete binding. An unbound
owner has no purpose. Warm slots contain only `commands` instances, and `consume_warm()`
must reject a `workspaces` owner without consuming a slot or changing its binding.

Ready-slot claims must preserve the published ID while it is probed;
discarding a proven unusable ID must clear the slot and enqueue cleanup in one atomic
transition. After an uncertain network result, never destroy a Sandbox that may already
be the authoritative binding.

`InMemoryOpenSandboxState(namespace=...)` demonstrates the behavior but does not share state across processes.

Next: [Sandbox usage reference](api-reference.md).
