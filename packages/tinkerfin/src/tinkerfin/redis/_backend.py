"""RunIdentity-driven Redis coordination composed from the reusable lease lock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Self

from redis.asyncio import Redis

from tinkerfin_contracts import RunIdentity

from ..coordination import _coordination_key
from ..errors import (
    RedisLeaseError,
    RedisLeaseTimeoutError,
    RedisLeaseUnavailableError,
    RunCoordinationError,
    RunCoordinationOwnershipLostError,
    RunCoordinationTimeoutError,
    RunCoordinationUnavailableError,
)
from ._lease_lock import RedisLeaseLock, RedisLeaseLost

_DEFAULT_KEY_PREFIX = "tinkerfin:run:"
_DEFAULT_LEASE_TTL_SECONDS = 30.0
_DEFAULT_WAIT_POLL_SECONDS = 0.1


def _coordination_error(
    operation: str,
    error: Exception,
) -> RunCoordinationError:
    """Translate lease failures into the stable coordinator error taxonomy.

    Ownership loss, deadline exhaustion, and backend unavailability remain distinct for
    host recovery policy. The original lease error and Redis operation stay in trusted
    diagnostic context rather than client-safe messages.
    """

    diagnostic_context = {"implementation": "redis", "operation": operation}
    if isinstance(error, RunCoordinationError):
        return error
    if isinstance(error, RedisLeaseLost):
        return RunCoordinationOwnershipLostError(
            "Run coordination ownership was lost",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    if isinstance(error, RedisLeaseTimeoutError | TimeoutError):
        return RunCoordinationTimeoutError(
            "Run coordination timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    if isinstance(error, RedisLeaseUnavailableError):
        return RunCoordinationUnavailableError(
            "Run coordination is unavailable",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return RunCoordinationError(
        "Run coordination failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


class RedisRunCoordinator:
    """Serialize Graph runs whose RunIdentity resolves to the same Redis lease key.

    The coordinator deliberately keeps the small `RunCoordinator` extension shape.
    `RedisLeaseLock` owns acquisition, fencing, renewal, cancellation, release, and
    Redis client lifecycle details.
    """

    def __init__(
        self,
        *,
        lease_lock: RedisLeaseLock,
        key_resolver: Callable[[RunIdentity], str] | None = None,
    ) -> None:
        """Bind a borrowed lease manager and canonical Run-to-resource resolver."""

        if not isinstance(lease_lock, RedisLeaseLock):
            raise TypeError("lease_lock must be a RedisLeaseLock")
        if key_resolver is not None and not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        self._lease_lock = lease_lock
        self._key_resolver = key_resolver

    @classmethod
    def from_client(
        cls,
        client: Redis,
        *,
        key_resolver: Callable[[RunIdentity], str] | None = None,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator:
        """Create a coordinator that borrows one asynchronous Redis client."""

        if key_resolver is not None and not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        return cls(
            lease_lock=RedisLeaseLock.from_client(
                client,
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
            key_resolver=key_resolver,
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_resolver: Callable[[RunIdentity], str] | None = None,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator:
        """Create a coordinator that owns the Redis client built from `url`."""

        if key_resolver is not None and not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        return cls(
            lease_lock=RedisLeaseLock.from_url(
                url,
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
            key_resolver=key_resolver,
        )

    async def __aenter__(self) -> Self:
        try:
            await self._lease_lock.__aenter__()
        except RedisLeaseError as error:
            translated = _coordination_error("start", error)
            raise translated from error
        except Exception as error:
            translated = _coordination_error("start", error)
            raise translated from error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self._lease_lock.__aexit__(exc_type, exc_value, traceback)
        except Exception as error:
            translated = _coordination_error("close", error)
            if exc_value is not None:
                exc_value.add_note(str(translated))
                return
            raise translated from error

    def __call__(
        self,
        identity: RunIdentity,
        /,
    ) -> AbstractAsyncContextManager[None]:
        return self._coordinate(identity)

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _coordinate(self, identity: RunIdentity) -> AsyncIterator[None]:
        resolved_key = _coordination_key(identity, self._key_resolver)
        context = self._lease_lock.hold(resolved_key)
        try:
            await context.__aenter__()
        except Exception as error:
            translated = _coordination_error("enter", error)
            raise translated from error

        primary: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await context.__aexit__(
                    None if primary is None else type(primary),
                    primary,
                    None if primary is None else primary.__traceback__,
                )
            except Exception as error:
                translated = _coordination_error("exit", error)
                if primary is not None:
                    primary.add_note(str(translated))
                else:
                    raise translated from error


__all__ = ["RedisRunCoordinator"]
