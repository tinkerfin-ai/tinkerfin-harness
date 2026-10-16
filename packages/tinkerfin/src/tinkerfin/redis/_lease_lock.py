"""Cancellation-safe Redis leases with automatic renewal and fencing tokens."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .._tasks import join_task
from ..errors import (
    RedisLeaseError,
    RedisLeaseLifecycleError,
    RedisLeaseProtocolError,
    RedisLeaseTimeoutError,
    RedisLeaseUnavailableError,
    TinkerFinErrorCode,
)

_DEFAULT_KEY_PREFIX = "tinkerfin:lease:"
_DEFAULT_LEASE_TTL_SECONDS = 30.0
_DEFAULT_WAIT_POLL_SECONDS = 0.1
_RENEW_COMMAND_DEADLINE_RATIO = 0.8

_ACQUIRE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
local fencing_token = redis.call('INCR', KEYS[2])
redis.call('PSETEX', KEYS[1], ARGV[2], ARGV[1])
return fencing_token
"""

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
return redis.call('DEL', KEYS[1])
"""

_RedisClient = Redis


class _AsyncRedisClient(Protocol):
    """Record the awaitable Redis API used by the lease lifecycle."""

    async def ping(self) -> bool: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes | int,
    ) -> object: ...

    async def aclose(self) -> None: ...


def _positive_seconds(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return resolved


def _key_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(
            "key_prefix must be a non-blank string without surrounding whitespace"
        )
    return value if value.endswith(":") else f"{value}:"


def _resource_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("resource_key must be a string")
    if not value or value != value.strip():
        raise ValueError(
            "resource_key must be non-blank without surrounding whitespace"
        )
    return value


def _redis_integer(value: object, *, operation: str) -> int:
    if isinstance(value, bool):
        raise RedisLeaseProtocolError(
            f"Redis {operation} returned an invalid boolean reply",
            diagnostic_context={"implementation": "redis", "operation": operation},
        )
    if isinstance(value, int):
        return value
    if isinstance(value, bytes | str):
        try:
            return int(value)
        except ValueError as error:
            raise RedisLeaseProtocolError(
                f"Redis {operation} returned a non-integer reply",
                diagnostic_context={
                    "implementation": "redis",
                    "operation": operation,
                },
                cause=error,
            ) from error
    raise RedisLeaseProtocolError(
        f"Redis {operation} returned a non-integer reply",
        diagnostic_context={"implementation": "redis", "operation": operation},
    )


def _redis_operation_error(
    operation: str,
    error: Exception,
) -> RedisLeaseError:
    diagnostic_context = {"implementation": "redis", "operation": operation}
    if isinstance(error, RedisTimeoutError | TimeoutError):
        return RedisLeaseTimeoutError(
            f"Redis lease {operation} timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    if isinstance(error, RedisConnectionError):
        return RedisLeaseUnavailableError(
            f"Redis is unavailable for lease {operation}",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return RedisLeaseError(
        f"Redis lease {operation} failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


@dataclass(frozen=True, slots=True)
class RedisLease:
    """One acquired resource lease and its monotonic fencing token.

    The owner token remains private to Redis renewal and release. Callers may pass
    `fencing_token` to an external conditional-write mechanism when stale writers
    must be rejected after a lease expires.
    """

    resource_key: str
    fencing_token: int


class RedisLeaseLost(RedisLeaseError):
    """The Redis lease can no longer authorize critical-section work."""

    code = TinkerFinErrorCode.REDIS_LEASE_LOST

    def __init__(
        self,
        *,
        lease: RedisLease,
        cause: BaseException | None = None,
    ) -> None:
        self.lease = lease
        message = "Redis lease ownership was lost"
        super().__init__(
            message,
            diagnostic_context={
                "implementation": "redis",
                "operation": "ownership",
                "resource_key": lease.resource_key,
                "fencing_token": lease.fencing_token,
            },
            cause=cause,
        )


@dataclass(frozen=True, slots=True)
class _Settings:
    key_prefix: str
    lease_ttl_seconds: float
    renew_interval_seconds: float
    wait_poll_seconds: float

    @classmethod
    def resolve(
        cls,
        *,
        key_prefix: str,
        lease_ttl_seconds: float,
        renew_interval_seconds: float | None,
        wait_poll_seconds: float,
    ) -> _Settings:
        ttl = _positive_seconds("lease_ttl_seconds", lease_ttl_seconds)
        renewal = (
            ttl / 3
            if renew_interval_seconds is None
            else _positive_seconds("renew_interval_seconds", renew_interval_seconds)
        )
        if renewal >= ttl / 2:
            raise ValueError(
                "renew_interval_seconds must be less than half lease_ttl_seconds"
            )
        return cls(
            key_prefix=_key_prefix(key_prefix),
            lease_ttl_seconds=ttl,
            renew_interval_seconds=renewal,
            wait_poll_seconds=_positive_seconds(
                "wait_poll_seconds",
                wait_poll_seconds,
            ),
        )

    @property
    def lease_ttl_milliseconds(self) -> int:
        return max(1, math.ceil(self.lease_ttl_seconds * 1000))

    @property
    def command_timeout_seconds(self) -> float:
        return self.lease_ttl_seconds * _RENEW_COMMAND_DEADLINE_RATIO


@dataclass(slots=True)
class _HeldLease:
    lease: RedisLease
    lock_key: str
    fencing_key: str
    owner_token: str
    owner_task: asyncio.Task[object]
    renewal_started: asyncio.Event
    lost_error: RedisLeaseLost | None = None


class RedisLeaseLock:
    """Manage independent renewable Redis leases in one async lifecycle.

    Acquisition uses one Lua script to allocate the fencing token and write the
    expiring owner token atomically. Renewal and release follow redis-py's
    token-checked lock semantics, while the owned asyncio renewal task adds the
    lifecycle, fail-closed cancellation, and cleanup guarantees needed by TinkerFin.

    `from_client()` borrows its Redis client. `from_url()` owns and closes the client
    it creates. Redis Cluster is intentionally unsupported because all lease and
    fencing keys must share one non-cluster atomic script boundary.
    """

    def __init__(
        self,
        *,
        client: _RedisClient,
        owns_client: bool,
        settings: _Settings,
    ) -> None:
        self._client = cast(_AsyncRedisClient, client)
        self._owns_client = owns_client
        self._settings = settings
        self._state = "new"
        self._state_lock = asyncio.Lock()
        self._active_scopes = 0
        self._active_scopes_done = asyncio.Event()
        self._active_scopes_done.set()
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def from_client(
        cls,
        client: _RedisClient,
        *,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisLeaseLock:
        """Create a lease manager that borrows one non-cluster Redis client."""

        if isinstance(client, RedisCluster):
            raise TypeError(
                "Redis Cluster is unsupported by the RedisLeaseLock client boundary"
            )
        return cls(
            client=client,
            owns_client=False,
            settings=_Settings.resolve(
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisLeaseLock:
        """Create a lease manager that owns the Redis client built from `url`."""

        settings = _Settings.resolve(
            key_prefix=key_prefix,
            lease_ttl_seconds=lease_ttl_seconds,
            renew_interval_seconds=renew_interval_seconds,
            wait_poll_seconds=wait_poll_seconds,
        )
        redis_from_url = cast(
            Callable[..., Redis],
            Redis.from_url,  # pyright: ignore[reportUnknownMemberType]
        )
        client = redis_from_url(url, decode_responses=False)
        return cls(
            client=client,
            owns_client=True,
            settings=settings,
        )

    async def __aenter__(self) -> Self:
        startup_error: BaseException | None = None
        async with self._state_lock:
            if self._state != "new":
                raise RedisLeaseLifecycleError("RedisLeaseLock is single-use")
            self._state = "opening"
            try:
                async with asyncio.timeout(self._settings.command_timeout_seconds):
                    await self._client.ping()
            except BaseException as error:  # noqa: BLE001 - owned cleanup is mandatory
                self._state = "closed"
                startup_error = error
            else:
                self._state = "open"
                return self

        assert startup_error is not None
        if self._owns_client:
            close_task = asyncio.create_task(
                self._client.aclose(),
                name="tinkerfin-redis-lease-startup-close",
            )
            try:
                await join_task(close_task)
            except asyncio.CancelledError as cancellation:
                cancellation.add_note(
                    "Redis lease startup also failed: "
                    f"{type(startup_error).__name__}: {startup_error}"
                )
                raise
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary
                startup_error.add_note(
                    "Redis lease startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        if isinstance(startup_error, Exception):
            health_error = _redis_operation_error("health check", startup_error)
            raise health_error from startup_error
        raise startup_error.with_traceback(startup_error.__traceback__)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            exc_value.add_note(
                "Redis lease manager cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def aclose(self) -> None:
        """Reject new leases, wait for active scopes, and close an owned client."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name="tinkerfin-redis-lease-close",
            )
            self._close_task = task
        await join_task(task)

    async def _close_once(self) -> None:
        async with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closing"
        try:
            await self._active_scopes_done.wait()
            if self._owns_client:
                try:
                    await self._client.aclose()
                except Exception as error:
                    translated = _redis_operation_error("client close", error)
                    raise translated from error
        finally:
            async with self._state_lock:
                self._state = "closed"

    def hold(self, resource_key: str) -> AbstractAsyncContextManager[RedisLease]:
        """Return an async scope that acquires and renews one resource lease."""

        return self._hold(resource_key)

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _hold(self, resource_key: str) -> AsyncIterator[RedisLease]:
        canonical_key = _resource_key(resource_key)
        await self._register_scope()
        digest = hashlib.sha256(canonical_key.encode()).hexdigest()
        base = f"{self._settings.key_prefix}{digest}"
        lock_key = f"{base}:lease"
        fencing_key = f"{base}:fencing"
        owner_token = uuid4().hex
        held: _HeldLease | None = None
        renewal: asyncio.Task[None] | None = None
        acquired = False
        primary: BaseException | None = None
        try:
            fencing_token = await self._acquire(
                lock_key=lock_key,
                fencing_key=fencing_key,
                owner_token=owner_token,
            )
            acquired = True
            if not await self._is_open():
                raise RedisLeaseLifecycleError("RedisLeaseLock is closing")
            owner_task = cast(asyncio.Task[object] | None, asyncio.current_task())
            if owner_task is None:  # pragma: no cover - async contexts run in Tasks
                raise RedisLeaseLifecycleError(
                    "Redis lease ownership requires an asyncio task"
                )
            lease = RedisLease(
                resource_key=canonical_key,
                fencing_token=fencing_token,
            )
            held = _HeldLease(
                lease=lease,
                lock_key=lock_key,
                fencing_key=fencing_key,
                owner_token=owner_token,
                owner_task=owner_task,
                renewal_started=asyncio.Event(),
            )
            renewal = asyncio.create_task(
                self._renew_forever(held),
                name=f"tinkerfin-redis-lease-renew:{digest}",
            )
            await held.renewal_started.wait()
            yield lease
        except BaseException as error:  # noqa: BLE001 - preserve scope outcome
            primary = error
        finally:
            cleanup_errors: list[BaseException] = []
            if renewal is not None:
                try:
                    await join_task(
                        renewal,
                        cancel=True,
                        suppress_task_cancellation=True,
                    )
                except BaseException as error:  # noqa: BLE001 - finish all cleanup
                    cleanup_errors.append(error)
            if acquired:
                try:
                    released = await self._release(
                        lock_key=lock_key,
                        owner_token=owner_token,
                    )
                    if released == 0 and held is not None and held.lost_error is None:
                        held.lost_error = RedisLeaseLost(lease=held.lease)
                except BaseException as error:  # noqa: BLE001 - resolve below
                    cleanup_errors.append(error)
            try:
                self._raise_scope_outcome(
                    primary=primary,
                    lost_error=None if held is None else held.lost_error,
                    cleanup_errors=cleanup_errors,
                )
            finally:
                await self._unregister_scope()

    async def _register_scope(self) -> None:
        async with self._state_lock:
            if self._state != "open":
                raise RedisLeaseLifecycleError(
                    "RedisLeaseLock must be entered before use"
                )
            self._active_scopes += 1
            self._active_scopes_done.clear()

    async def _unregister_scope(self) -> None:
        async with self._state_lock:
            self._active_scopes -= 1
            if self._active_scopes == 0:
                self._active_scopes_done.set()

    async def _is_open(self) -> bool:
        async with self._state_lock:
            return self._state == "open"

    async def _acquire(
        self,
        *,
        lock_key: str,
        fencing_key: str,
        owner_token: str,
    ) -> int:
        while True:
            if not await self._is_open():
                raise RedisLeaseLifecycleError("RedisLeaseLock is closing")
            try:

                async def evaluate() -> object:
                    async with asyncio.timeout(self._settings.command_timeout_seconds):
                        return await self._client.eval(
                            _ACQUIRE_SCRIPT,
                            2,
                            lock_key,
                            fencing_key,
                            owner_token,
                            self._settings.lease_ttl_milliseconds,
                        )

                attempt = asyncio.create_task(
                    evaluate(),
                    name="tinkerfin-redis-lease-acquire",
                )
                reply = await join_task(attempt)
                token = _redis_integer(reply, operation="acquisition")
            except (RedisError, TimeoutError) as error:
                await self._release_uncertain_acquisition(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    primary=error,
                )
                translated = _redis_operation_error("acquisition", error)
                raise translated from error
            except BaseException as error:
                await self._release_uncertain_acquisition(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    primary=error,
                )
                raise
            if token > 0:
                return token
            if token < 0:
                raise RedisLeaseProtocolError(
                    "Redis acquisition returned a negative fencing token",
                    diagnostic_context={
                        "implementation": "redis",
                        "operation": "acquisition",
                    },
                )
            await asyncio.sleep(self._settings.wait_poll_seconds)

    async def _release_uncertain_acquisition(
        self,
        *,
        lock_key: str,
        owner_token: str,
        primary: BaseException,
    ) -> None:
        try:
            await self._release(lock_key=lock_key, owner_token=owner_token)
        except asyncio.CancelledError as cancellation:
            if cancellation is not primary:
                cancellation.add_note(
                    "Redis acquisition also failed: "
                    f"{type(primary).__name__}: {primary}"
                )
            raise
        except BaseException as cleanup_error:  # noqa: BLE001 - retain primary
            primary.add_note(
                "Redis acquisition cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def _renew_forever(self, held: _HeldLease) -> None:
        loop = asyncio.get_running_loop()
        last_success = loop.time()
        held.renewal_started.set()
        while True:
            target = last_success + self._settings.renew_interval_seconds
            await asyncio.sleep(max(0.0, target - loop.time()))
            deadline = (
                last_success
                + self._settings.lease_ttl_seconds * _RENEW_COMMAND_DEADLINE_RATIO
            )
            try:
                if loop.time() >= deadline:
                    raise TimeoutError("renewal safety deadline elapsed")
                async with asyncio.timeout_at(deadline):
                    reply = await self._client.eval(
                        _RENEW_SCRIPT,
                        1,
                        held.lock_key,
                        held.owner_token,
                        self._settings.lease_ttl_milliseconds,
                    )
                renewed = _redis_integer(reply, operation="renewal")
                if renewed != 1:
                    raise RuntimeError("Redis renewal did not confirm lease ownership")
            except asyncio.CancelledError:
                raise
            except BaseException as error:  # noqa: BLE001 - uncertainty loses lease
                self._mark_lost(held, cause=error)
                return
            last_success = loop.time()

    @staticmethod
    def _mark_lost(held: _HeldLease, *, cause: BaseException) -> None:
        if held.lost_error is not None:
            return
        held.lost_error = RedisLeaseLost(lease=held.lease, cause=cause)
        held.owner_task.cancel(
            f"Redis lease lost for {held.lease.resource_key!r}: "
            f"{type(cause).__name__}: {cause}"
        )

    async def _release(self, *, lock_key: str, owner_token: str) -> int:
        async def evaluate() -> object:
            async with asyncio.timeout(self._settings.command_timeout_seconds):
                return await self._client.eval(
                    _RELEASE_SCRIPT,
                    1,
                    lock_key,
                    owner_token,
                )

        release = asyncio.create_task(
            evaluate(),
            name="tinkerfin-redis-lease-release",
        )
        try:
            reply = await join_task(release)
            released = _redis_integer(reply, operation="release")
        except (RedisError, TimeoutError) as error:
            translated = _redis_operation_error("release", error)
            raise translated from error
        if released not in {0, 1}:
            raise RedisLeaseProtocolError(
                "Redis release returned an invalid result",
                diagnostic_context={
                    "implementation": "redis",
                    "operation": "release",
                },
            )
        return released

    @staticmethod
    def _raise_scope_outcome(
        *,
        primary: BaseException | None,
        lost_error: RedisLeaseLost | None,
        cleanup_errors: list[BaseException],
    ) -> None:
        diagnostics: list[BaseException] = []
        if lost_error is not None:
            diagnostics.append(lost_error)
        diagnostics.extend(cleanup_errors)
        if primary is not None:
            for error in diagnostics:
                primary.add_note(
                    f"Redis lease cleanup also failed: {type(error).__name__}: {error}"
                )
            raise primary.with_traceback(primary.__traceback__)
        if lost_error is not None:
            for error in cleanup_errors:
                lost_error.add_note(
                    f"Redis lease cleanup also failed: {type(error).__name__}: {error}"
                )
            raise lost_error
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            group = BaseExceptionGroup("Redis lease cleanup failed", cleanup_errors)
            raise RedisLeaseError(
                "Redis lease cleanup failed",
                diagnostic_context={
                    "implementation": "redis",
                    "operation": "cleanup",
                },
                cause=group,
            ) from group


__all__ = ["RedisLease", "RedisLeaseLock", "RedisLeaseLost"]
