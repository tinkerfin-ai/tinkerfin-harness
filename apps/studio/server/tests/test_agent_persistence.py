"""Agent 持久化资源的启动与关闭结算契约"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import RedisTestService

from tinkerfin_studio.agent import persistence as persistence_module
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.config.settings import RedisRuntimeSettings


@dataclass(slots=True)
class _LifecycleTrace:
    events: list[str] = field(default_factory=list)
    store_setup_error: BaseException | None = None
    saver_setup_error: BaseException | None = None
    redis_create_error: BaseException | None = None
    saver_create_error: BaseException | None = None
    redis_close_error: BaseException | None = None
    store_close_error: BaseException | None = None


class _FakeStore:
    def __init__(self, trace: _LifecycleTrace) -> None:
        self._trace = trace

    async def setup(self) -> None:
        self._trace.events.append("store.setup")
        if self._trace.store_setup_error is not None:
            raise self._trace.store_setup_error


class _FakeStoreResource:
    def __init__(self, trace: _LifecycleTrace) -> None:
        self._trace = trace
        self.store = _FakeStore(trace)

    async def __aenter__(self) -> _FakeStore:
        self._trace.events.append("store.enter")
        try:
            await self.store.setup()
        except BaseException:
            self._trace.events.append("store.close")
            raise
        return self.store

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._trace.events.append("store.close")
        if self._trace.store_close_error is not None:
            raise self._trace.store_close_error


class _FakeRedis:
    def __init__(self, trace: _LifecycleTrace) -> None:
        self._trace = trace

    async def aclose(self) -> None:
        self._trace.events.append("redis.close")
        if self._trace.redis_close_error is not None:
            raise self._trace.redis_close_error


class _FakeSaver:
    def __init__(self, trace: _LifecycleTrace) -> None:
        self._trace = trace

    async def asetup(self) -> None:
        self._trace.events.append("saver.setup")
        if self._trace.saver_setup_error is not None:
            raise self._trace.saver_setup_error


@pytest_asyncio.fixture
async def persistence_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        yield engine
    finally:
        await engine.dispose()


def _redis_settings(
    *,
    host: str = "127.0.0.1",
    port: int = 6379,
) -> RedisRuntimeSettings:
    return RedisRuntimeSettings(
        password=None,
        max_connections=80,
        socket_timeout_seconds=10,
        host=host,
        port=port,
        database=15,
        checkpoint_database=0,
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    trace: _LifecycleTrace,
    persistence_engine: AsyncEngine,
) -> tuple[_FakeStore, _FakeRedis, _FakeSaver]:
    resource = _FakeStoreResource(trace)
    redis = _FakeRedis(trace)
    saver = _FakeSaver(trace)

    def create_store(engine: AsyncEngine) -> _FakeStoreResource:
        assert engine is persistence_engine
        return resource

    def create_redis(*_args: object, **_kwargs: object) -> _FakeRedis:
        trace.events.append("redis.create")
        if trace.redis_create_error is not None:
            raise trace.redis_create_error
        return redis

    def create_saver(**_kwargs: object) -> _FakeSaver:
        trace.events.append("saver.create")
        if trace.saver_create_error is not None:
            raise trace.saver_create_error
        return saver

    monkeypatch.setattr(persistence_module, "SqlAlchemyStore", create_store)
    monkeypatch.setattr(persistence_module, "create_redis_client", create_redis)
    monkeypatch.setattr(persistence_module, "AsyncRedisSaver", create_saver)
    return resource.store, redis, saver


def _persistence(engine: AsyncEngine) -> AgentPersistence:
    return AgentPersistence(engine, _redis_settings())


@pytest.mark.asyncio
async def test_successful_lifecycle_publishes_then_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    trace = _LifecycleTrace()
    store, _redis, saver = _install_fakes(monkeypatch, trace, persistence_engine)
    persistence = _persistence(persistence_engine)

    async with persistence:
        assert persistence.store is store
        assert persistence.checkpointer is saver
        assert trace.events == [
            "store.enter",
            "store.setup",
            "redis.create",
            "saver.create",
            "saver.setup",
        ]

    assert trace.events[-2:] == ["redis.close", "store.close"]
    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.store
    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.checkpointer


@pytest.mark.asyncio
async def test_store_setup_failure_closes_before_other_resources_start(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    setup_error = RuntimeError("store setup failed")
    trace = _LifecycleTrace(store_setup_error=setup_error)
    _install_fakes(monkeypatch, trace, persistence_engine)

    with pytest.raises(RuntimeError, match="store setup failed") as raised:
        await _persistence(persistence_engine).__aenter__()

    assert raised.value is setup_error
    assert trace.events == ["store.enter", "store.setup", "store.close"]


@pytest.mark.asyncio
async def test_redis_close_failure_still_closes_store_and_settles_once(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    redis_error = RuntimeError("redis close failed")
    trace = _LifecycleTrace(redis_close_error=redis_error)
    _install_fakes(monkeypatch, trace, persistence_engine)
    persistence = await _persistence(persistence_engine).__aenter__()

    with pytest.raises(RuntimeError, match="redis close failed") as raised:
        await persistence.__aexit__(None, None, None)

    assert raised.value is redis_error
    assert trace.events[-2:] == ["redis.close", "store.close"]
    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.store
    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.checkpointer
    settled_events = list(trace.events)
    await persistence.__aexit__(None, None, None)
    assert trace.events == settled_events


@pytest.mark.asyncio
async def test_multiple_cleanup_failures_are_reported_together(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    redis_error = RuntimeError("redis close failed")
    store_error = OSError("store close failed")
    trace = _LifecycleTrace(
        redis_close_error=redis_error,
        store_close_error=store_error,
    )
    _install_fakes(monkeypatch, trace, persistence_engine)
    persistence = await _persistence(persistence_engine).__aenter__()

    with pytest.raises(ExceptionGroup) as raised:
        await persistence.__aexit__(None, None, None)

    assert raised.value.exceptions == (redis_error, store_error)
    assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.asyncio
async def test_setup_failure_remains_primary_when_both_cleanups_fail(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    setup_error = ValueError("checkpointer setup failed")
    redis_error = RuntimeError("redis close failed")
    store_error = OSError("store close failed")
    trace = _LifecycleTrace(
        saver_setup_error=setup_error,
        redis_close_error=redis_error,
        store_close_error=store_error,
    )
    _install_fakes(monkeypatch, trace, persistence_engine)

    with pytest.raises(ValueError, match="checkpointer setup failed") as raised:
        await _persistence(persistence_engine).__aenter__()

    assert raised.value is setup_error
    assert isinstance(raised.value.__cause__, ExceptionGroup)
    assert raised.value.__cause__.exceptions == (redis_error, store_error)
    assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["redis", "saver"])
async def test_construction_failure_closes_an_already_open_store(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
    failure_stage: str,
) -> None:
    construction_error = ConnectionError(f"{failure_stage} construction failed")
    trace = _LifecycleTrace(
        redis_create_error=(construction_error if failure_stage == "redis" else None),
        saver_create_error=(construction_error if failure_stage == "saver" else None),
    )
    _install_fakes(monkeypatch, trace, persistence_engine)

    with pytest.raises(ConnectionError, match="construction failed") as raised:
        await _persistence(persistence_engine).__aenter__()

    assert raised.value is construction_error
    assert trace.events[-1] == "store.close"
    if failure_stage == "saver":
        assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_after_store_close(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    trace = _LifecycleTrace(redis_close_error=asyncio.CancelledError())
    _install_fakes(monkeypatch, trace, persistence_engine)
    persistence = await _persistence(persistence_engine).__aenter__()

    with pytest.raises(asyncio.CancelledError):
        await persistence.__aexit__(None, None, None)

    assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.asyncio
async def test_body_failure_remains_primary_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    body_error = KeyError("body failed")
    close_error = RuntimeError("redis close failed")
    trace = _LifecycleTrace(redis_close_error=close_error)
    _install_fakes(monkeypatch, trace, persistence_engine)

    with pytest.raises(KeyError, match="body failed") as raised:
        async with _persistence(persistence_engine):
            raise body_error

    assert raised.value is body_error
    assert raised.value.__cause__ is close_error
    assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.asyncio
async def test_cleanup_cancellation_outranks_body_failure_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
    persistence_engine: AsyncEngine,
) -> None:
    body_error = ValueError("body failed")
    cancellation = asyncio.CancelledError()
    trace = _LifecycleTrace(redis_close_error=cancellation)
    _install_fakes(monkeypatch, trace, persistence_engine)

    with pytest.raises(asyncio.CancelledError) as raised:
        async with _persistence(persistence_engine):
            raise body_error

    assert raised.value is cancellation
    assert raised.value.__cause__ is body_error
    assert trace.events[-2:] == ["redis.close", "store.close"]


@pytest.mark.docker_integration
async def test_real_store_and_checkpointer_complete_one_owned_lifecycle(
    mysql_sandbox_url: str,
    redis_test_service: RedisTestService,
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    persistence = AgentPersistence(
        engine,
        _redis_settings(
            host=redis_test_service.host,
            port=redis_test_service.port,
        ),
    )

    try:
        async with persistence:
            assert persistence.store is not None
            assert persistence.checkpointer is not None
    finally:
        await engine.dispose()

    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.store
    with pytest.raises(RuntimeError, match="尚未启动"):
        _ = persistence.checkpointer
