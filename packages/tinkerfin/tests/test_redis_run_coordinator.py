from __future__ import annotations

import asyncio
from collections import deque
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import ConnectionError as RedisConnectionError

from tinkerfin import RunIdentity
from tinkerfin.coordination import (
    RunCoordinationError,
    RunCoordinationUnavailableError,
)
from tinkerfin.redis import (
    RedisLeaseError,
    RedisLeaseLifecycleError,
    RedisLeaseUnavailableError,
    RedisRunCoordinator,
)


def _identity(*, thread_id: str = "user-1", run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def test_from_client_rejects_redis_cluster_outside_the_client_boundary() -> None:
    cluster = object.__new__(RedisCluster)

    with pytest.raises(TypeError, match="client boundary"):
        RedisRunCoordinator.from_client(
            cast(Redis, cluster),
            key_resolver=lambda identity: identity.thread_id,
        )


class _ScriptedRedis:
    def __init__(
        self,
        replies: list[int | BaseException],
        *,
        ping_error: BaseException | None = None,
    ) -> None:
        self.replies = deque(replies)
        self.ping_error = ping_error
        self.eval_calls: list[tuple[int, tuple[object, ...]]] = []
        self.close_calls = 0

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def eval(
        self,
        _script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> int:
        self.eval_calls.append((numkeys, keys_and_args))
        if not self.replies:
            raise AssertionError("unexpected Redis script call")
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def aclose(self) -> None:
        self.close_calls += 1


class _BlockingAcquireRedis(_ScriptedRedis):
    def __init__(self) -> None:
        super().__init__([1, 1])
        self.acquire_started = asyncio.Event()
        self.release_acquire = asyncio.Event()

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> int:
        if not self.eval_calls:
            self.acquire_started.set()
            await self.release_acquire.wait()
        return await super().eval(script, numkeys, *keys_and_args)


@pytest.mark.parametrize("custom_key", [False, True])
async def test_redis_coordination_defaults_to_namespaced_threads(
    custom_key: bool,
) -> None:
    client = _ScriptedRedis([1, 1, 1, 1, 1, 1])
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=(lambda _: "shared-key") if custom_key else None,
    )
    identities = (
        RunIdentity(namespace="test", thread_id="thread", run_id="one"),
        RunIdentity(namespace="test", thread_id="thread", run_id="two"),
        RunIdentity(namespace="other", thread_id="thread", run_id="one"),
    )
    async with coordinator:
        for identity in identities:
            async with coordinator(identity):
                pass
    keys = [client.eval_calls[index][1][:2] for index in (0, 2, 4)]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert client.close_calls == 0


class _CommittedBlockingAcquireRedis(_ScriptedRedis):
    """Model a lease committed before Redis delivers the script reply."""

    def __init__(self) -> None:
        super().__init__([])
        self.acquire_committed = asyncio.Event()
        self.release_acquire_reply = asyncio.Event()
        self.lock_owner: object | None = None

    async def eval(
        self,
        _script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> int:
        self.eval_calls.append((numkeys, keys_and_args))
        if len(self.eval_calls) == 1:
            self.lock_owner = keys_and_args[-2]
            self.acquire_committed.set()
            await self.release_acquire_reply.wait()
            return 1
        if self.lock_owner == keys_and_args[-1]:
            self.lock_owner = None
            return 1
        return 0


class _BlockingPingRedis(_ScriptedRedis):
    def __init__(self) -> None:
        super().__init__([])
        self.ping_started = asyncio.Event()
        self.release_ping = asyncio.Event()
        self.ping_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        self.ping_started.set()
        await self.release_ping.wait()
        return True


class _BlockingPingAndCloseRedis(_BlockingPingRedis):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_cancelled = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.close_completed.set()


@pytest.mark.asyncio
async def test_from_client_borrows_redis_and_hashes_the_resolved_identity() -> None:
    client = _ScriptedRedis([1, 1])
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: f"tenant/{identity.thread_id}",
    )

    async with coordinator:
        async with coordinator(_identity(thread_id="private-user")):
            pass

    assert client.close_calls == 0
    assert len(client.eval_calls) == 2
    acquire_numkeys, acquire_arguments = client.eval_calls[0]
    assert acquire_numkeys == 2
    generated_keys = tuple(str(value) for value in acquire_arguments[:2])
    assert all(key.startswith("tinkerfin:run:") for key in generated_keys)
    assert all("private-user" not in key for key in generated_keys)


@pytest.mark.asyncio
async def test_identity_key_is_resolved_once_per_coordinated_run() -> None:
    client = _ScriptedRedis([1, 1])
    resolver_calls = 0

    def resolve(identity: RunIdentity) -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return identity.thread_id

    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=resolve,
    )

    async with coordinator:
        async with coordinator(_identity()):
            pass

    assert resolver_calls == 1


@pytest.mark.asyncio
async def test_coordinator_close_waits_for_active_run_release() -> None:
    client = _ScriptedRedis([1, 1])
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: identity.thread_id,
    )
    await coordinator.__aenter__()
    entered = asyncio.Event()
    release_run = asyncio.Event()

    async def run() -> None:
        async with coordinator(_identity()):
            entered.set()
            await release_run.wait()

    running = asyncio.create_task(run())
    await entered.wait()
    closing = asyncio.create_task(coordinator.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0)
        assert not closing.done()
        release_run.set()
        await running
        await closing
    finally:
        release_run.set()
        await asyncio.gather(running, closing, return_exceptions=True)

    assert len(client.eval_calls) == 2


@pytest.mark.asyncio
async def test_from_url_closes_its_client_when_health_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ScriptedRedis(
        [],
        ping_error=RedisConnectionError("Redis unavailable"),
    )

    def from_url(url: str, *, decode_responses: bool) -> Redis:
        assert url == "redis://coordination.example/0"
        assert decode_responses is False
        return cast(Redis, client)

    monkeypatch.setattr(Redis, "from_url", staticmethod(from_url))
    coordinator = RedisRunCoordinator.from_url(
        "redis://coordination.example/0",
        key_resolver=lambda identity: identity.thread_id,
    )

    with pytest.raises(RunCoordinationUnavailableError) as raised:
        await coordinator.__aenter__()

    assert isinstance(raised.value.cause, RedisLeaseUnavailableError)
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_from_url_closes_owned_client_after_non_redis_health_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ValueError("invalid Redis health response")
    client = _ScriptedRedis([], ping_error=failure)

    monkeypatch.setattr(
        Redis,
        "from_url",
        staticmethod(lambda _url, **_options: cast(Redis, client)),
    )
    coordinator = RedisRunCoordinator.from_url(
        "redis://coordination.example/0",
        key_resolver=lambda identity: identity.thread_id,
    )

    with pytest.raises(RunCoordinationError) as raised:
        await coordinator.__aenter__()

    assert isinstance(raised.value.cause, RedisLeaseError)
    assert raised.value.cause.cause is failure
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_from_url_cancellation_closes_owned_client_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingPingAndCloseRedis()

    monkeypatch.setattr(
        Redis,
        "from_url",
        staticmethod(lambda _url, **_options: cast(Redis, client)),
    )
    coordinator = RedisRunCoordinator.from_url(
        "redis://coordination.example/0",
        key_resolver=lambda identity: identity.thread_id,
    )
    entering = asyncio.create_task(coordinator.__aenter__())
    await client.ping_started.wait()
    entering.cancel("Redis startup cancelled")

    try:
        await asyncio.wait_for(client.close_started.wait(), timeout=0.2)
        entering.cancel("Redis startup cancelled again")
        client.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await entering
    finally:
        client.release_close.set()
        await asyncio.gather(entering, return_exceptions=True)

    assert client.close_calls == 1
    assert client.close_completed.is_set()
    assert not client.close_cancelled.is_set()


@pytest.mark.asyncio
async def test_concurrent_enter_allows_exactly_one_caller() -> None:
    client = _BlockingPingRedis()
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: identity.thread_id,
    )
    first = asyncio.create_task(coordinator.__aenter__())
    await client.ping_started.wait()
    second = asyncio.create_task(coordinator.__aenter__())
    await asyncio.sleep(0)
    client.release_ping.set()

    try:
        results = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        client.release_ping.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await coordinator.__aexit__(None, None, None)

    assert sum(result is coordinator for result in results) == 1
    failures = [
        result for result in results if isinstance(result, RunCoordinationError)
    ]
    assert len(failures) == 1
    assert isinstance(failures[0].cause, RedisLeaseLifecycleError)
    assert "single-use" in str(failures[0].cause)
    assert client.ping_calls == 1


@pytest.mark.asyncio
async def test_close_cancellation_waits_for_active_run_cleanup() -> None:
    client = _ScriptedRedis([1, 1])
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: identity.thread_id,
    )
    await coordinator.__aenter__()
    entered = asyncio.Event()
    release_run = asyncio.Event()

    async def run() -> None:
        async with coordinator(_identity()):
            entered.set()
            await release_run.wait()

    running = asyncio.create_task(run())
    await entered.wait()
    closing = asyncio.create_task(coordinator.__aexit__(None, None, None))
    await asyncio.sleep(0)
    closing.cancel("host shutdown was cancelled")

    try:
        await asyncio.sleep(0)
        assert not closing.done()
        release_run.set()
        await running
        with pytest.raises(
            asyncio.CancelledError,
            match="host shutdown was cancelled",
        ):
            await closing
    finally:
        release_run.set()
        await asyncio.gather(running, closing, return_exceptions=True)
        await coordinator.__aexit__(None, None, None)

    with pytest.raises(RunCoordinationError) as raised:
        async with coordinator(_identity(thread_id="user-2")):
            pass
    assert isinstance(raised.value.cause, RedisLeaseLifecycleError)
    assert "entered before use" in str(raised.value.cause)


@pytest.mark.asyncio
async def test_acquisition_that_finishes_after_close_does_not_enter_run() -> None:
    client = _BlockingAcquireRedis()
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: identity.thread_id,
    )
    await coordinator.__aenter__()
    entered = False

    async def run() -> None:
        nonlocal entered
        async with coordinator(_identity()):
            entered = True

    running = asyncio.create_task(run())
    await client.acquire_started.wait()
    closing = asyncio.create_task(coordinator.__aexit__(None, None, None))
    await asyncio.sleep(0)
    client.release_acquire.set()

    with pytest.raises(RunCoordinationError) as raised:
        await running
    assert isinstance(raised.value.cause, RedisLeaseLifecycleError)
    assert "closing" in str(raised.value.cause)
    await closing

    assert not entered
    assert len(client.eval_calls) == 2


@pytest.mark.asyncio
async def test_cancellation_after_acquire_commit_waits_for_reply_and_releases() -> None:
    client = _CommittedBlockingAcquireRedis()
    coordinator = RedisRunCoordinator.from_client(
        cast(Redis, client),
        key_resolver=lambda identity: identity.thread_id,
    )
    await coordinator.__aenter__()
    entered = False

    async def run() -> None:
        nonlocal entered
        async with coordinator(_identity()):
            entered = True

    running = asyncio.create_task(run())
    await client.acquire_committed.wait()
    running.cancel("cancelled after Redis committed the lease")
    await asyncio.sleep(0)
    waited_for_reply = not running.done()
    client.release_acquire_reply.set()

    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="cancelled after Redis committed the lease",
        ):
            await running
    finally:
        client.release_acquire_reply.set()
        await asyncio.gather(running, return_exceptions=True)
        await coordinator.__aexit__(None, None, None)

    assert waited_for_reply
    assert not entered
    assert client.lock_owner is None
