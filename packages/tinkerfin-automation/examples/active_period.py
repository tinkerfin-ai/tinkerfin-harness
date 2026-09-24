"""Run a local task manually and query its bounded schedule and history."""

import asyncio
from datetime import UTC, datetime, timedelta

from tinkerfin_automation import (
    Automation,
    ExecutionFilter,
    ExecutionRequest,
    ExecutionStatus,
    Schedule,
    TaskFilter,
)


async def summarize(request: ExecutionRequest) -> dict[str, str]:
    """Return a local result without network or model dependencies."""
    return {"summary": f"Summary for {request.execution.owner_id}"}


async def main() -> None:
    """Own the worker and service for one complete application session."""
    owner_id = "example-user"  # A host obtains this from trusted authentication.
    now = datetime.now(UTC)
    automation = Automation(namespace="example")
    automation.target("summary", summarize)
    async with automation.worker() as worker:
        owner = automation.for_owner(owner_id)
        task = await owner.create_task(
            name="Daily summary",
            target="summary",
            input={},
            schedule=Schedule.every(
                days=1,
                start_at=now + timedelta(days=1),
                active_from=now + timedelta(days=1),
                active_until=now + timedelta(days=8),
            ),
            request_id="create-summary",
        )
        await worker.check_ready()
        run = await task.run(request_id="manual-summary")
        await run.wait()
        tasks = await owner.list_tasks(filters=TaskFilter(name_contains="SUMMARY"))
        filters = ExecutionFilter(
            name_contains="summary",
            statuses=(ExecutionStatus.SUCCEEDED,),
            queued_from=now,
        )
        runs = await owner.list_runs(filters=filters)
        counts = await owner.summarize_runs(filters=filters)
        assert len(tasks.items) == len(runs.items) == 1
        assert counts[ExecutionStatus.SUCCEEDED] == 1
        print(runs.items[0].result, counts)


if __name__ == "__main__":
    asyncio.run(main())
