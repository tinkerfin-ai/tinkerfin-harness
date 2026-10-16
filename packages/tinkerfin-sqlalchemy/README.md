# tinkerfin-sqlalchemy

Async transaction support for storage integrations using SQLite, MySQL, or PostgreSQL.
Application code normally uses a TinkerFin Store and supplies its own SQLAlchemy
`AsyncEngine`; storage integrations use `SqlTransaction` directly.

## Installation

```bash
pip install tinkerfin-sqlalchemy
```

Python 3.11 or newer is required. Install an asynchronous database driver such as
`aiosqlite`, `asyncmy`, or `asyncpg`.

## Quick Start

Install the driver for this in-memory example:

```bash
pip install aiosqlite
```

```python
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_sqlalchemy import SqlTransaction


async def main() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with SqlTransaction(engine, schema_lock="example-balances") as connection:
            await connection.execute(
                text("CREATE TABLE balances (amount INTEGER NOT NULL)")
            )
            await connection.execute(text("INSERT INTO balances (amount) VALUES (0)"))

        async with SqlTransaction(engine) as connection:
            await connection.execute(text("UPDATE balances SET amount = amount + 1"))

        async with SqlTransaction(engine, read_only=True) as connection:
            result = await connection.execute(text("SELECT amount FROM balances"))
            print(result.scalar_one())
    finally:
        await engine.dispose()


asyncio.run(main())
```

Successful writes commit; failed writes roll back. Reads share one database snapshot.
The Engine remains owned by the caller. Temporary lock settings are restored before
connections return to the pool; connections that cannot be restored are discarded.
Cancellation waits for connection cleanup. Driver timeouts remain host configuration.

Connections must have exclusive pool checkouts. File-based SQLite engines use this
by default. For an in-memory SQLite database, configure `AsyncAdaptedQueuePool` with
`pool_size=1` and `max_overflow=0`; `StaticPool` shares one live connection across
transactions and is rejected. Discarding an in-memory connection also discards its data.

For schema setup, supply a stable `schema_lock` name and run DDL on the yielded
connection. Integrations keep their own schema, retry policy, and commit confirmation.
`SqlTransaction` performs one attempt and never replays application work.

## Documentation

[TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/docs/en/index.md)

## License

[Apache-2.0](https://github.com/tinkerfin-ai/tinkerfin-harness/blob/main/LICENSE)
