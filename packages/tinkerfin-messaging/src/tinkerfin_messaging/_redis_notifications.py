"""Share bounded Redis change hints without sharing replay or caller lifetimes."""

from __future__ import annotations

__all__ = ["_RedisNotifications", "_validate_wait"]

import asyncio
import hashlib
import math
from typing import TYPE_CHECKING

from ._redis_control import _read_notifications, _redis_protocol_error
from ._redis_scripts import _WAIT_CURSOR_SCRIPT
from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .backend_contract import MessagingChangeWait
from .errors import StreamDeleted, StreamExpired

if TYPE_CHECKING:
    from .redis import RedisBackend


class _WaitGroup:
    """Keep one current wakeup, never a queue of notifications or message bodies."""

    def __init__(self) -> None:
        self.users = 0
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def acquire(self) -> asyncio.Future[None]:
        if self.ready.done():
            self.ready = asyncio.get_running_loop().create_future()
        self.users += 1
        return self.ready

    def wake(self) -> None:
        if not self.ready.done():
            self.ready.set_result(None)


class _RedisNotifications:
    """Own one reader while calls are waiting on this borrowed backend.

    The Redis index retains at most 4096 fixed-size metadata records; one read returns
    at most 128. Python retains only that batch and groups with active callers. Each
    group owns one Future, so a slow caller cannot accumulate events or delay peers.
    There is no admission quota for a run to reacquire after each wakeup. Caller stack
    and Future callbacks still scale with actual concurrent calls, not event history.

    Digests route hints only. Every caller reloads generation-fenced durable state;
    notification loss, duplication, or a digest collision cannot authorize data access.
    """

    def __init__(self, backend: RedisBackend) -> None:
        self._backend = backend
        self._groups: dict[tuple[bytes, int], _WaitGroup] = {}
        self._reader: asyncio.Task[TaskOutcome[None]] | None = None
        self._closing: asyncio.Future[None] | None = None
        self._cursor = 0
        self._failure: BaseException | None = None

    async def wait(self, wait: MessagingChangeWait) -> None:
        """Retain cleanup even if a transport abandons its nested generator pull."""

        task = asyncio.create_task(
            self._run_wait(wait), name="tinkerfin-messaging-redis-wait"
        )
        owner = asyncio.current_task()

        def owner_finished(_task: asyncio.Task[object]) -> None:
            if not task.done() and not task.cancelling():
                task.cancel()

        def finished(completed: asyncio.Task[TaskOutcome[None]]) -> None:
            if owner is not None:
                owner.remove_done_callback(owner_finished)
            if not completed.cancelled():
                completed.exception()

        if owner is not None:
            owner.add_done_callback(owner_finished)
        task.add_done_callback(finished)
        primary: BaseException | None = None
        try:
            # This task belongs to this caller alone. Forwarding cancellation into
            # its captured outcome preserves the driver's original cancellation
            # and cause instead of creating a second cancellation at a shield.
            outcome = await task
            if isinstance(outcome, BaseException):
                raise outcome
        except BaseException as error:
            primary = error
            raise
        finally:
            if not task.done() and not task.cancelling():
                task.cancel()
            try:
                await join_owned_task(task)
            except BaseException as error:  # noqa: BLE001 - retain the captured control outcome
                outcome = None if task.cancelled() else task.result()
                if isinstance(primary, asyncio.CancelledError) and isinstance(
                    outcome, asyncio.CancelledError
                ):
                    # A later request can arrive after this exclusive operation
                    # captured cancellation but before its caller resumes. Retain
                    # the operation's first cancellation and direct driver cause.
                    primary = outcome
                else:
                    primary = (
                        error if primary is None else select_failure(primary, error)
                    )
                raise primary

    async def _run_wait(self, wait: MessagingChangeWait) -> TaskOutcome[None]:
        return await capture(self._wait(wait))

    async def _wait(self, wait: MessagingChangeWait) -> None:
        while self._closing is not None:
            # Closing belongs to the preceding calls. New callers wait only for
            # released resources and never adopt that previous operation's result.
            await asyncio.shield(self._closing)
        keys = self._backend._keys(
            wait.channel, wait.identity, generation=wait.generation
        )
        scope = (
            hashlib.sha1(keys.control.encode(), usedforsecurity=False)
            .hexdigest()
            .encode()
        )
        key = (scope, wait.generation)
        group = self._groups.get(key)
        if group is None:
            group = _WaitGroup()
            self._groups[key] = group
        ready = group.acquire()
        primary: BaseException | None = None
        try:
            # Register before checking the durable cursor. A preceding hint may have
            # been read with no local subscribers; this check closes that race.
            response = await self._backend._eval(
                _WAIT_CURSOR_SCRIPT, [keys.control, keys.meta, keys.tombstone], []
            )
            if len(response) != 5:
                raise _redis_protocol_error("Redis wait cursor returned invalid fields")
            reason, generation, state, message, control = response
            if reason == b"expired":
                raise StreamExpired(
                    channel=wait.channel,
                    identity=wait.identity,
                    generation=wait.generation,
                )
            if reason == b"deleted":
                raise StreamDeleted(
                    channel=wait.channel,
                    identity=wait.identity,
                    generation=wait.generation,
                )
            if reason:
                raise _redis_protocol_error(
                    "Redis wait cursor returned an invalid tombstone"
                )
            if generation != str(wait.generation).encode() or state != b"active":
                return
            message_sequence = _integer(message)
            control_sequence = _integer(control)
            if (
                message_sequence > wait.after.message_sequence
                or control_sequence > wait.after.control_sequence
            ):
                return
            if self._reader is None:
                self._failure = None
                self._reader = asyncio.create_task(
                    self._run_reader(),
                    name="tinkerfin-messaging-redis-notifications",
                )
            if self._failure is not None:
                raise self._failure
            timeout = (
                5.0 if wait.timeout_seconds is None else min(5.0, wait.timeout_seconds)
            )
            await _wait_for_hint(ready, timeout)
            if self._failure is not None:
                raise self._failure
        except BaseException as error:
            primary = error
            raise
        finally:
            group.users -= 1
            if not group.users:
                del self._groups[key]
            if not self._groups and self._reader is not None:
                reader = self._reader
                self._closing = asyncio.get_running_loop().create_future()
                if not reader.done() and not reader.cancelling():
                    reader.cancel()
                try:
                    await self._join_reader(reader)
                except BaseException as error:
                    if primary is None:
                        raise
                    if error is not primary:
                        for note in tuple(getattr(error, "__notes__", ())):
                            primary.add_note(note)
                    raise select_failure(primary, error)

    async def _join_reader(self, reader: asyncio.Task[TaskOutcome[None]]) -> None:
        try:
            await join_owned_task(reader)
        except BaseException as error:
            outcome = None if reader.cancelled() else reader.result()
            if isinstance(outcome, BaseException) and outcome is not error:
                for note in tuple(getattr(outcome, "__notes__", ())):
                    error.add_note(note)
            current = asyncio.current_task()
            if (
                not isinstance(error, asyncio.CancelledError)
                or not reader.cancelling()
                or error.__cause__ is not None
                or (current is not None and current.cancelling())
            ):
                raise
        finally:
            if reader.done() and self._reader is reader:
                self._reader = None
                closing = self._closing
                self._closing = None
                self._failure = None
                if closing is not None:
                    closing.set_result(None)

    async def _run_reader(self) -> TaskOutcome[None]:
        return await capture(self._read())

    async def _read(self) -> None:
        try:
            while self._groups:
                response = await _read_notifications(self._backend, after=self._cursor)
                if len(response) > 1:
                    raise _redis_protocol_error(
                        "Redis notifications returned multiple streams"
                    )
                for stream, entries in response:
                    if (
                        stream != self._backend._notifications_key.encode()
                        or len(entries) > 128
                    ):
                        raise _redis_protocol_error(
                            "Redis notifications exceeded the bounded stream response"
                        )
                    for identifier, fields in entries:
                        if not identifier.endswith(b"-0"):
                            raise _redis_protocol_error(
                                "Redis notification ID is not a canonical sequence"
                            )
                        sequence = _integer(identifier[:-2], positive=True)
                        if sequence <= self._cursor:
                            raise _redis_protocol_error(
                                "Redis notification sequence did not advance"
                            )
                        if set(fields) != {
                            b"scope",
                            b"generation",
                            b"message",
                            b"control",
                        }:
                            raise _redis_protocol_error(
                                "Redis notification fields are invalid"
                            )
                        scope = fields[b"scope"]
                        if (
                            not isinstance(scope, bytes)
                            or len(scope) != 40
                            or any(byte not in b"0123456789abcdef" for byte in scope)
                        ):
                            raise _redis_protocol_error(
                                "Redis notification scope is invalid"
                            )
                        generation = _integer(fields[b"generation"], positive=True)
                        _integer(fields[b"message"])
                        _integer(fields[b"control"])
                        if sequence != self._cursor + 1:
                            # A trimmed hint is not a missing committed message. Reload
                            # every active view instead of fabricating replay evidence.
                            for group in self._groups.values():
                                group.wake()
                        self._cursor = sequence
                        group = self._groups.get((scope, generation))
                        if group is not None:
                            group.wake()
        except BaseException as error:
            self._failure = error
            for group in self._groups.values():
                group.wake()
            raise


def _integer(value: bytes, *, positive: bool = False) -> int:
    if not isinstance(value, bytes) or not value.isdigit() or len(value) > 19:
        raise _redis_protocol_error(
            "Redis notification cursor is not a bounded integer"
        )
    parsed = int(value)
    if (
        str(parsed).encode() != value
        or parsed > 2**63 - 1
        or (positive and parsed == 0)
    ):
        raise _redis_protocol_error("Redis notification cursor is not canonical")
    return parsed


async def _wait_for_hint(ready: asyncio.Future[None], timeout: float) -> None:
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            await asyncio.shield(ready)
    except TimeoutError:
        if not deadline.expired():
            raise


def _validate_wait(wait: MessagingChangeWait) -> None:
    """Reject invalid deadlines before registering any owned work."""

    timeout = wait.timeout_seconds
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number or None")
