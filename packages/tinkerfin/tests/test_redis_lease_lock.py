"""Redis lease fencing, renewal, cancellation, and resource lifecycle contracts."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from tinkerfin.redis import (
    RedisLease,
    RedisLeaseLock,
    RedisLeaseLost,
    RedisLeaseUnavailableError,
)


class _LeaseRedis:
    """Execute the three lease scripts atomically without emulating Redis timing."""

    def __init__(self) -> None:
        self.owners: dict[str, object] = {}
        self.fences: Counter[str] = Counter()
        self.acquired_keys: list[str] = []
        self.renew_calls: Counter[str] = Counter()
        self.active_renewals: Counter[str] = Counter()
        self.peak_renewals: Counter[str] = Counter()
        self.fail_renew_keys: set[str] = set()
        self.block_renew_keys: dict[str, asyncio.Event] = {}
        self.release_error: BaseException | None = None
        self.block_next_acquire_reply = False
        self.acquire_committed = asyncio.Event()
        self.release_acquire_reply = asyncio.Event()
        self.close_calls = 0
        self._atomic = asyncio.Lock()

    async def ping(self) -> bool:
        return True

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> int:
        if "fencing_token" in script:
            assert numkeys == 2
            lock_key = str(keys_and_args[0])
            fencing_key = str(keys_and_args[1])
            owner_token = keys_and_args[2]
            async with self._atomic:
                if lock_key in self.owners:
                    return 0
                self.fences[fencing_key] += 1
                token = self.fences[fencing_key]
                self.owners[lock_key] = owner_token
                self.acquired_keys.append(lock_key)
                block_reply = self.block_next_acquire_reply
                self.block_next_acquire_reply = False
            if block_reply:
                self.acquire_committed.set()
                await self.release_acquire_reply.wait()
            return token

        if "PEXPIRE" in script:
            assert numkeys == 1
            lock_key = str(keys_and_args[0])
            owner_token = keys_and_args[1]
            self.renew_calls[lock_key] += 1
            self.active_renewals[lock_key] += 1
            self.peak_renewals[lock_key] = max(
                self.peak_renewals[lock_key],
                self.active_renewals[lock_key],
            )
            try:
                blocker = self.block_renew_keys.get(lock_key)
                if blocker is not None:
                    await blocker.wait()
                if lock_key in self.fail_renew_keys:
                    raise RedisConnectionError("renewal unavailable")
                return int(self.owners.get(lock_key) == owner_token)
            finally:
                self.active_renewals[lock_key] -= 1

        if "DEL" in script:
            assert numkeys == 1
            if self.release_error is not None:
                error = self.release_error
                self.release_error = None
                raise error
            lock_key = str(keys_and_args[0])
            owner_token = keys_and_args[1]
            async with self._atomic:
                if self.owners.get(lock_key) != owner_token:
                    return 0
                del self.owners[lock_key]
                return 1

        raise AssertionError("unexpected lease script")

    async def aclose(self) -> None:
        self.close_calls += 1

    def force_expire(self, lock_key: str) -> None:
        self.owners.pop(lock_key, None)


def _lock_key(prefix: str, resource_key: str) -> str:
    digest = hashlib.sha256(resource_key.encode()).hexdigest()
    return f"{prefix}:{digest}:lease"


def test_base_import_survives_without_redis_and_extra_error_is_explicit() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = """
import importlib.abc
import tinkerfin
import sys

assert tinkerfin.TinkerFin

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "redis" or fullname.startswith("redis."):
            error = ModuleNotFoundError("blocked Redis dependency")
            error.name = "redis"
            raise error
        return None

sys.meta_path.insert(0, Blocker())
try:
    from tinkerfin.redis import RedisLeaseLock
except ModuleNotFoundError as error:
    print(str(error))
else:
    raise SystemExit("Redis integration unexpectedly imported")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert 'pip install "tinkerfin[redis]"' in completed.stdout


def test_public_lease_is_immutable() -> None:
    lease = RedisLease(resource_key="resource", fencing_token=1)

    with pytest.raises(FrozenInstanceError):
        setattr(lease, "fencing_token", 2)


@pytest.mark.asyncio
async def test_old_owner_release_cannot_delete_a_new_owner() -> None:
    client = _LeaseRedis()
    old = RedisLeaseLock.from_client(
        cast(Redis, client),
        key_prefix="takeover-locks",
        lease_ttl_seconds=10,
        renew_interval_seconds=4,
        wait_poll_seconds=0.001,
    )
    new = RedisLeaseLock.from_client(
        cast(Redis, client),
        key_prefix="takeover-locks",
        lease_ttl_seconds=10,
        renew_interval_seconds=4,
        wait_poll_seconds=0.001,
    )
    old_entered = asyncio.Event()
    release_old = asyncio.Event()
    new_entered = asyncio.Event()
    release_new = asyncio.Event()
    tokens: list[int] = []

    async def old_owner() -> None:
        async with old.hold("resource") as lease:
            tokens.append(lease.fencing_token)
            old_entered.set()
            await release_old.wait()

    async def new_owner() -> None:
        async with new.hold("resource") as lease:
            tokens.append(lease.fencing_token)
            new_entered.set()
            await release_new.wait()

    async with old, new:
        old_task = asyncio.create_task(old_owner())
        await old_entered.wait()
        key = _lock_key("takeover-locks", "resource")
        client.force_expire(key)
        new_task = asyncio.create_task(new_owner())
        await new_entered.wait()
        release_old.set()
        with pytest.raises(RedisLeaseLost):
            await old_task
        assert key in client.owners
        release_new.set()
        await new_task

    assert tokens == [1, 2]
    assert client.owners == {}


@pytest.mark.asyncio
async def test_business_error_remains_primary_when_release_fails() -> None:
    client = _LeaseRedis()
    client.release_error = RedisConnectionError("release unavailable")
    lock = RedisLeaseLock.from_client(cast(Redis, client))
    business_error = ValueError("business failed")

    with pytest.raises(ValueError) as captured:
        async with lock:
            async with lock.hold("resource"):
                raise business_error

    assert captured.value is business_error
    assert any(
        RedisLeaseUnavailableError.__name__ in note for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_aclose_waits_for_active_scope_and_rejects_new_scopes() -> None:
    client = _LeaseRedis()
    lock = RedisLeaseLock.from_client(cast(Redis, client))
    await lock.__aenter__()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def active() -> None:
        async with lock.hold("active"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(active())
    await entered.wait()
    closing = asyncio.create_task(lock.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(RuntimeError, match="closing"):
        async with lock.hold("late"):
            pass
    release.set()
    await task
    await closing

    assert client.owners == {}
    assert not any(
        task.get_name().startswith("tinkerfin-redis-lease-renew:")
        for task in asyncio.all_tasks()
        if not task.done()
    )


@pytest.mark.asyncio
async def test_uncertain_acquire_releases_token_and_leaves_a_fencing_gap() -> None:
    client = _LeaseRedis()
    client.block_next_acquire_reply = True
    lock = RedisLeaseLock.from_client(cast(Redis, client), key_prefix="uncertain")
    await lock.__aenter__()
    entered = False

    async def acquire() -> None:
        nonlocal entered
        async with lock.hold("resource"):
            entered = True

    task = asyncio.create_task(acquire())
    await client.acquire_committed.wait()
    task.cancel("caller cancelled uncertain acquisition")
    await asyncio.sleep(0)
    client.release_acquire_reply.set()
    with pytest.raises(
        asyncio.CancelledError,
        match="caller cancelled uncertain acquisition",
    ):
        await task

    assert entered is False
    assert client.owners == {}
    async with lock.hold("resource") as lease:
        assert lease.fencing_token == 2
    await lock.aclose()


@pytest.mark.asyncio
async def test_from_url_owns_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LeaseRedis()

    monkeypatch.setattr(
        Redis,
        "from_url",
        staticmethod(lambda _url, **_options: cast(Redis, client)),
    )
    async with RedisLeaseLock.from_url("redis://lease.example/0") as lock:
        async with lock.hold("resource"):
            pass

    assert client.close_calls == 1
