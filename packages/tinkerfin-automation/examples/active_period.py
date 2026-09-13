"""Run a local task manually and query its bounded schedule and history."""

import asyncio
from datetime import UTC, datetime, timedelta

from tinkerfin_automation import (
    AutomationEngine,
    AutomationService,
    ExecutionFilter,
    ExecutionRequest,
    ExecutionStatus,
    FunctionTarget,
    IntervalSchedule,
    TaskFilter,
)


async def summarize(request: ExecutionRequest) -> dict[str, str]:
    """Return a local result without network or model dependencies."""
    return {"summary": f"Summary for {request.execution.owner_id}"}


async def main() -> None:
    """Own the worker and service for one complete application session."""
    owner_id = "example-user"  # A host obtains this from trusted authentication.
    now = datetime.now(UTC)
    async with AutomationService(namespace="example") as service:
        async with AutomationEngine(
            service, targets={"summary": FunctionTarget(summarize)}
        ) as worker:
            task = await service.create_task(
                owner_id=owner_id,
                name="Daily summary",
                target="summary",
                input={},
                schedule=IntervalSchedule(
                    every_seconds=86400,
                    start_at=now + timedelta(days=1),
                    active_from=now + timedelta(days=1),
                    active_until=now + timedelta(days=8),
                ),
                request_id="create-summary",
            )
            await worker.check_ready()
            await service.run_task_now(
                owner_id=owner_id,
                task_id=task.task_id,
                expected_revision=task.revision,
                request_id="manual-summary",
            )
            await worker.wait_until_idle()
            tasks = await service.list_tasks(
                owner_id=owner_id, filters=TaskFilter(name_contains="SUMMARY")
            )
            filters = ExecutionFilter(
                name_contains="summary",
                statuses=(ExecutionStatus.SUCCEEDED,),
                queued_from=now,
            )
            runs = await service.list_executions(owner_id=owner_id, filters=filters)
            counts = await service.summarize_executions(
                owner_id=owner_id, filters=filters
            )
            assert len(tasks.items) == len(runs.items) == 1
            assert counts[ExecutionStatus.SUCCEEDED] == 1
            print(runs.items[0].result, counts)


if __name__ == "__main__":
    asyncio.run(main())
