"""Target permissions apply to saved tasks and the exact revision being used."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from tinkerfin_automation import (
    Automation,
    AutomationOwner,
    ExecutionRequest,
    OnceSchedule,
    TaskConflictError,
    TaskHandle,
    create_automation_tools,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.models import AutomationTask
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import AutomationStore


@pytest.fixture
async def automation_owner(store_with_clock):
    store, clock = store_with_clock
    app = Automation(namespace="app", store=store, clock=clock)

    @app.target("allowed")
    async def allowed(request: ExecutionRequest) -> None:
        return None

    async with app.worker():
        yield app.for_owner("owner")


async def _task(
    service: AutomationService, clock: ManualClock, target: str
) -> AutomationTask:
    return await service.create_task(
        owner_id="owner",
        name="Report",
        target=target,
        schedule=OnceSchedule(at=clock.now() + timedelta(days=1)),
    )


@pytest.mark.parametrize(
    "action", ["update_automation", "enable_automation", "run_automation_task_now"]
)
async def test_existing_task_cannot_bypass_target_permissions(
    store_with_clock: tuple[AutomationStore, ManualClock],
    automation_owner: AutomationOwner,
    action: str,
) -> None:
    store, clock = store_with_clock
    async with AutomationService(namespace="app", store=store, clock=clock) as service:
        task = await _task(service, clock, "restricted")
        tools = {
            t.name: t
            for t in create_automation_tools(
                automation_owner, allowed_targets={"allowed"}
            )
        }
        arguments: dict[str, Any] = {"task_id": task.task_id, "request_id": "attempt"}
        if action != "run_automation_task_now":
            arguments["expected_revision"] = task.revision
        with pytest.raises(ValueError, match="not allowed"):
            await tools[action].ainvoke(arguments)
        assert await service.get_task(owner_id="owner", task_id=task.task_id) == task
        assert not (await service.list_executions(owner_id="owner")).items


@pytest.mark.parametrize(
    "action,revision_offset",
    [
        ("update_automation", 0),
        ("enable_automation", 0),
        ("run_automation_task_now", 0),
        ("update_automation", 1),
        ("enable_automation", 1),
    ],
)
async def test_concurrent_target_change_cannot_change_authorized_work(
    store_with_clock: tuple[AutomationStore, ManualClock],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    revision_offset: int,
    automation_owner: AutomationOwner,
) -> None:
    store, clock = store_with_clock
    async with AutomationService(namespace="app", store=store, clock=clock) as service:
        async with AutomationService(namespace="app", store=store, clock=clock) as peer:
            task = await _task(service, clock, "allowed")
            ready, release = asyncio.Event(), asyncio.Event()
            read = automation_owner.task

            async def gated_read(task_id: str) -> TaskHandle:
                result = await read(task_id)
                if not ready.is_set():
                    ready.set()
                    await release.wait()
                return result

            monkeypatch.setattr(automation_owner, "task", gated_read)
            tools = {
                t.name: t
                for t in create_automation_tools(
                    automation_owner, allowed_targets={"allowed"}
                )
            }
            arguments: dict[str, Any] = {
                "task_id": task.task_id,
                "request_id": "attempt",
            }
            if action != "run_automation_task_now":
                arguments["expected_revision"] = task.revision + revision_offset
            pending = asyncio.create_task(tools[action].ainvoke(arguments))
            try:
                await ready.wait()
                await peer.update_task(
                    owner_id="owner",
                    task_id=task.task_id,
                    expected_revision=task.revision,
                    target="restricted",
                )
                release.set()
                with pytest.raises(TaskConflictError):
                    await pending
                assert not (await service.list_executions(owner_id="owner")).items
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_run_now_checks_revision_inside_enqueue_and_keeps_command_idempotency(
    store_with_clock: tuple[AutomationStore, ManualClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, clock = store_with_clock
    async with AutomationService(namespace="app", store=store, clock=clock) as service:
        task = await _task(service, clock, "allowed")
        ready, release = asyncio.Event(), asyncio.Event()
        enqueue = store.enqueue_execution

        async def gated_enqueue(*args: Any, **kwargs: Any):
            ready.set()
            await release.wait()
            return await enqueue(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(store, "enqueue_execution", gated_enqueue)
            pending = asyncio.create_task(
                service.run_task_now(
                    owner_id="owner",
                    task_id=task.task_id,
                    expected_revision=task.revision,
                    request_id="first",
                )
            )
            try:
                await ready.wait()
                updated = await service.update_task(
                    owner_id="owner",
                    task_id=task.task_id,
                    expected_revision=task.revision,
                    name="Updated report",
                )
                release.set()
                with pytest.raises(TaskConflictError):
                    await pending
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        first = await service.run_task_now(
            owner_id="owner",
            task_id=task.task_id,
            expected_revision=updated.revision,
            request_id="first",
        )
        await service.update_task(
            owner_id="owner",
            task_id=task.task_id,
            expected_revision=updated.revision,
            name="Another report",
        )
        repeated = await service.run_task_now(
            owner_id="owner",
            task_id=task.task_id,
            expected_revision=updated.revision,
            request_id="first",
        )
        assert repeated == first


@pytest.mark.parametrize(
    "action", ["update_automation", "enable_automation", "run_automation_task_now"]
)
async def test_allowed_task_command_replays_after_its_revision_changes(
    store_with_clock: tuple[AutomationStore, ManualClock],
    automation_owner: AutomationOwner,
    action: str,
) -> None:
    store, clock = store_with_clock
    async with AutomationService(namespace="app", store=store, clock=clock) as service:
        task = await _task(service, clock, "allowed")
        tools = {
            t.name: t
            for t in create_automation_tools(
                automation_owner, allowed_targets={"allowed"}
            )
        }
        arguments: dict[str, Any] = {"task_id": task.task_id, "request_id": "repeat"}
        if action != "run_automation_task_now":
            arguments["expected_revision"] = task.revision
        first = await tools[action].ainvoke(arguments)
        repeated = await tools[action].ainvoke(arguments)
        if action == "run_automation_task_now":
            assert repeated["execution_id"] == first["execution_id"]
        else:
            assert repeated == first
