"""全局 LangGraph Store 与 Redis checkpointer 生命周期"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from types import TracebackType
from typing import NoReturn, Self

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.store.base import BaseStore
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_langgraph_store import SqlAlchemyStore
from tinkerfin_studio.config.settings import RedisRuntimeSettings
from tinkerfin_studio.infrastructure.redis_client import create_redis_client
from tinkerfin_studio.infrastructure.redis_keys import (
    CHECKPOINT_KEY_PREFIX,
    CHECKPOINT_WRITE_KEY_PREFIX,
)


def _cleanup_failure(failures: tuple[BaseException, ...]) -> BaseException:
    """把一次或多次资源关闭失败保留为一个异常对象"""

    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup("Agent 持久化资源关闭失败", list(failures))


def _raise_settlement_failure(
    *,
    primary: BaseException | None,
    primary_traceback: TracebackType | None,
    failures: tuple[BaseException, ...],
) -> NoReturn:
    """保留主因、取消语义和全部资源关闭失败

    Args:
        primary: 触发关闭的业务、启动或取消异常
        primary_traceback: 主异常原始 traceback
        failures: 按资源实际关闭顺序记录的失败

    Raises:
        BaseException: 主因、进程控制异常或资源关闭异常组
    """

    assert failures
    cleanup = _cleanup_failure(failures)
    if primary is not None and not isinstance(primary, Exception):
        raise primary.with_traceback(primary_traceback) from cleanup
    control = next(
        (failure for failure in failures if not isinstance(failure, Exception)),
        None,
    )
    if control is not None:
        remaining = tuple(failure for failure in failures if failure is not control)
        context = ((primary,) if primary is not None else ()) + remaining
        if context:
            raise control.with_traceback(control.__traceback__) from _cleanup_failure(
                context
            )
        raise control.with_traceback(control.__traceback__)
    if primary is not None:
        raise primary.with_traceback(primary_traceback) from cleanup
    raise cleanup


class _PersistenceResources:
    """注册并一次性结算 AgentPersistence 创建的全部资源"""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._failures: list[BaseException] = []

    async def enter_store(
        self,
        resource: AbstractAsyncContextManager[SqlAlchemyStore],
    ) -> SqlAlchemyStore:
        """进入长期记忆 Store 并登记关闭回调"""

        store = await resource.__aenter__()

        async def close_store(
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            try:
                return bool(await resource.__aexit__(exc_type, exc_value, traceback))
            except BaseException as error:  # noqa: BLE001 - 继续关闭后续资源
                self._failures.append(error)
                return False

        self._stack.push_async_exit(close_store)
        return store

    def own_redis(self, client: Redis) -> None:
        """登记 Agent checkpoint 独占 Redis client 的关闭回调"""

        async def close_redis() -> None:
            try:
                await client.aclose()
            except BaseException as error:  # noqa: BLE001 - 继续关闭 Store
                self._failures.append(error)

        self._stack.push_async_callback(close_redis)

    async def settle(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> tuple[BaseException, ...]:
        """消费全部退出回调并返回完整关闭失败"""

        self._failures.clear()
        try:
            await self._stack.__aexit__(exc_type, exc_value, traceback)
        except BaseException as error:  # noqa: BLE001 - 保留退出栈自身失败
            self._failures.append(error)
        return tuple(self._failures)


class AgentPersistence:
    """拥有 Deep Agents 共用 Store 与 checkpointer 的进程级资源"""

    def __init__(
        self,
        engine: AsyncEngine,
        redis: RedisRuntimeSettings,
    ) -> None:
        self._engine = engine
        self._redis_settings = redis
        self._resources: _PersistenceResources | None = None
        self._store: SqlAlchemyStore | None = None
        self._checkpointer: AsyncRedisSaver | None = None

    @property
    def store(self) -> BaseStore:
        """返回生命周期内可用的长期记忆 Store"""

        if self._store is None:
            raise RuntimeError("Agent Store 尚未启动")
        return self._store

    @property
    def checkpointer(self) -> AsyncRedisSaver:
        """返回生命周期内可用的 Redis checkpointer"""

        if self._checkpointer is None:
            raise RuntimeError("Agent checkpointer 尚未启动")
        return self._checkpointer

    async def __aenter__(self) -> Self:
        """初始化长期记忆 Store 与会话恢复索引"""

        if self._resources is not None:
            raise RuntimeError("Agent persistence 已经启动")
        resources = _PersistenceResources()
        try:
            # 长期记忆借用组件库连接池，Store 负责建表校验
            store_resource = SqlAlchemyStore(self._engine)
            store = await resources.enter_store(store_resource)
            checkpoint_redis = create_redis_client(
                self._redis_settings,
                database=self._redis_settings.checkpoint_database,
            )

            resources.own_redis(checkpoint_redis)
            checkpointer = AsyncRedisSaver(
                redis_client=checkpoint_redis,
                checkpoint_prefix=CHECKPOINT_KEY_PREFIX,
                checkpoint_write_prefix=CHECKPOINT_WRITE_KEY_PREFIX,
            )
            await checkpointer.asetup()
        except BaseException as error:
            failures = await resources.settle(
                type(error),
                error,
                error.__traceback__,
            )
            if failures:
                _raise_settlement_failure(
                    primary=error,
                    primary_traceback=error.__traceback__,
                    failures=failures,
                )
            raise
        self._resources = resources
        self._store = store
        self._checkpointer = checkpointer
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭会话恢复连接，并等待长期记忆操作结束"""

        resources = self._resources
        if resources is None:
            return
        try:
            failures = await resources.settle(
                exc_type,
                exc_value,
                traceback,
            )
            if failures:
                _raise_settlement_failure(
                    primary=exc_value,
                    primary_traceback=traceback,
                    failures=failures,
                )
        finally:
            self._checkpointer = None
            self._store = None
            self._resources = None
