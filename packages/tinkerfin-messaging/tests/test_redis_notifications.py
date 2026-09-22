"""Deterministic Redis wait ownership without network or elapsed-time assertions."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Self

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection
from redis.exceptions import RedisError
from redis.typing import KeyT, StreamIdT

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MessagingBackendError,
    MessagingBackendProtocolError,
    RedisBackend,
    _redis_notifications,
)
from tinkerfin_messaging.backend_contract import (
    MessagingChangeCursor,
    MessagingChangeWait,
)

_Read = list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]


class _RedisScenario:
    def __init__(self) -> None:
        self.children: list[_ControlledRedis] = []
        self.reads: asyncio.Queue[Mapping[KeyT, StreamIdT]] = asyncio.Queue(maxsize=8)
        self.replies: asyncio.Queue[_Read | BaseException] = asyncio.Queue(maxsize=8)
        self.checked: asyncio.Queue[None] = asyncio.Queue(maxsize=8)
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.release_close.set()
        self.cursor = [b"", b"1", b"active", b"0", b"0"]
        self.max_connections = 0
        self.stream = b""
        self.scopes: list[bytes] = []
        self.fail_close = False
        self.close_error: BaseException | None = None


class _ControlledRedis(Redis):
    """Control Redis responses at the existing asynchronous client boundary."""

    def __init__(self, scenario: _RedisScenario, *, pinned: bool = False) -> None:
        super().__init__(socket_timeout=None)
        self.scenario = scenario
        self.pinned = pinned

    def client(self) -> _ControlledRedis:
        child = _ControlledRedis(self.scenario, pinned=True)
        self.scenario.children.append(child)
        return child

    async def initialize(self) -> Self:
        self.connection = Connection(socket_timeout=None)
        active = sum(child.connection is not None for child in self.scenario.children)
        self.scenario.max_connections = max(self.scenario.max_connections, active)
        return self

    async def execute_command(self, *args: object, **options: object) -> object:
        if args[0] == "GET":
            self.scenario.checked.put_nowait(None)
            return None
        if args[0] == "EVAL":
            self.scenario.checked.put_nowait(None)
            control = args[3]
            assert isinstance(control, str)
            self.scenario.scopes.append(
                hashlib.sha1(control.encode(), usedforsecurity=False)
                .hexdigest()
                .encode()
            )
            return list(self.scenario.cursor)
        raise AssertionError(f"unexpected test command: {args[0]}")

    async def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> _Read:
        self.scenario.reads.put_nowait(streams)
        stream = next(iter(streams))
        assert isinstance(stream, str)
        self.scenario.stream = stream.encode()
        response = await self.scenario.replies.get()
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        if self.pinned:
            self.scenario.closing.set()
            await self.scenario.release_close.wait()
            self.connection = None
        await super().aclose(close_connection_pool)
        if self.pinned and self.scenario.close_error is not None:
            raise self.scenario.close_error
        if self.pinned and self.scenario.fail_close:
            raise RedisError("controlled blocking close failure")


def _wait(thread: str) -> MessagingChangeWait:
    return MessagingChangeWait(
        channel="events",
        identity=RunIdentity(namespace="test", thread_id=thread, run_id="run"),
        generation=1,
        after=MessagingChangeCursor(message_sequence=0, control_sequence=0),
        timeout_seconds=5.0,
    )


@pytest.fixture(autouse=True)
def notification_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let test signals control completion instead of the real polling deadline."""

    async def wait_for_hint(ready: asyncio.Future[None], timeout: float) -> None:
        del timeout
        await asyncio.shield(ready)

    monkeypatch.setattr(_redis_notifications, "_wait_for_hint", wait_for_hint)


async def _wait_outcome(backend: RedisBackend) -> BaseException | None:
    # Process-control failures are test results, not instructions to stop pytest.
    try:
        await backend.wait_for_messaging_change(_wait("first"))
    except BaseException as error:  # noqa: BLE001 - inspect driver control failures without stopping pytest
        return error
    return None


async def test_redis_waits_for_distinct_threads_share_one_connection() -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    first = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    second: asyncio.Task[None] | None = None
    try:
        await scenario.reads.get()
        await scenario.checked.get()
        second = asyncio.create_task(backend.wait_for_messaging_change(_wait("second")))
        await scenario.checked.get()
        assert len(scenario.children) == 1
    finally:
        first.cancel()
        pending = [first]
        if second is not None:
            second.cancel()
            pending.append(second)
        await asyncio.gather(*pending, return_exceptions=True)
        assert all(child.connection is None for child in scenario.children)
        await client.aclose()


def _notification(
    scenario: _RedisScenario,
    *,
    sequence: int = 1,
    generation: int = 1,
    scope: bytes | None = None,
) -> _Read:
    """Reply using the stream and control scope observed at the Redis boundary."""
    return [
        (
            scenario.stream,
            [
                (
                    f"{sequence}-0".encode(),
                    {
                        b"scope": scope if scope is not None else scenario.scopes[-1],
                        b"generation": str(generation).encode(),
                        b"message": b"1",
                        b"control": b"0",
                    },
                )
            ],
        )
    ]


@pytest.mark.parametrize("second_thread", ["first", "second"])
async def test_notification_cancellation_does_not_detach_another_waiter(
    second_thread: str,
) -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    first = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    second: asyncio.Task[None] | None = None
    try:
        await scenario.reads.get()
        await scenario.checked.get()
        second = asyncio.create_task(
            backend.wait_for_messaging_change(_wait(second_thread))
        )
        await scenario.checked.get()
        first.cancel("first consumer left")
        with pytest.raises(asyncio.CancelledError, match="first consumer left"):
            await first
        assert not second.done()
        assert scenario.children[0].connection is not None
        scenario.replies.put_nowait(_notification(scenario))
        await second
        assert all(child.connection is None for child in scenario.children)
    finally:
        pending = [first] if second is None else [first, second]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await client.aclose()


async def test_notifications_keep_thread_and_generation_isolation() -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(_notification(scenario, scope=b"0" * 40))
        await scenario.reads.get()
        assert not waiting.done()
        scenario.replies.put_nowait(_notification(scenario, sequence=2, generation=2))
        await scenario.reads.get()
        assert not waiting.done()
        scenario.replies.put_nowait(_notification(scenario, sequence=3))
        await waiting
        assert scenario.max_connections == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


async def test_trimmed_notifications_wake_all_active_views() -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(
            _notification(scenario, sequence=4097, scope=b"0" * 40)
        )
        await waiting
        assert all(child.connection is None for child in scenario.children)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


async def test_registration_rechecks_changes_already_read_without_local_waiters() -> (
    None
):
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(_notification(scenario, scope=b"0" * 40))
        await scenario.reads.get()
        scenario.cursor[3] = b"1"
        await backend.wait_for_messaging_change(_wait("second"))
        assert not waiting.done()
        assert scenario.max_connections == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize("fail_close", [False, True])
async def test_last_waiter_repeated_cancellation_settles_close_before_new_reader(
    fail_close: bool,
) -> None:
    scenario = _RedisScenario()
    scenario.fail_close = fail_close
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    first = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    second: asyncio.Task[None] | None = None
    try:
        await scenario.reads.get()
        scenario.release_close.clear()
        first.cancel("first cancellation")
        await scenario.closing.wait()
        first.cancel("second cancellation")
        second = asyncio.create_task(backend.wait_for_messaging_change(_wait("second")))
        assert not first.done()
        scenario.release_close.set()
        with pytest.raises(
            asyncio.CancelledError, match="first cancellation"
        ) as raised:
            await first
        if fail_close:
            assert any(
                "blocking client close" in note for note in raised.value.__notes__
            )
        scenario.fail_close = False
        await scenario.reads.get()
        assert scenario.max_connections == 1
        scenario.replies.put_nowait(_notification(scenario))
        await second
    finally:
        scenario.release_close.set()
        pending = [first] if second is None else [first, second]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await client.aclose()


async def test_shared_reader_failure_reaches_all_registered_waiters() -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    first = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    second: asyncio.Task[None] | None = None
    try:
        await scenario.reads.get()
        await scenario.checked.get()
        second = asyncio.create_task(backend.wait_for_messaging_change(_wait("second")))
        await scenario.checked.get()
        scenario.replies.put_nowait(RedisError("controlled read failure"))
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(error, MessagingBackendError) for error in results)
        assert all(child.connection is None for child in scenario.children)
        recovered = asyncio.create_task(
            backend.wait_for_messaging_change(_wait("recovered"))
        )
        try:
            await scenario.reads.get()
            scenario.replies.put_nowait(_notification(scenario))
            await recovered
        finally:
            recovered.cancel()
            await asyncio.gather(recovered, return_exceptions=True)
    finally:
        pending = [first] if second is None else [first, second]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await client.aclose()


async def test_new_waiter_can_cancel_while_previous_reader_is_closing() -> None:
    entered = asyncio.Event()

    async def wait_for_second(backend: RedisBackend) -> None:
        entered.set()
        await backend.wait_for_messaging_change(_wait("second"))

    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    first = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    second: asyncio.Task[None] | None = None
    try:
        await scenario.reads.get()
        scenario.release_close.clear()
        first.cancel("original owner left")
        await scenario.closing.wait()
        second = asyncio.create_task(wait_for_second(backend))
        await entered.wait()
        second.cancel("new waiter left")
        with pytest.raises(asyncio.CancelledError, match="new waiter left"):
            await second
        assert not first.done()
        assert scenario.children[0].connection is not None
        scenario.release_close.set()
        with pytest.raises(asyncio.CancelledError, match="original owner left"):
            await first
        assert scenario.max_connections == 1
        assert all(child.connection is None for child in scenario.children)
    finally:
        scenario.release_close.set()
        pending = [first] if second is None else [first, second]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await client.aclose()


async def test_malformed_metadata_preserves_protocol_errors_and_releases_reader() -> (
    None
):
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(backend.wait_for_messaging_change(_wait("first")))
    try:
        await scenario.reads.get()
        response = _notification(scenario)
        response[0][1][0][1][b"generation"] = b"01"
        scenario.replies.put_nowait(response)
        with pytest.raises(MessagingBackendProtocolError):
            await waiting
        assert all(child.connection is None for child in scenario.children)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize(
    "failure_type",
    [asyncio.CancelledError, GeneratorExit, SystemExit, KeyboardInterrupt],
)
async def test_reader_control_failures_wake_waiters_without_a_deadline(
    failure_type: type[BaseException],
) -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    failure = failure_type("driver stopped itself")

    waiting = asyncio.create_task(_wait_outcome(backend))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(failure)
        assert await waiting is failure
        assert all(child.connection is None for child in scenario.children)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


async def test_reader_close_process_control_precedes_repeated_caller_cancellation() -> (
    None
):
    scenario = _RedisScenario()
    scenario.release_close.clear()
    failure = GeneratorExit("close process control")
    scenario.close_error = failure
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)

    waiting = asyncio.create_task(_wait_outcome(backend))
    try:
        await scenario.reads.get()
        waiting.cancel("original cancellation")
        await scenario.closing.wait()
        waiting.cancel("repeated cancellation")
        assert not waiting.done()
        scenario.release_close.set()
        assert await waiting is failure
        assert all(child.connection is None for child in scenario.children)
    finally:
        scenario.release_close.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


async def test_normal_notification_keeps_an_independent_reader_close_failure() -> None:
    scenario = _RedisScenario()
    scenario.release_close.clear()
    close_error = RedisError("independent close failure")
    scenario.close_error = close_error
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(_wait_outcome(backend))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(_notification(scenario))
        await scenario.closing.wait()
        assert waiting.cancelling() == 0
        scenario.release_close.set()
        outcome = await waiting
        assert isinstance(outcome, MessagingBackendError)
        assert outcome.cause is close_error
        assert all(child.connection is None for child in scenario.children)
    finally:
        scenario.release_close.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize(
    ("read_failure_type", "close_failure_type"),
    [
        (RedisError, RedisError),
        (GeneratorExit, RedisError),
        (RedisError, GeneratorExit),
        (asyncio.CancelledError, RedisError),
    ],
)
async def test_reader_preserves_independent_read_and_close_failures(
    read_failure_type: type[BaseException], close_failure_type: type[BaseException]
) -> None:
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    read_error = read_failure_type("read failure")
    close_error = close_failure_type("close failure")
    scenario.close_error = close_error

    waiting = asyncio.create_task(_wait_outcome(backend))
    try:
        await scenario.reads.get()
        scenario.replies.put_nowait(read_error)
        outcome = await waiting
        assert isinstance(outcome, BaseException)
        pending = [outcome]
        seen: set[int] = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            pending.extend(
                item
                for item in (error.__cause__, error.__context__)
                if item is not None
            )
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        assert id(read_error) in seen and id(close_error) in seen
        if isinstance(close_error, GeneratorExit):
            assert outcome is close_error
        elif not isinstance(read_error, Exception):
            assert outcome is read_error
        else:
            assert isinstance(outcome, MessagingBackendError)
        assert all(child.connection is None for child in scenario.children)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()


async def test_wait_expiration_preserves_the_callers_lease_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = asyncio.Event()
    entered = asyncio.Event()
    deadlines: list[float] = []

    async def expire(ready: asyncio.Future[None], timeout: float) -> None:
        deadlines.append(timeout)
        entered.set()
        await elapsed.wait()

    monkeypatch.setattr(_redis_notifications, "_wait_for_hint", expire)
    scenario = _RedisScenario()
    client = _ControlledRedis(scenario)
    backend = RedisBackend(client)
    waiting = asyncio.create_task(
        backend.wait_for_messaging_change(replace(_wait("first"), timeout_seconds=0.25))
    )
    try:
        await entered.wait()
        await scenario.reads.get()
        assert deadlines == [0.25]
        elapsed.set()
        await waiting
        assert all(child.connection is None for child in scenario.children)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await client.aclose()
