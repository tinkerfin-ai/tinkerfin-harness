# Persistent state and extensions

[Rooted files and commands](rooted-filesystem.md) · [中文](../../cn/sandbox/persistence-and-extensions.md)

Use persistent State to share Sandbox bindings between workers and recover them after
restart. It stores bindings, leases, availability, and cleanup work. Container files
need their own volumes and backups. With `OpenSandboxConfig(ttl=None)`, new persistent
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
        backend = await manager.get("projects/project-1")
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
domain is separate from the Runtime namespace and application key used to select each
Sandbox. See [keys and workspace use](index.md).

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
indexes, and database comments. Creating the schema requires DDL permissions; a
precreated complete schema can use a DML account.

## Warm capacity

```python
config = OpenSandboxConfig(warm_pool_size=2)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
    warm_pool_size=2,
)
```

`get()` assigns a warm instance to an application workspace.

## Prepare workspaces

```python
async def prepare_project(backend) -> None:
    result = await backend.aexecute("mkdir -p /workspace/project /workspace/output")
    if result.exit_code != 0:
        raise RuntimeError("Could not prepare workspace directories")


client = OpenSandboxClient(
    connection_config=connection_config,
    config=config,
    initializers=[prepare_project],
)
```

Initializers run after creation and each connection to an existing Sandbox. Make
them idempotent and preserve existing workspace contents. Use asynchronous callbacks
for I/O and propagate cancellation; synchronous callbacks must be non-blocking.
Connection and initialization share the earlier client or recovery deadline.
Initializer failure is reported as `OpenSandboxInitializationError` and does not
authorize retries or recreation. See the [usage reference](api-reference.md) for
callback and timeout constraints.

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
idempotent release. Ready-slot claims must preserve the published ID while it is probed;
discarding a proven unusable ID must clear the slot and enqueue cleanup in one atomic
transition. After an uncertain network result, never destroy a Sandbox that may already
be the authoritative binding.

`InMemoryOpenSandboxState(namespace=...)` demonstrates the behavior but does not share state across processes.

Next: [Sandbox usage reference](api-reference.md).
