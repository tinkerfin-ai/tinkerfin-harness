"""Saved Runtime scopes survive scheduling, owner rebinding, and retries."""

from datetime import timedelta

import pytest

from tinkerfin_automation import Automation, RequestConflictError, Schedule
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_automation.targets import ExecutionRequest


async def test_one_worker_keeps_runtime_scopes_and_fresh_thread_identities(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)
    observed: list[tuple[str, str]] = []

    @app.target("report")
    async def report(request: ExecutionRequest) -> None:
        observed.append(
            (request.execution.owner_id, request.execution.identity.namespace)
        )

    async with app.worker() as worker:
        owner = app.for_owner("alice", execution_namespace="alice-runtime")
        task = await owner.create_task(
            name="Report",
            target="report",
            schedule=Schedule.every(minutes=1, start_at=clock.now()),
        )
        assert task.snapshot.execution_namespace == owner.execution_namespace
        rebound = app.for_owner("alice", execution_namespace="unrelated-runtime")
        same_task = await rebound.task(task.id)
        await same_task.update(name="Renamed")
        first = await same_task.run()
        second = await same_task.run()
        bob = await app.for_owner("bob", execution_namespace="bob-runtime").run(
            "report"
        )
        await worker.wait_until_idle()
        assert first.identity.namespace == second.identity.namespace == "alice-runtime"
        assert first.identity.thread_id != second.identity.thread_id
        assert bob.identity.namespace == "bob-runtime"
        assert all(
            item.snapshot.namespace == "scheduler" for item in (first, second, bob)
        )
        assert sorted(observed) == [
            ("alice", "alice-runtime"),
            ("alice", "alice-runtime"),
            ("bob", "bob-runtime"),
        ]


async def test_retry_preserves_the_failed_execution_runtime_scope(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)

    @app.target("report")
    async def report(_request: ExecutionRequest) -> None:
        raise ValueError("report unavailable")

    async with app.worker() as worker:
        run = await app.for_owner("alice", execution_namespace="alice-runtime").run(
            "report"
        )
        await worker.wait_until_idle()
        observed = await app.for_owner(
            "alice", execution_namespace="different-runtime"
        ).get_run(run.id)
        retry = await observed.retry()
        assert retry.identity.namespace == "alice-runtime"
        assert retry.identity.thread_id != run.identity.thread_id
        await worker.wait_until_idle()


async def test_runtime_scope_is_part_of_creation_and_submission_intent(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)

    @app.target("report")
    async def report(_request: ExecutionRequest) -> None:
        pass

    async with app.worker() as worker:
        first = app.for_owner("alice")
        other = app.for_owner("alice", execution_namespace="alice-runtime")
        assert first.execution_namespace == "scheduler"
        schedule = Schedule.every(minutes=1, start_at=clock.now() + timedelta(days=1))
        await first.create_task(
            name="Report", target="report", schedule=schedule, request_id="task"
        )
        with pytest.raises(RequestConflictError):
            await other.create_task(
                name="Report", target="report", schedule=schedule, request_id="task"
            )
        await first.run("report", request_id="run")
        with pytest.raises(RequestConflictError):
            await other.run("report", request_id="run")
        await worker.wait_until_idle()


@pytest.mark.parametrize("namespace", ["", " padded ", "x" * 129])
def test_owner_rejects_invalid_runtime_namespace(namespace: str) -> None:
    with pytest.raises(ValueError):
        Automation().for_owner("alice", execution_namespace=namespace)
