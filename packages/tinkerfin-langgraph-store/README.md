# TinkerFin LangGraph Store

Persistent LangGraph memory with asynchronous reads, atomic batches, structured
search, and namespace listing. SQLite, PostgreSQL, and MySQL use the same
query and batch API.

## Installation

```bash
pip install "tinkerfin-langgraph-store[sqlalchemy]" aiosqlite
```

Choose `asyncpg` for PostgreSQL or `asyncmy` for MySQL. Supply a SQLAlchemy
`AsyncEngine` configured with that driver.

## Quick start

```python
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_langgraph_store import SqlAlchemyStore


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///memory.db")
    try:
        async with SqlAlchemyStore(engine) as store:
            await store.aput(("users", "alice"), "preferences", {"language": "en"})
            item = await store.aget(("users", "alice"), "preferences")
            print(item.value if item else None)
            matches = await store.asearch(("users",), filter={"language": "en"})
            print([item.namespace for item in matches])
    finally:
        await engine.dispose()


asyncio.run(main())
```

Pass the Store to `TinkerFin(store=store).with_namespace(...).build(model=...)` to
isolate memory by Runtime namespace. The caller owns the Engine. Closing the Store
waits for accepted operations and does not close the Engine. Setup is automatic;
`await store.setup()` can validate storage at application startup.

## Stored data and queries

- `aget`, `aput`, `adelete`, and `abatch` use complete namespace/key identities.
  A batch reads one snapshot before writing; the final write to each key wins.
  Updates preserve `created_at`.
- `asearch` matches a literal namespace prefix. Results sort by `updated_at`
  descending, then namespace and key ascending. `limit` and `offset` apply after
  filtering.
- Filters use literal top-level field names. `$eq` and `$ne` compare typed JSON
  values; use `$eq` for an object value. Missing fields match neither operator.
  Null differs from missing, booleans differ from numbers, and object key order
  does not affect equality. Integral floats equal their corresponding integers.
- `$gt`, `$gte`, `$lt`, and `$lte` compare numbers at binary64 precision. Operands
  must be finite numbers within that range. Multiple fields and operators combine
  with AND.
- `alist_namespaces` matches complete paths before `max_depth`, deduplication,
  sorting, and pagination. Its `*` matches one label; search treats `*` literally.
  Only namespaces containing documents appear.

Namespaces follow LangGraph's label rules. Namespace, key, and document sizes are
subject to the database's payload and resource limits. Document values must be
finite JSON objects. TTL, semantic search, vector indexing, and synchronous
database calls are unsupported.

Use a pool with exclusive checkouts. File SQLite Engines use one by default;
in-memory SQLite needs `AsyncAdaptedQueuePool(pool_size=1, max_overflow=0)`.
Existing tables and indexes must match the complete current structure.

## Documentation and license

[TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/tree/main/docs/en)
· [MIT License](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-langgraph-store/LICENSE)
· [NOTICE](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/packages/tinkerfin-langgraph-store/NOTICE)
