"""Execution observation is bounded, cancellable, and independent of execution control."""

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tinkerfin_automation import ExecutionStatus, MemoryAutomationStore
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.errors import AutomationLifecycleError, AutomationWaitTimeout
from tinkerfin_automation.facade import Automation
from tinkerfin_automation.targets import ExecutionRequest

NOW = datetime(2026, 9, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    "status",
    [
        status
        for status in ExecutionStatus
        if status
        not in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCEL_REQUESTED,
        }
    ],
)
async def test_wait_returns_finished_and_attention_states(status, monkeypatch) -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    async with Automation(store=store, clock=clock) as app:
        run = await app.for_owner("subject").run("remote")

        async def observe(*args, **kwargs):
            return replace(run.snapshot, status=status, result={"items": [1]})

        monkeypatch.setattr(store, "get_execution", observe)
        assert await run.wait() is run
        assert run.status is status
        assert run.succeeded is (status is ExecutionStatus.SUCCEEDED)
        result = run.result
        assert isinstance(result, dict)
        result["items"] = []
        assert run.result == {"items": [1]}
    await store.close()


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.QUEUED, ExecutionStatus.RUNNING, ExecutionStatus.CANCEL_REQUESTED],
)
async def test_wait_timeout_preserves_unfinished_state_without_cancelling(
    status, monkeypatch
) -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    observed = asyncio.Event()
    async with Automation(store=store, clock=clock) as app:
        run = await app.for_owner("subject").run("remote")

        async def observe(*args, **kwargs):
            observed.set()
            return replace(run.snapshot, status=status)

        async def unexpected_cancel(*args, **kwargs):
            pytest.fail("observation cancelled execution")

        monkeypatch.setattr(store, "get_execution", observe)
        monkeypatch.setattr(store, "cancel_execution", unexpected_cancel)
        waiting = asyncio.create_task(run.wait(timeout=30))
        await observed.wait()
        await clock.advance(timedelta(seconds=30))
        with pytest.raises(AutomationWaitTimeout) as caught:
            await waiting
        assert caught.value.code == "automation.wait_timeout"
        assert isinstance(caught.value, TimeoutError)
        assert run.status is status
    await store.close()


@pytest.mark.parametrize("stop", ["cancel", "close", "timeout", "store_error"])
async def test_wait_settles_pending_read_and_preserves_failure(
    stop, monkeypatch
) -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    app = Automation(store=store, clock=clock)
    await app.__aenter__()
    run = await app.for_owner("subject").run("remote")
    entered = asyncio.Event()
    released = asyncio.Event()
    fail = asyncio.Event()
    error = RuntimeError("read unavailable")

    async def observe(*args, **kwargs):
        entered.set()
        try:
            await fail.wait()
            raise error
        finally:
            released.set()

    monkeypatch.setattr(store, "get_execution", observe)
    waiting = asyncio.create_task(run.wait())
    await entered.wait()
    if stop == "cancel":
        waiting.cancel()
        expected = asyncio.CancelledError
    elif stop == "close":
        await app.aclose()
        expected = AutomationLifecycleError
    elif stop == "timeout":
        await clock.advance(timedelta(seconds=30))
        expected = AutomationWaitTimeout
    else:
        fail.set()
        expected = RuntimeError
    with pytest.raises(expected) as caught:
        await waiting
    if stop == "store_error":
        assert caught.value is error
    assert released.is_set()
    assert run.status is ExecutionStatus.QUEUED
    await app.aclose()
    await store.close()


async def test_wait_does_not_hold_handle_lock_between_reads(monkeypatch) -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    observed = asyncio.Event()
    original = store.get_execution

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        observed.set()
        return result

    monkeypatch.setattr(store, "get_execution", observe)
    async with Automation(store=store, clock=clock) as app:
        run = await app.for_owner("subject").run("remote")
        waiting = asyncio.create_task(run.wait())
        await observed.wait()
        await run.cancel()
        await clock.advance(timedelta(seconds=1))
        assert await waiting is run
        assert run.status is ExecutionStatus.CANCELLED
    await store.close()


async def test_wait_deadline_includes_another_refresh_without_cancelling_it(
    monkeypatch,
) -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    read_started, release_read = asyncio.Event(), asyncio.Event()
    deadline_started = asyncio.Event()
    read = store.get_execution
    wait_until = clock.wait_until

    async def observe(*args, **kwargs):
        read_started.set()
        await release_read.wait()
        return await read(*args, **kwargs)

    async def wait(when: datetime) -> None:
        if when == NOW + timedelta(seconds=30):
            deadline_started.set()
        await wait_until(when)

    monkeypatch.setattr(store, "get_execution", observe)
    monkeypatch.setattr(clock, "wait_until", wait)
    try:
        async with Automation(store=store, clock=clock) as app:
            run = await app.for_owner("subject").run("remote")
            refreshing = asyncio.create_task(run.refresh())
            waiting = None
            try:
                await read_started.wait()
                waiting = asyncio.create_task(run.wait(timeout=30))
                await deadline_started.wait()
                await clock.advance(timedelta(seconds=30))
                with pytest.raises(AutomationWaitTimeout):
                    await waiting
                assert not refreshing.done()
                release_read.set()
                assert await refreshing is run
                assert run.status is ExecutionStatus.QUEUED
            finally:
                release_read.set()
                if waiting is not None:
                    if not waiting.done():
                        waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                await asyncio.gather(refreshing, return_exceptions=True)
    finally:
        await store.close()


async def test_remote_worker_result_is_visible_through_shared_store(
    store_with_clock,
) -> None:
    store, clock = store_with_clock
    worker_app = Automation(namespace="shared", store=store, clock=clock)

    @worker_app.target("report")
    async def report(request: ExecutionRequest) -> dict[str, str]:
        return {"result": "ready"}

    async with worker_app.worker() as worker:
        async with Automation(namespace="shared", store=store, clock=clock) as client:
            run = await client.for_owner("subject").run("report")
            await worker.wait_until_idle()
            assert await run.wait() is run
            assert run.succeeded
            assert run.result == {"result": "ready"}


@pytest.mark.parametrize("duration", [0, -1, True, float("nan"), float("inf")])
async def test_wait_rejects_invalid_seconds(duration) -> None:
    async with Automation() as app:
        run = await app.for_owner("subject").run("remote")
        with pytest.raises(ValueError):
            await run.wait(timeout=duration)
        with pytest.raises(ValueError):
            await run.wait(poll_interval=duration)


async def test_wait_observes_a_separate_worker_process(
    tmp_path: Path, monkeypatch
) -> None:
    from sql_test_support import control_database_clock
    from sqlalchemy.ext.asyncio import create_async_engine

    from tinkerfin_automation import SqlAlchemyAutomationStore

    url = f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}"
    database = create_async_engine(url)
    store = SqlAlchemyAutomationStore(database)
    clock = ManualClock(NOW)
    control_database_clock(database, clock, monkeypatch)
    observed = asyncio.Event()
    read = store.get_execution

    async def observe(*args, **kwargs):
        snapshot = await read(*args, **kwargs)
        observed.set()
        return snapshot

    monkeypatch.setattr(store, "get_execution", observe)
    process = None
    waiting = None
    try:
        async with Automation(
            namespace="process-test", store=store, clock=clock
        ) as client:
            run = await client.for_owner("subject").run("report")
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("wait_worker.py")),
                url,
                clock.now().isoformat(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdin is not None and process.stdout is not None
            assert await process.stdout.readline() == b"running\n"
            waiting = asyncio.create_task(run.wait())
            await observed.wait()
            assert run.status is ExecutionStatus.RUNNING
            process.stdin.write(b"finish\n")
            await process.stdin.drain()
            assert await process.stdout.readline() == b"completed\n"
            await clock.advance(timedelta(seconds=1))
            assert await waiting is run
            assert run.succeeded and run.result == {"process": "worker"}
            _, errors = await process.communicate()
            assert process.returncode == 0, errors.decode()
    finally:
        if waiting is not None:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        await store.close()
        await database.dispose()
