"""Public Messaging error-family and backend-boundary contracts."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    InvalidCursor,
    MemoryBackend,
    Messaging,
    MessagingBackendProtocolError,
    MessagingBackendTimeout,
    MessagingBackendUnavailable,
    MessagingError,
    MessagingErrorCode,
    RedisBackend,
    UnexpectedMessagingBackendError,
)
from tinkerfin_messaging.backend_contract import (
    MessagingChangeCursor,
    MessagingChangeWait,
    MessagingStateQuery,
    MessagingStateSnapshot,
)


class _FailingBackend(MemoryBackend):
    async def load_messaging_state(
        self,
        query: MessagingStateQuery,
    ) -> MessagingStateSnapshot:
        del query
        raise ConnectionError("implementation detail")


class _FailingRedis:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_connection_kwargs(self) -> dict[str, object]:
        return {"decode_responses": False}

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        del name
        raise self.error

    async def eval(self, script: str, numkeys: int, *keys: str) -> object:
        del script, numkeys, keys
        raise self.error


class _ProtocolRedis:
    def get_connection_kwargs(self) -> dict[str, object]:
        return {"decode_responses": False}

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        del name
        return {b"state": b"corrupt", b"generation": b"1"}

    async def eval(self, script: str, numkeys: int, *keys: str) -> object:
        del script, numkeys, keys
        return [b"OK", b"1", b"corrupt"]


class _CancellationSuppressingRedis(Redis):
    """Inject a driver that consumes cancellation before returning or failing."""

    def __init__(self, error: BaseException | None) -> None:
        super().__init__(decode_responses=False)
        self.error = error
        self.entered = asyncio.Event()

    async def execute_command(self, *args: object, **options: object) -> object:
        del args, options
        if self.entered.is_set():
            raise AssertionError("a cancelled operation issued another Redis command")
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.error is not None:
                raise self.error from None
            return None


@pytest.mark.parametrize("cancel_requests", [1, 2])
@pytest.mark.parametrize(
    "driver_error",
    [
        None,
        RedisConnectionError("connection lost after cancellation"),
        RedisTimeoutError("command timed out after cancellation"),
        RuntimeError("driver failed after cancellation"),
    ],
)
async def test_redis_change_wait_preserves_cancellation_consumed_by_driver(
    driver_error: Exception | None,
    cancel_requests: int,
) -> None:
    client = _CancellationSuppressingRedis(driver_error)
    backend = RedisBackend(client)
    pending = asyncio.create_task(
        backend.wait_for_messaging_change(
            MessagingChangeWait(
                channel="events",
                identity=RunIdentity(
                    namespace="test", thread_id="thread-1", run_id="run-1"
                ),
                generation=1,
                after=MessagingChangeCursor(message_sequence=0, control_sequence=0),
                timeout_seconds=1,
            )
        )
    )
    try:
        await client.entered.wait()
        for _ in range(cancel_requests):
            pending.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await pending
        assert raised.value.__cause__ is driver_error
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize(
    "driver_error", [asyncio.CancelledError("driver cancellation"), GeneratorExit()]
)
async def test_redis_backend_preserves_driver_control_exceptions(
    driver_error: BaseException,
) -> None:
    client = _CancellationSuppressingRedis(driver_error)
    backend = RedisBackend(client)
    pending = asyncio.create_task(
        backend.load_messaging_state(
            MessagingStateQuery(
                channel="events",
                identity=RunIdentity(
                    namespace="test", thread_id="thread-1", run_id="run-1"
                ),
            )
        )
    )
    try:
        await client.entered.wait()
        pending.cancel()
        with pytest.raises(type(driver_error)) as raised:
            await pending
        assert raised.value is driver_error
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await client.aclose()


def test_error_codes_are_unique_and_namespaced() -> None:
    values = [code.value for code in MessagingErrorCode]

    assert len(values) == len(set(values))
    assert all(value.startswith("messaging.") for value in values)


def test_semantic_error_exposes_read_only_context() -> None:
    error = InvalidCursor(after=3, latest=2)

    assert isinstance(error, MessagingError)
    assert error.code is MessagingErrorCode.INVALID_CURSOR
    assert dict(error.context) == {"after": 3, "latest": 2}
    assert dict(error.diagnostic_context) == {}
    assert not hasattr(error.context, "__setitem__")
    assert not hasattr(error.diagnostic_context, "__setitem__")


def test_error_separates_and_copies_safe_and_diagnostic_context() -> None:
    context = {"retryable": True}
    diagnostic_context = {"implementation": "custom", "operation": "read"}
    error = MessagingError(
        "Messaging failed",
        context=context,
        diagnostic_context=diagnostic_context,
    )
    context["retryable"] = False
    diagnostic_context["operation"] = "mutated"

    assert str(error) == "Messaging failed"
    assert dict(error.context) == {"retryable": True}
    assert dict(error.diagnostic_context) == {
        "implementation": "custom",
        "operation": "read",
    }


async def test_facade_wraps_an_undeclared_custom_backend_failure() -> None:
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")
    async with Messaging(backend=_FailingBackend()) as messaging:
        channel = messaging.channel(name="events")
        with pytest.raises(UnexpectedMessagingBackendError) as raised:
            await channel.latest_seq(identity=identity)
    assert isinstance(raised.value.cause, ConnectionError)
    assert raised.value.code is MessagingErrorCode.UNEXPECTED_BACKEND_FAILURE
    assert dict(raised.value.context) == {}
    assert dict(raised.value.diagnostic_context) == {"operation": "latest_seq"}


@pytest.mark.parametrize(
    ("driver_error", "expected_type", "expected_code"),
    [
        (
            RedisConnectionError("connection lost"),
            MessagingBackendUnavailable,
            MessagingErrorCode.BACKEND_UNAVAILABLE,
        ),
        (
            RedisTimeoutError("command timed out"),
            MessagingBackendTimeout,
            MessagingErrorCode.BACKEND_TIMEOUT,
        ),
    ],
)
async def test_redis_backend_translates_driver_failures(
    driver_error: Exception,
    expected_type: type[MessagingError],
    expected_code: MessagingErrorCode,
) -> None:
    client = cast(Redis, _FailingRedis(driver_error))
    backend = RedisBackend(client)
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")

    with pytest.raises(expected_type) as raised:
        await backend.load_messaging_state(
            MessagingStateQuery(channel="events", identity=identity)
        )

    assert raised.value.code is expected_code
    assert raised.value.cause is driver_error
    assert dict(raised.value.context) == {}
    assert dict(raised.value.diagnostic_context) == {
        "implementation": "redis",
        "operation": "stream control read",
    }
    assert "Redis" not in str(raised.value)


async def test_redis_protocol_details_are_trusted_diagnostics_only() -> None:
    client = cast(Redis, _ProtocolRedis())
    backend = RedisBackend(client)
    identity = RunIdentity(namespace="test", thread_id="thread-1", run_id="run-1")

    with pytest.raises(MessagingBackendProtocolError) as raised:
        await backend.load_messaging_state(
            MessagingStateQuery(channel="events", identity=identity)
        )

    assert dict(raised.value.context) == {}
    assert dict(raised.value.diagnostic_context) == {
        "implementation": "redis",
        "operation": "protocol_validation",
        "detail": "Redis stream control has invalid state: 'corrupt'",
    }
    assert (
        str(raised.value) == "Messaging backend returned an invalid protocol response"
    )
