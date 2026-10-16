"""Replaceable task wakeup contracts and the default in-memory scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypeAlias

from ._tasks import (
    TaskOutcome,
    capture,
    capture_call,
    join_owned_task,
    select_failure,
    stop_owned_tasks,
    task_result,
)
from .clock import AutomationClock, SystemClock
from .errors import AutomationLifecycleError, AutomationSchedulerError

TaskDue: TypeAlias = Callable[[str], Awaitable[None]]


class AutomationScheduler(Protocol):
    """Maintain task wakeups while Automation storage remains authoritative."""

    async def start(self, on_task_due: TaskDue) -> None:
        """Start delivering task IDs to one asynchronous callback."""

    async def schedule_task(self, task_id: str, run_at: datetime) -> None:
        """Create or replace the next wakeup for one task."""

    async def remove_task(self, task_id: str) -> None:
        """Remove a task wakeup if one exists."""

    async def close(self) -> None:
        """Stop future wakeups and settle scheduler-owned work."""


class _SchedulerCall:
    def __init__(self, scheduler: AutomationScheduler) -> None:
        self.scheduler = scheduler
        self.active = True


_scheduler_calls: ContextVar[tuple[_SchedulerCall, ...]] = ContextVar(
    "tinkerfin_automation_scheduler_calls", default=()
)


@contextmanager
def _callback_scope(scheduler: AutomationScheduler) -> Generator[None, None, None]:
    call = _SchedulerCall(scheduler)
    ancestors = tuple(item for item in _scheduler_calls.get() if item.active)
    token = _scheduler_calls.set((*ancestors, call))
    try:
        yield
    finally:
        # A detached child may retain this context after the callback returned.
        # Only the actual in-progress callback forbids waiting for its own owner.
        call.active = False
        _scheduler_calls.reset(token)


def _require_external_close(scheduler: AutomationScheduler) -> None:
    if any(
        call.active and call.scheduler is scheduler for call in _scheduler_calls.get()
    ):
        raise AutomationLifecycleError(
            "Close the scheduler or Service after its callback returns"
        )


class MemoryScheduler:
    """Run task wakeups in one process with no durable scheduler state.

    The scheduler owns exactly one loop task. Due callbacks are awaited serially and
    should only enqueue durable work; target execution belongs to AutomationEngine.
    """

    def __init__(self, *, clock: AutomationClock | None = None) -> None:
        """Create a stopped scheduler using a system clock by default."""

        self._clock = clock or SystemClock()
        self._jobs: dict[str, datetime] = {}
        self._changed = asyncio.Event()
        self._on_task_due: TaskDue | None = None
        self._loop_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self, on_task_due: TaskDue) -> None:
        """Start one owned wakeup loop.

        Raises:
            AutomationLifecycleError: The scheduler is closed or already started.
        """

        if self._closed:
            raise AutomationLifecycleError("Memory scheduler is closed")
        if self._loop_task is not None:
            raise AutomationLifecycleError("Memory scheduler is already started")
        if not callable(on_task_due):
            raise TypeError("on_task_due must be callable")
        self._on_task_due = on_task_due
        self._loop_task = asyncio.create_task(
            capture(self._run_loop()), name="tinkerfin-automation-memory-scheduler"
        )

    async def schedule_task(self, task_id: str, run_at: datetime) -> None:
        """Create or replace one in-memory wakeup."""

        if self._closed:
            raise AutomationLifecycleError("Memory scheduler is closed")
        if not task_id or task_id != task_id.strip():
            raise ValueError("task_id must be a non-empty canonical value")
        if run_at.tzinfo is None or run_at.utcoffset() is None:
            raise ValueError("run_at must be timezone-aware")
        async with self._lock:
            self._jobs[task_id] = run_at.astimezone(UTC)
        self._changed.set()

    async def remove_task(self, task_id: str) -> None:
        """Remove an in-memory wakeup without changing the task definition."""

        async with self._lock:
            self._jobs.pop(task_id, None)
        self._changed.set()

    async def dispatch_due(self) -> int:
        """Deliver all currently due task IDs for deterministic host-driven tests."""

        callback = self._on_task_due
        if callback is None:
            raise AutomationLifecycleError("Memory scheduler is not started")
        now = self._clock.now()
        async with self._lock:
            due = sorted(
                (
                    (run_at, task_id)
                    for task_id, run_at in self._jobs.items()
                    if run_at <= now
                ),
                key=lambda item: (item[0], item[1]),
            )
            for _, task_id in due:
                self._jobs.pop(task_id, None)
        for _, task_id in due:
            with _callback_scope(self):
                await callback(task_id)
        return len(due)

    async def close(self) -> None:
        """Join one shutdown without letting cancelled waiters interrupt loop cleanup.

        The scheduler cancels its own loop once. Callers receive their cancellation
        only after that loop and its children settle. Repeated calls retain any
        independent loop or cleanup failure.

        Raises:
            AutomationLifecycleError: Called from an active due or clock callback.
        """

        _require_external_close(self)
        if self._close_task is None:
            self._closed = True
            self._changed.set()
            self._close_task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-automation-scheduler-close"
            )
        await join_owned_task(self._close_task)

    async def _close_once(self) -> None:
        try:
            if self._loop_task is not None:
                await stop_owned_tasks((self._loop_task,))
        except AutomationSchedulerError:
            raise
        except Exception as error:
            raise AutomationSchedulerError(
                "Memory scheduler shutdown failed", cause=error
            ) from error
        finally:
            self._on_task_due = None

    async def _stop_waiters(
        self, children: Iterable[asyncio.Task[TaskOutcome[object]]]
    ) -> None:
        # Closing the loop while it replaces a timer must not cancel that timer's
        # already-running finally a second time. The loop joins this one settlement.
        task = asyncio.create_task(
            capture(stop_owned_tasks(children)),
            name="tinkerfin-automation-scheduler-waiter-close",
        )
        await join_owned_task(task)

    async def _wait_until(self, when: datetime) -> None:
        with _callback_scope(self):
            await self._clock.wait_until(when)

    async def _run_loop(self) -> None:
        wait_task: asyncio.Task[TaskOutcome[None]] | None = None
        change_task: asyncio.Task[TaskOutcome[bool]] | None = None
        failure: BaseException | None = None
        try:
            while not self._closed:
                await self.dispatch_due()
                async with self._lock:
                    next_run_at = min(self._jobs.values(), default=None)
                self._changed.clear()
                if next_run_at is None:
                    await self._changed.wait()
                    continue
                wait_task = asyncio.create_task(
                    capture_call(partial(self._wait_until, next_run_at)),
                    name="tinkerfin-automation-memory-scheduler-clock",
                )
                change_task = asyncio.create_task(
                    capture(self._changed.wait()),
                    name="tinkerfin-automation-memory-scheduler-change",
                )
                done, pending = await asyncio.wait(
                    {wait_task, change_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                try:
                    await self._stop_waiters(pending)
                finally:
                    # These outcomes were consumed, including deliberate timer
                    # cancellation; do not reinterpret them as a new failure below.
                    if wait_task in pending:
                        wait_task = None
                    if change_task in pending:
                        change_task = None
                for task in done:
                    task_result(task)
                wait_task = None
                change_task = None
        except BaseException as error:  # noqa: BLE001 - preserve loop control while settling both children
            failure = error
        try:
            await self._stop_waiters(
                task for task in (wait_task, change_task) if task is not None
            )
        except BaseException as error:  # noqa: BLE001 - retain loop and cleanup failures together
            failure = error if failure is None else select_failure(failure, error)
        if failure is not None:
            if isinstance(failure, Exception):
                raise AutomationSchedulerError(
                    "Memory scheduler wakeup loop failed", cause=failure
                ) from failure
            raise failure


__all__ = ["AutomationScheduler", "MemoryScheduler", "TaskDue"]
