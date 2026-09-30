"""Targets select new execution spaces while committed commands retain theirs."""

import asyncio
from collections.abc import Mapping
from datetime import timedelta

import pytest
from langchain.agents.middleware.types import InputAgentState
from pydantic import JsonValue

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin_automation import (
    Automation,
    AutomationLifecycleError,
    ExecutionStatus,
    RequestConflictError,
    Schedule,
    TargetNamespaceError,
    TargetNotFoundError,
    TinkerFinTarget,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore, CommandReceipt
from tinkerfin_automation.targets import ExecutionRequest


async def report(_request: ExecutionRequest) -> None:
    """Complete a target without external effects."""


async def test_fixed_runtimes_select_their_spaces_for_one_owner(
    store_with_clock: tuple[AutomationStore, ManualClock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, clock = store_with_clock
    observed: list[str] = []

    async def invoke(
        self: AgentRuntime[None],
        *,
        input: InputAgentState | None,
        **_kwargs: object,
    ) -> Mapping[str, object]:
        observed.append(self.namespace)
        return {"messages": []}

    monkeypatch.setattr(AgentRuntime, "ainvoke", invoke)
    app = Automation(namespace="scheduler", store=store, clock=clock)
    for name in ("project-a", "project-b"):
        runtime = TinkerFin().with_namespace(name).build(model="provider:model")
        app.target(name, TinkerFinTarget(runtime))
    async with app.worker() as worker:
        owner = app.for_owner("alice")
        runs = [await owner.run(name) for name in ("project-a", "project-b")]
        await worker.wait_until_idle()
        assert [run.identity.namespace for run in runs] == ["project-a", "project-b"]
        assert sorted(observed) == ["project-a", "project-b"]
        for run in runs:
            assert (await owner.get_run(run.id)).status is ExecutionStatus.SUCCEEDED


async def test_dynamic_target_preserves_user_spaces_and_fresh_threads(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)
    observed: list[tuple[str, str]] = []

    @app.target("report", execution_namespace=lambda owner: f"{owner}-runtime")
    async def record(request: ExecutionRequest) -> None:
        observed.append(
            (request.execution.owner_id, request.execution.identity.namespace)
        )

    async with app.worker() as worker:
        owner = app.for_owner("alice")
        task = await owner.create_task(
            name="Report",
            target="report",
            schedule=Schedule.every(minutes=1, start_at=clock.now()),
        )
        same = await app.for_owner("alice").task(task.id)
        await same.update(name="Renamed")
        first, second = await same.run(), await same.run()
        bob = await app.for_owner("bob").run("report")
        await worker.wait_until_idle()
        assert first.identity.namespace == second.identity.namespace == "alice-runtime"
        assert first.identity.thread_id != second.identity.thread_id
        assert bob.identity.namespace == "bob-runtime"
        assert sorted(observed) == [
            ("alice", "alice-runtime"),
            ("alice", "alice-runtime"),
            ("bob", "bob-runtime"),
        ]


async def test_retry_and_saved_run_queries_need_no_target_declaration(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)

    @app.target("report", execution_namespace="alice-runtime")
    async def fail(_request: ExecutionRequest) -> None:
        raise ValueError("report unavailable")

    async with app.worker() as worker:
        run = await app.for_owner("alice").run("report")
        await worker.wait_until_idle()
    async with Automation(namespace="scheduler", store=store, clock=clock) as client:
        owner = client.for_owner("alice")
        retry = await (await owner.get_run(run.id)).retry()
        assert retry.identity.namespace == run.identity.namespace == "alice-runtime"
        assert retry.identity.thread_id != run.identity.thread_id
        await retry.cancel()


def test_fixed_runtime_cannot_have_a_second_namespace_source() -> None:
    runtime = TinkerFin().with_namespace("user").build(model="provider:model")
    app = Automation(namespace="scheduler")
    with pytest.raises(ValueError, match="already selects"):
        app.target("report", TinkerFinTarget(runtime), execution_namespace="user")
    app.target("report", TinkerFinTarget(runtime))


async def test_delayed_worker_entry_rejects_remote_declarations() -> None:
    app = Automation()
    app.target("local", report)
    worker = app.worker()
    app.remote_target("remote", execution_namespace="user")
    with pytest.raises(AutomationLifecycleError, match="client lifecycle"):
        async with worker:
            pytest.fail("a remote declaration entered a local worker lifecycle")
    async with app:
        assert not (await app.for_owner("owner").list_runs()).items


@pytest.mark.parametrize("namespace", ["", " padded ", "x" * 129])
async def test_invalid_policy_rejects_before_any_submission(
    store_with_clock: tuple[AutomationStore, ManualClock],
    namespace: str,
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)
    app.target("report", report, execution_namespace=lambda _owner: namespace)
    async with app.worker():
        owner = app.for_owner("alice")
        with pytest.raises(TargetNamespaceError):
            await owner.create_task(
                name="Report",
                target="report",
                schedule=Schedule.once(at=clock.now() + timedelta(days=1)),
            )
        with pytest.raises(TargetNamespaceError):
            await owner.run("report")
        with pytest.raises(TargetNotFoundError):
            await owner.run("unknown")
        assert not (await owner.list_tasks()).items
        assert not (await owner.list_runs()).items


async def test_target_updates_preserve_space_and_queued_snapshots(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    app = Automation(namespace="scheduler", store=store, clock=clock)
    for target, namespace in [
        ("first", "user"),
        ("same", "user"),
        ("other", "elsewhere"),
    ]:
        app.target(target, report, execution_namespace=namespace)
    async with app.worker() as worker:
        owner = app.for_owner("alice")
        task = await owner.create_task(
            name="Report",
            target="first",
            schedule=Schedule.once(at=clock.now() + timedelta(days=1)),
        )
        original = await task.run()
        await task.update(target="same")
        with pytest.raises(TargetNamespaceError):
            await task.update(target="other", name="Must not persist")
        assert (await owner.task(task.id)).snapshot.target == "same"
        assert task.snapshot.name == "Report"
        assert (await owner.get_run(original.id)).snapshot.target == "first"
        await worker.wait_until_idle()


@pytest.mark.parametrize("saved_state", ["paused", "deleted"])
async def test_replay_precedes_changed_or_missing_target_and_past_schedule(
    store_with_clock: tuple[AutomationStore, ManualClock],
    saved_state: str,
) -> None:
    store, clock = store_with_clock
    schedule = Schedule.once(at=clock.now() + timedelta(hours=1))
    app = Automation(namespace="scheduler", store=store, clock=clock)
    app.target("report", report, execution_namespace="original")
    async with app.worker() as worker:
        owner = app.for_owner("alice")
        task = await owner.create_task(
            name="Report",
            target="report",
            schedule=schedule,
            request_id="create",
        )
        run = await owner.run("report", request_id="run")
        await task.update(name="Renamed", request_id="update")
        if saved_state == "paused":
            await task.pause()
        else:
            await task.delete()
        await worker.wait_until_idle()
    await clock.advance(timedelta(hours=2))
    restarted = Automation(namespace="scheduler", store=store, clock=clock)
    restarted.target("unrelated", report)
    async with restarted.worker() as worker:
        owner = restarted.for_owner("alice")
        repeated = await owner.create_task(
            name="Report",
            target="report",
            schedule=schedule,
            request_id="create",
        )
        assert repeated.id == task.id
        assert repeated.snapshot.execution_namespace == "original"
        assert (await owner.run("report", request_id="run")).id == run.id
        await repeated.update(name="Renamed", expected_revision=1, request_id="update")
        assert repeated.snapshot.name == "Renamed"
        with pytest.raises(RequestConflictError):
            await owner.run("report", input={"changed": True}, request_id="run")
        await worker.wait_until_idle()
        assert len((await owner.list_runs()).items) == 1
        tasks = (await owner.list_tasks()).items
        assert len(tasks) == (1 if saved_state == "paused" else 0)


@pytest.mark.parametrize("operation", ["create", "run"])
async def test_concurrent_first_submissions_use_the_winner_receipt(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store, clock = notifying_store_with_clock
    read = store.get_command_receipt
    ready = asyncio.Event()
    readers = 0

    async def synchronized_read(
        namespace: str,
        owner_id: str,
        request_id: str,
    ) -> CommandReceipt | None:
        nonlocal readers
        receipt = await read(namespace, owner_id, request_id)
        if receipt is None and request_id == "same":
            readers += 1
            if readers == 2:
                ready.set()
            await ready.wait()
        return receipt

    monkeypatch.setattr(store, "get_command_receipt", synchronized_read)
    first = Automation(namespace="scheduler", store=store, clock=clock)
    second = Automation(namespace="scheduler", store=store, clock=clock)
    first.remote_target("report", execution_namespace="first-space")
    second.remote_target("report", execution_namespace="second-space")
    async with first, second:
        owners = [app.for_owner("alice") for app in (first, second)]
        if operation == "create":
            tasks = await asyncio.gather(
                *[
                    owner.create_task(
                        name="Report",
                        target="report",
                        request_id="same",
                        schedule=Schedule.once(at=clock.now() + timedelta(days=1)),
                    )
                    for owner in owners
                ]
            )
            assert tasks[0].snapshot == tasks[1].snapshot
            assert len((await owners[0].list_tasks()).items) == 1
        else:
            runs = await asyncio.gather(
                *[owner.run("report", request_id="same") for owner in owners]
            )
            assert runs[0].identity == runs[1].identity
            assert runs[0].id == runs[1].id
            assert len((await owners[0].list_runs()).items) == 1


async def test_remote_client_manages_tasks_but_does_not_supply_worker_code(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    client = Automation(namespace="scheduler", store=store, clock=clock)
    client.remote_target("report", execution_namespace=lambda owner: f"ns_{owner}")
    client.remote_target("replacement", execution_namespace="ns_alice")
    with pytest.raises(AutomationLifecycleError, match="client lifecycle"):
        client.worker()
    async with client:
        owner = client.for_owner("alice")
        task = await owner.create_task(
            name="Report",
            target="report",
            schedule=Schedule.once(at=clock.now() + timedelta(days=1)),
        )
        assert task.snapshot.execution_namespace == "ns_alice"
        await task.update(target="replacement")
        await task.pause()
    async with Automation(namespace="scheduler", store=store, clock=clock) as queries:
        task = await queries.for_owner("alice").task(task.id)
        run = await task.run()
        await run.cancel()
        await task.delete()


async def test_worker_rejects_remote_configuration_mismatch_before_target_effects(
    notifying_store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = notifying_store_with_clock
    client = Automation(namespace="scheduler", store=store, clock=clock)
    client.remote_target("report", execution_namespace="wrong")
    async with client:
        run = await client.for_owner("alice").run("report")
    calls: list[JsonValue] = []
    app = Automation(namespace="scheduler", store=store, clock=clock)

    @app.target("report", execution_namespace="correct")
    async def execute(request: ExecutionRequest) -> None:
        calls.append(request.execution.execution_id)

    async with app.worker() as worker:
        await worker.wait_until_idle()
        result = await app.for_owner("alice").get_run(run.id)
        assert result.status is ExecutionStatus.FAILED
        assert result.snapshot.failure_code == "automation.target_namespace"
        assert calls == []
