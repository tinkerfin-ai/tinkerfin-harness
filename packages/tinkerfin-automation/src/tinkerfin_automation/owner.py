"""Task operations bound to one trusted ownership scope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, Self, TypeVar

from pydantic import JsonValue

from tinkerfin_contracts.identity import validate_namespace

from .facade import Automation
from .handles import RunHandle, TaskHandle
from .models import AttentionResolution, ExecutionStatus, TaskStatus
from .policies import ExecutionLimits, MisfirePolicy
from .queries import ExecutionFilter, TaskFilter
from .schedules import ScheduleSpec

_ItemT = TypeVar("_ItemT")


@dataclass(frozen=True, slots=True)
class HandlePage(Generic[_ItemT]):
    """One query page; reuse its cursor only with the same owner and filters."""

    items: tuple[_ItemT, ...]
    next_cursor: str | None


class AutomationOwner:
    """Reuse an Automation lifecycle with an immutable authenticated subject scope.

    Obtain this view through Automation.for_owner(). It creates no resources and
    does not authenticate the owner; the host must authenticate and authorize first.
    """

    _automation: Automation
    _owner_id: str
    _execution_namespace: str

    def __init__(self) -> None:
        """Require Automation.for_owner() to bind an ownership scope."""
        raise TypeError("Use Automation.for_owner()")

    @classmethod
    def _bind(
        cls, automation: Automation, owner_id: str, execution_namespace: str | None
    ) -> Self:
        automation._service._validate_owner(owner_id)
        owner = cls.__new__(cls)
        owner._automation = automation
        owner._owner_id = owner_id
        owner._execution_namespace = validate_namespace(
            automation._service.namespace
            if execution_namespace is None
            else execution_namespace
        )
        return owner

    @property
    def namespace(self) -> str:
        """Return the namespace bound to the shared Automation lifecycle."""
        return self._automation._service.namespace

    @property
    def owner_id(self) -> str:
        """Return the host identity bound to every operation on this view."""
        return self._owner_id

    @property
    def execution_namespace(self) -> str:
        """Return the Runtime namespace selected for new tasks and taskless work."""
        return self._execution_namespace

    async def create_task(
        self,
        *,
        name: str,
        target: str,
        schedule: ScheduleSpec,
        input: Mapping[str, JsonValue] | None = None,
        misfire_policy: MisfirePolicy | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
    ) -> TaskHandle:
        """Save an enabled task inside a worker lifecycle.

        Args:
            name: User-visible task name.
            target: Host-authorized registered target.
            schedule: Validated schedule with a stable explicit time anchor.
            input: Secret-free JSON to snapshot for each execution.
            misfire_policy: Optional policy for missed occurrences.
            limits: Optional bounded execution and queue policy.
            request_id: Stable key for replaying this exact creation command.

        Returns:
            A handle containing the saved task snapshot.

        Raises:
            AutomationLifecycleError: No active local worker manages schedules.
            InvalidScheduleError: The schedule has no future occurrence.
            RequestConflictError: The request key describes different input.
        """
        async with self._automation._operation(schedule_write=True):
            snapshot = await self._automation._service.create_task(
                owner_id=self.owner_id,
                execution_namespace=self.execution_namespace,
                name=name,
                target=target,
                schedule=schedule,
                input=input,
                misfire_policy=misfire_policy,
                limits=limits,
                request_id=request_id,
            )
            return TaskHandle._from_snapshot(self, snapshot)

    async def task(self, task_id: str) -> TaskHandle:
        """Read an owned task or raise TaskNotFoundError without revealing other owners."""
        async with self._automation._operation():
            return TaskHandle._from_snapshot(
                self,
                await self._automation._service.get_task(
                    owner_id=self.owner_id, task_id=task_id
                ),
            )

    async def delete_task(
        self, task_id: str, *, expected_revision: int, request_id: str | None = None
    ) -> None:
        """Delete a task by saved command, including replay after its definition is gone.

        Args:
            task_id: Task identity from a previously observed owned task.
            expected_revision: Revision saved with the original command, at least 1.
            request_id: Original command key; reuse unchanged on a network retry.

        Raises:
            TaskConflictError: The current task no longer has the expected revision.
            RequestConflictError: The key was reused for a different command.
            TaskNotFoundError: No task or matching deletion receipt exists.
            AutomationLifecycleError: No active worker manages schedules.
        """
        from .handles import _revision

        revision = _revision(expected_revision)
        async with self._automation._operation(schedule_write=True):
            await self._automation._service.delete_task(
                owner_id=self.owner_id,
                task_id=task_id,
                expected_revision=revision,
                request_id=request_id,
            )

    async def run(
        self,
        target: str,
        *,
        input: Mapping[str, JsonValue] | None = None,
        limits: ExecutionLimits | None = None,
        request_id: str | None = None,
    ) -> RunHandle:
        """Submit immediate work without creating a task or waiting for its outcome."""
        async with self._automation._operation():
            return RunHandle._from_snapshot(
                self,
                await self._automation._service.execute_once(
                    owner_id=self.owner_id,
                    execution_namespace=self.execution_namespace,
                    target=target,
                    input=input,
                    limits=limits,
                    request_id=request_id,
                ),
            )

    async def get_run(self, execution_id: str) -> RunHandle:
        """Read one owned execution by execution ID, not by its Runtime run ID."""
        async with self._automation._operation():
            return RunHandle._from_snapshot(
                self,
                await self._automation._service.get_execution(
                    owner_id=self.owner_id, execution_id=execution_id
                ),
            )

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filters: TaskFilter | None = None,
    ) -> HandlePage[TaskHandle]:
        """Read up to 100 tasks with no additional per-item queries."""
        async with self._automation._operation():
            page = await self._automation._service.list_tasks(
                owner_id=self.owner_id, limit=limit, cursor=cursor, filters=filters
            )
            return HandlePage(
                tuple(TaskHandle._from_snapshot(self, item) for item in page.items),
                page.next_cursor,
            )

    async def list_runs(
        self,
        *,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        filters: ExecutionFilter | None = None,
    ) -> HandlePage[RunHandle]:
        """Read up to 100 execution snapshots, optionally including a deleted task's history."""
        async with self._automation._operation():
            page = await self._automation._service.list_executions(
                owner_id=self.owner_id,
                task_id=task_id,
                limit=limit,
                cursor=cursor,
                filters=filters,
            )
            return HandlePage(
                tuple(RunHandle._from_snapshot(self, item) for item in page.items),
                page.next_cursor,
            )

    async def summarize_tasks(
        self, *, filters: TaskFilter | None = None
    ) -> dict[TaskStatus, int]:
        """Count all matching tasks, independently of the current page."""
        async with self._automation._operation():
            return await self._automation._service.summarize_tasks(
                owner_id=self.owner_id, filters=filters
            )

    async def summarize_runs(
        self, *, filters: ExecutionFilter | None = None
    ) -> dict[ExecutionStatus, int]:
        """Count all matching executions, independently of the current page."""
        async with self._automation._operation():
            return await self._automation._service.summarize_executions(
                owner_id=self.owner_id, filters=filters
            )

    async def resolve_run(
        self,
        execution_id: str,
        *,
        resolution: AttentionResolution,
        reason: str,
        request_id: str,
    ) -> RunHandle:
        """Record a host-verified outcome after separate authorization and audit.

        This settles uncertain external work; it does not approve or resume a graph.
        The host must verify the external outcome before selecting a resolution.

        Args:
            execution_id: Owned execution awaiting an explicit outcome.
            resolution: Independently verified terminal outcome.
            reason: Safe audit explanation of the verification.
            request_id: Stable key for this exact settlement command.

        Returns:
            A handle containing the recorded outcome.

        Raises:
            ResolutionNotAllowedError: The execution is not awaiting resolution.
            RequestConflictError: The key was reused for another command.
        """
        async with self._automation._operation():
            return RunHandle._from_snapshot(
                self,
                await self._automation._service.resolve_execution(
                    owner_id=self.owner_id,
                    execution_id=execution_id,
                    resolution=resolution,
                    reason=reason,
                    request_id=request_id,
                ),
            )


__all__ = ["AutomationOwner", "HandlePage"]
