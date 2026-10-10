"""Own disposable Engines borrowed by Messaging contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@contextmanager
def database_time(engine: AsyncEngine, now: Callable[[], datetime]) -> Iterator[None]:
    """Control storage time on an owned Engine while retaining real SQL and locks."""
    expression = {
        "sqlite": "strftime('%Y-%m-%d %H:%M:%f', 'now')",
        "mysql": "UTC_TIMESTAMP(6)",
        "postgresql": "timezone('UTC', clock_timestamp())",
    }[engine.dialect.name]

    def replace_clock(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        # Execution events cover already pooled connections too. No application
        # values are interpolated; only a test-owned datetime replaces the clock.
        fixed = now().strftime("%Y-%m-%d %H:%M:%S.%f")
        return statement.replace(expression, f"'{fixed}'"), parameters

    event.listen(
        engine.sync_engine, "before_cursor_execute", replace_clock, retval=True
    )
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", replace_clock)


@asynccontextmanager
async def messaging_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'messaging.db'}", pool_size=4, max_overflow=0
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        yield instance
    finally:
        await instance.dispose()
