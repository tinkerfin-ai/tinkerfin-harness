"""Explicitly refreshed task and execution snapshots with bound ownership."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Self

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity

from ._tasks import (
    capture,
    join_owned_task,
    select_failure,
    stop_owned_tasks,
    task_result,
)
from .errors import (
    AutomationLifecycleError,
    AutomationStoreProtocolError,
    AutomationWaitTimeout,
)
from .models import AutomationExecution, AutomationTask, ExecutionStatus, TaskStatus
from .policies import ExecutionLimits, MisfirePolicy
from .schedules import ScheduleSpec

if TYPE_CHECKING:
    from .owner import AutomationOwner


def _revision(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected_revision must be an integer of at least 1")
    return value


def _task_copy(value: AutomationTask) -> AutomationTask:
    return replace(
        value,
        input=deepcopy(dict(value.input)),
        schedule=value.schedule.model_copy(deep=True),
    )


def _run_copy(value: AutomationExecution) -> AutomationExecution:
    return replace(
        value, input=deepcopy(dict(value.input)), result=deepcopy(value.result)
    )


class TaskHandle:
    """Operate on one observed task without automatically refreshing its revision.

    Obtain handles from AutomationOwner. Each handle owns an independent snapshot;
    a failed command leaves that snapshot unchanged. Explicit expected_revision
    values preserve remote form intent and must be reused with the original request
    key on replay. Different handles coordinate through the Store's atomic checks.
    """

    _owner: AutomationOwner
    _snapshot: AutomationTask
    _lock: asyncio.Lock
    _deleted: bool

    def __init__(self) -> None:
        """Require an owner operation to obtain a verified, resource-bound handle."""
        raise TypeError("Obtain handles through AutomationOwner operations")

    @classmethod
    def _from_snapshot(cls, owner: AutomationOwner, snapshot: AutomationTask) -> Self:
        if not isinstance(snapshot, AutomationTask):
            raise TypeError("snapshot must be an AutomationTask")
        if (snapshot.namespace, snapshot.owner_id) != (owner.namespace, owner.owner_id):
            raise AutomationStoreProtocolError("Task snapshot does not match its owner")
        handle = cls.__new__(cls)
        handle._owner = owner
        handle._snapshot = _task_copy(snapshot)
        handle._lock = asyncio.Lock()
        handle._deleted = False
        return handle

    def __repr__(self) -> str:
        """Describe identity and revision without exposing saved inputs."""
        return f"TaskHandle(id={self.id!r}, revision={self.revision}, status={self.status.value!r})"

    @property
    def id(self) -> str:
        """Return the persisted task identity without I/O."""
        return self._snapshot.task_id

    @property
    def revision(self) -> int:
        """Return the last revision observed by this handle."""
        return self._snapshot.revision

    @property
    def status(self) -> TaskStatus:
        """Return the last observed scheduling status."""
        return self._snapshot.status

    @property
    def next_run_at(self) -> datetime | None:
        """Return the last observed next scheduled occurrence in UTC."""
        return self._snapshot.next_run_at

    @property
    def snapshot(self) -> AutomationTask:
        """Return a detached value whose nested input cannot mutate this handle."""
        return _task_copy(self._snapshot)

    @property
    def is_deleted(self) -> bool:
        """Whether this handle has successfully deleted its task definition."""
        return self._deleted

    async def refresh(self) -> Self:
        """Read the current task snapshot, preserving the old snapshot on failure."""
        return await self._change(
            partial(
                self._owner._automation._service.get_task,
                owner_id=self._owner.owner_id,
                task_id=self.id,
            ),
            schedule_write=False,
        )

    async def update(
        self,
        *,
        expected_revision: int | None = None,
        name: str | None = None,
        target: str | None = None,
        schedule: ScheduleSpec | None = None,
        input: Mapping[str, JsonValue] | None = None,
        misfire_policy: MisfirePolicy | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
    ) -> Self:
        """Update selected fields using the observed or explicitly supplied revision.

        Args:
            expected_revision: Original observed revision; None uses this handle's.
            name: Replacement name; None leaves it unchanged.
            target: Replacement host-authorized target; None leaves it unchanged.
            schedule: Complete replacement schedule, including active bounds.
            input: Replacement JSON; {} clears input and None leaves it unchanged.
            misfire_policy: Replacement missed-occurrence policy, if supplied.
            limits: Replacement execution policy, if supplied.
            request_id: Stable key paired with the original parameters and revision.

        Returns:
            This handle after updating its snapshot.

        Raises:
            TaskConflictError: The requested revision is no longer current.
            RequestConflictError: The request key identifies different parameters.
        """
        revision = _revision(
            self.revision if expected_revision is None else expected_revision
        )
        return await self._change(
            partial(
                self._owner._automation._service.update_task,
                owner_id=self._owner.owner_id,
                task_id=self.id,
                expected_revision=revision,
                name=name,
                target=target,
                schedule=schedule,
                input=None if input is None else deepcopy(dict(input)),
                misfire_policy=misfire_policy,
                limits=limits,
                request_id=request_id,
            )
        )

    async def pause(
        self, *, expected_revision: int | None = None, request_id: str | None = None
    ) -> Self:
        """Stop future wakeups without cancelling already admitted executions."""
        revision = _revision(
            self.revision if expected_revision is None else expected_revision
        )
        return await self._change(
            partial(
                self._owner._automation._service.pause_task,
                owner_id=self._owner.owner_id,
                task_id=self.id,
                expected_revision=revision,
                request_id=request_id,
            )
        )

    async def enable(
        self, *, expected_revision: int | None = None, request_id: str | None = None
    ) -> Self:
        """Resume future wakeups from Store time without paused-period catch-up."""
        revision = _revision(
            self.revision if expected_revision is None else expected_revision
        )
        return await self._change(
            partial(
                self._owner._automation._service.enable_task,
                owner_id=self._owner.owner_id,
                task_id=self.id,
                expected_revision=revision,
                request_id=request_id,
            )
        )

    async def delete(
        self, *, expected_revision: int | None = None, request_id: str | None = None
    ) -> None:
        """Delete the definition, retaining history and this handle's last snapshot.

        Replays still reach the Store. is_deleted never fabricates idempotent success.

        Args:
            expected_revision: Original command revision; None uses this snapshot.
            request_id: Stable deletion key reused with the original revision.

        Raises:
            TaskNotFoundError: No task or matching deletion receipt exists.
            TaskConflictError: The task changed after the requested revision.
            RequestConflictError: The key identifies a different command.
            AutomationLifecycleError: No active worker manages schedules.
        """
        revision = _revision(
            self.revision if expected_revision is None else expected_revision
        )
        async with self._lock:
            await self._owner.delete_task(
                self.id, expected_revision=revision, request_id=request_id
            )
            self._deleted = True

    async def run(
        self, *, expected_revision: int | None = None, request_id: str | None = None
    ) -> RunHandle:
        """Submit an additional execution without altering the saved schedule."""
        revision = _revision(
            self.revision if expected_revision is None else expected_revision
        )
        async with self._owner._automation._operation(), self._lock:
            snapshot = await self._owner._automation._service.run_task_now(
                owner_id=self._owner.owner_id,
                task_id=self.id,
                expected_revision=revision,
                request_id=request_id,
            )
            return RunHandle._from_snapshot(self._owner, snapshot)

    async def _change(
        self,
        change: Callable[[], Awaitable[AutomationTask]],
        *,
        schedule_write: bool = True,
    ) -> Self:
        # Revisions are captured by the calling coroutine before this lock. Local
        # serialization must not silently rebase another already-started command.
        async with (
            self._owner._automation._operation(schedule_write=schedule_write),
            self._lock,
        ):
            snapshot = await change()
            if (snapshot.task_id, snapshot.namespace, snapshot.owner_id) != (
                self.id,
                self._owner.namespace,
                self._owner.owner_id,
            ):
                raise AutomationStoreProtocolError(
                    "Task operation returned another identity"
                )
            self._snapshot = _task_copy(snapshot)
            return self


class RunHandle:
    """Observe and control one submitted execution independently of other work.

    id is the Automation execution ID; identity.run_id belongs to the Runtime.
    Results may be None even after success, including TinkerFin Agent targets.
    Obtain handles from an owner or task operation; snapshots never auto-refresh.
    """

    _owner: AutomationOwner
    _snapshot: AutomationExecution
    _lock: asyncio.Lock

    def __init__(self) -> None:
        """Require an owner operation to obtain a verified, resource-bound handle."""
        raise TypeError("Obtain handles through AutomationOwner operations")

    @classmethod
    def _from_snapshot(
        cls, owner: AutomationOwner, snapshot: AutomationExecution
    ) -> Self:
        if not isinstance(snapshot, AutomationExecution):
            raise TypeError("snapshot must be an AutomationExecution")
        if (snapshot.namespace, snapshot.owner_id) != (owner.namespace, owner.owner_id):
            raise AutomationStoreProtocolError(
                "Execution snapshot does not match its owner"
            )
        handle = cls.__new__(cls)
        handle._owner = owner
        handle._snapshot = _run_copy(snapshot)
        handle._lock = asyncio.Lock()
        return handle

    def __repr__(self) -> str:
        """Describe execution state without exposing inputs, results, or tokens."""
        return f"RunHandle(id={self.id!r}, status={self.status.value!r})"

    @property
    def id(self) -> str:
        """Return the Automation execution ID."""
        return self._snapshot.execution_id

    @property
    def identity(self) -> RunIdentity:
        """Return the execution's immutable Runtime identity."""
        return self._snapshot.identity

    @property
    def status(self) -> ExecutionStatus:
        """Return the last observed execution status without I/O."""
        return self._snapshot.status

    @property
    def succeeded(self) -> bool:
        """Whether the last observed state is succeeded."""
        return self.status is ExecutionStatus.SUCCEEDED

    @property
    def result(self) -> JsonValue | None:
        """Return a detached JSON result, which may be None; never wait implicitly."""
        return deepcopy(self._snapshot.result)

    @property
    def snapshot(self) -> AutomationExecution:
        """Return a detached snapshot including safe failure information."""
        return _run_copy(self._snapshot)

    async def refresh(self) -> Self:
        """Read this execution's current state, preserving the snapshot on failure."""
        return await self._change(
            partial(
                self._owner._automation._service.get_execution,
                owner_id=self._owner.owner_id,
                execution_id=self.id,
            )
        )

    async def cancel(self, *, request_id: str | None = None) -> Self:
        """Request cancellation without treating cancel_requested as cancelled."""
        return await self._change(
            partial(
                self._owner._automation._service.cancel_execution,
                owner_id=self._owner.owner_id,
                execution_id=self.id,
                request_id=request_id,
            )
        )

    async def retry(self, *, request_id: str | None = None) -> RunHandle:
        """Submit a new attempt for failed/timed_out/cancelled work; retain this ID."""
        async with self._owner._automation._operation(), self._lock:
            return RunHandle._from_snapshot(
                self._owner,
                await self._owner._automation._service.retry_execution(
                    owner_id=self._owner.owner_id,
                    execution_id=self.id,
                    request_id=request_id,
                ),
            )

    async def wait(self, *, timeout: float = 30.0, poll_interval: float = 1.0) -> Self:
        """Observe this execution until finished or requiring external attention.

        Args:
            timeout: Finite positive seconds covering locks, reads and intervals.
            poll_interval: Finite positive seconds between Store observations.

        Returns:
            This handle with a terminal, interrupted, or needs_attention snapshot.
            Business failure is a returned state, not a raised target exception.

        Raises:
            AutomationWaitTimeout: Only this observer's deadline expired.
            AutomationLifecycleError: The owning Automation is closing or closed.
            asyncio.CancelledError: The caller cancelled observation, not execution.
            AutomationStoreError: A Store observation failed.
            ValueError: A duration is not a finite positive number.
        """
        for duration in (timeout, poll_interval):
            if (
                isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError("Wait durations must be finite positive seconds")
        app = self._owner._automation
        async with app._operation():
            deadline = asyncio.create_task(
                capture(
                    app._clock.wait_until(app._clock.now() + timedelta(seconds=timeout))
                ),
                name="tinkerfin-automation-wait-deadline",
            )
            closing = asyncio.create_task(
                capture(app._closing.wait()), name="tinkerfin-automation-wait-close"
            )
            observation = asyncio.create_task(
                capture(self._poll(poll_interval)),
                name="tinkerfin-automation-wait-observe",
            )
            children = (observation, deadline, closing)
            failure: BaseException | None = None
            try:
                done, _ = await asyncio.wait(
                    children, return_when=asyncio.FIRST_COMPLETED
                )
                if observation in done:
                    task_result(observation)
                elif closing in done:
                    task_result(closing)
                    raise AutomationLifecycleError(
                        "Automation closed while observing execution"
                    )
                else:
                    task_result(deadline)
                    raise AutomationWaitTimeout(
                        "Execution observation timed out",
                        context={"execution_id": self.id, "timeout": timeout},
                    )
            except BaseException as error:  # noqa: BLE001 - settle all observer children before propagating
                failure = error
            finally:
                cleanup = asyncio.create_task(
                    capture(stop_owned_tasks(children)),
                    name="tinkerfin-automation-wait-cleanup",
                )
                try:
                    await join_owned_task(cleanup)
                except BaseException as error:  # noqa: BLE001 - retain cleanup and cancellation
                    failure = (
                        error if failure is None else select_failure(failure, error)
                    )
            if failure is not None:
                raise failure
            return self

    async def _poll(self, interval: float) -> Self:
        while True:
            await self.refresh()
            if self.status.is_terminal or self.status in {
                ExecutionStatus.INTERRUPTED,
                ExecutionStatus.NEEDS_ATTENTION,
            }:
                return self
            # No handle lock spans the interval; cancellation from another coroutine
            # can update this same execution while the observer is sleeping.
            await self._owner._automation._clock.wait_until(
                self._owner._automation._clock.now() + timedelta(seconds=interval)
            )

    async def _change(
        self, change: Callable[[], Awaitable[AutomationExecution]]
    ) -> Self:
        async with self._owner._automation._operation(), self._lock:
            snapshot = await change()
            if (snapshot.execution_id, snapshot.namespace, snapshot.owner_id) != (
                self.id,
                self._owner.namespace,
                self._owner.owner_id,
            ):
                raise AutomationStoreProtocolError(
                    "Execution operation returned another identity"
                )
            self._snapshot = _run_copy(snapshot)
            return self


__all__ = ["RunHandle", "TaskHandle"]
