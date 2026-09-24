"""Owner-bound commands preserve revisions, isolation, and durable request intent."""

import asyncio

import pytest

from tinkerfin_automation import (
    AutomationLifecycleError,
    AutomationOwner,
    ExecutionStatus,
    RunHandle,
    Schedule,
    TaskConflictError,
    TaskHandle,
    TaskNotFoundError,
    TaskStatus,
)
from tinkerfin_automation.facade import Automation
from tinkerfin_automation.targets import ExecutionRequest


async def report(request: ExecutionRequest) -> None:
    assert request.input is request.execution.input


@pytest.mark.parametrize("handle_type", [AutomationOwner, TaskHandle, RunHandle])
def test_bound_objects_are_obtained_from_owner_operations(handle_type) -> None:
    with pytest.raises(TypeError):
        handle_type()


async def test_task_handles_keep_independent_snapshots_and_delete_receipts(
    store_with_clock,
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="app", store=store, clock=clock)
    app.target("report", report)
    async with app.worker():
        owner = app.for_owner("subject")
        task = await owner.create_task(
            name="Daily",
            target="report",
            schedule=Schedule.every(minutes=1, start_at=clock.now()),
            input={"nested": {"value": "original"}},
            request_id="create",
        )
        other = await owner.task(task.id)
        detached = task.snapshot
        nested = detached.input["nested"]
        assert isinstance(nested, dict)
        nested["value"] = "changed"
        assert task.snapshot.input == {"nested": {"value": "original"}}
        assert (await owner.task(task.id)).snapshot.input == task.snapshot.input
        assert await task.pause(request_id="pause") is task
        assert task.status is TaskStatus.PAUSED
        assert other.revision == 1
        with pytest.raises(TaskConflictError):
            await other.enable()
        assert other.revision == 1
        await other.refresh()
        with pytest.raises(TaskConflictError):
            await other.enable(expected_revision=1)
        assert await task.update(input={}) is task
        assert not task.snapshot.input
        assert (await owner.summarize_tasks())[TaskStatus.PAUSED] == 1
        page = await owner.list_tasks(limit=1)
        assert page.items[0].id == task.id
        assert page.next_cursor is None
        revision = task.revision
        await task.delete(expected_revision=revision, request_id="delete")
        assert task.is_deleted
        assert task.snapshot.name == "Daily"
        await owner.delete_task(
            task.id, expected_revision=revision, request_id="delete"
        )
        await task.delete(expected_revision=revision, request_id="delete")
        with pytest.raises(TaskNotFoundError):
            await task.refresh()
    assert task.is_deleted and task.snapshot.name == "Daily"


async def test_client_submits_and_cancels_but_cannot_write_schedules(
    store_with_clock,
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="app", store=store, clock=clock)
    async with app:
        owner = app.for_owner("subject")
        with pytest.raises(AutomationLifecycleError):
            await owner.create_task(
                name="Forbidden",
                target="remote",
                schedule=Schedule.every(minutes=1, start_at=clock.now()),
            )
        assert not (await owner.list_tasks()).items
        run = await owner.run(
            "remote", input={"secret_marker": "not-in-repr"}, request_id="submit"
        )
        assert run.status is ExecutionStatus.QUEUED
        assert run.id != run.identity.run_id
        assert "not-in-repr" not in repr(run)
        assert (await owner.get_run(run.id)).id == run.id
        assert await run.cancel(request_id="cancel") is run
        assert run.status is ExecutionStatus.CANCELLED
        retry = await run.retry(request_id="retry")
        assert retry.id != run.id
        assert retry.snapshot.retry_of == run.id
        assert run.status is ExecutionStatus.CANCELLED
        assert len((await owner.list_runs()).items) == 2
        assert (await owner.summarize_runs())[ExecutionStatus.QUEUED] == 1


async def test_owner_and_namespace_isolation_and_page_has_no_get_queries(
    store_with_clock, monkeypatch
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="app", store=store, clock=clock)
    app.target("report", report)
    async with app.worker():
        owner = app.for_owner("subject")
        task = await owner.create_task(
            name="Daily",
            target="report",
            schedule=Schedule.every(minutes=1, start_at=clock.now()),
        )
        with pytest.raises(TaskNotFoundError):
            await app.for_owner("another").task(task.id)
        async with Automation(namespace="another", store=store, clock=clock) as other:
            with pytest.raises(TaskNotFoundError):
                await other.for_owner("subject").task(task.id)

        async def unexpected_get(*args, **kwargs):
            raise AssertionError("page performed a per-item read")

        monkeypatch.setattr(store, "get_task", unexpected_get)
        assert (await owner.list_tasks()).items[0].id == task.id


async def test_concurrent_handle_commands_capture_revision_before_the_lock(
    store_with_clock, monkeypatch
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="app", store=store, clock=clock)
    app.target("report", report)
    async with app.worker():
        task = await app.for_owner("subject").create_task(
            name="Daily",
            target="report",
            schedule=Schedule.every(minutes=1, start_at=clock.now()),
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original = store.update_task

        async def update(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(store, "update_task", update)
        first = asyncio.create_task(task.pause())
        await entered.wait()
        second_started = asyncio.Event()

        async def second_change():
            second_started.set()
            return await task.update(name="Renamed")

        second = asyncio.create_task(second_change())
        await second_started.wait()
        release.set()
        await first
        with pytest.raises(TaskConflictError):
            await second
        assert task.revision == 2
        assert task.snapshot.name == "Daily"
