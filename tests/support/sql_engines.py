"""Own Engines borrowed by storage implementations in one test."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool


class SqlEngineFactory:
    """Create explicit test Engines and dispose them after storage owners settle."""

    def __init__(self) -> None:
        self._engines: list[AsyncEngine] = []

    def __call__(self, url: str) -> AsyncEngine:
        if url.startswith("sqlite") and (":memory:" in url or url.endswith("///")):
            engine = create_async_engine(
                url, poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0
            )
        else:
            engine = create_async_engine(url)
        self._engines.append(engine)
        return engine

    async def aclose(self) -> None:
        failures: list[BaseException] = []
        for engine in reversed(self._engines):
            try:
                await engine.dispose()
            except BaseException as error:  # noqa: BLE001 - dispose every test-owned Engine
                failures.append(error)
        self._engines.clear()
        if failures:
            raise BaseExceptionGroup("Test Engine cleanup failed", failures)
