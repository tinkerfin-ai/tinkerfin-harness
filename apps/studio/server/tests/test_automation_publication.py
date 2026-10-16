"""权威执行结算与用户会话入口的联合契约"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_automation_integration import automation_worker as automation_worker
from test_automation_integration import configuration, wait_for_published_conversation

from tinkerfin import RunIdentity
from tinkerfin_automation import Automation, AutomationOwner, ExecutionStatus, RunHandle
from tinkerfin_automation.clock import ManualClock
from tinkerfin_notifications import Notification, NotificationScope
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.automation.publication import AutomationConversationPublications
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import StudioAutomationService
from tinkerfin_studio.automation.target import StudioAutomationTarget
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.repository import ConversationRepository


async def test_finished_trace_remains_hidden_until_automation_settlement(
    automation_resources, automation_worker, monkeypatch
):
    resources = automation_resources
    trace_finished, allow_settlement = asyncio.Event(), asyncio.Event()
    execute = StudioAutomationTarget.run

    async def finishing(self, request):
        result = await execute(self, request)
        trace_finished.set()
        await allow_settlement.wait()
        return result

    monkeypatch.setattr(StudioAutomationTarget, "run", finishing)
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="visibility",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    run = await (
        await resources.automation.for_owner("1:project-1").task(task.id)
    ).run()
    try:
        await trace_finished.wait()
        assert (await run.refresh()).status == ExecutionStatus.RUNNING
        detail = await service.result(run.id)
        assert detail.messages
        assert detail.conversation.state == "pending"
        async with resources.database.session() as session:
            repository = ConversationRepository(session)
            for archived in (False, True):
                assert not await repository.list_threads(
                    user_id=1, page_size=1, cursor=None, archived=archived
                )
            assert (
                await repository.get_accessible_thread(
                    user_id=1, thread_id=run.identity.thread_id
                )
                is None
            )
            history = ConversationHistoryService(
                repository,
                user_id=1,
                tracer=resources.tracer,
                history_queries=resources.history_queries,
            )
            with pytest.raises(BusinessException) as failure:
                await history.get_detail(run.identity.thread_id)
            assert failure.value.error_code == ConversationErrorCode.NOT_FOUND
    finally:
        allow_settlement.set()
    await automation_worker.wait_until_idle()
    view = await wait_for_published_conversation(resources, run.id)
    assert view.conversation.state == "available"
    async with resources.database.session() as session:
        repository = ConversationRepository(session)
        rows = await repository.list_threads(user_id=1, page_size=1, cursor=None)
        assert [row.thread_id for row in rows] == [run.identity.thread_id]
        thread = rows[0]
        published_at = thread.published_at
        followup = await repository.create_run_registration(
            thread_id=thread.id,
            run_id="followup",
            parent_run_id=run.identity.run_id,
            model_id="main",
            input_json={},
        )
        assert await repository.activate_run_registration(
            thread_pk=thread.id, run_pk=followup.id, run_id=followup.run_id
        )
        assert thread.published_at == published_at
        assert (await repository.list_threads(user_id=1, page_size=1, cursor=None))[
            0
        ].status == "running"


async def register_pending(database, identity, execution_id="execution"):
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            project_id="project-1",
            thread_id=identity.thread_id,
            title="待开放",
            model_id="main",
        )
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="main",
            automation_execution_id=execution_id,
            input_json={"forwardedProps": {"projectId": "project-1"}},
        )
        await repository.activate_run_registration(
            thread_pk=thread.id, run_pk=run.id, run_id=identity.run_id
        )
        await repository.commit()
        return thread.id


def execution_reader(identity, status):
    reader = create_autospec(Automation, instance=True)
    run = create_autospec(RunHandle, instance=True)
    run.identity = identity
    run.status = status
    run.wait.return_value = run
    reader.for_owner.return_value = create_autospec(AutomationOwner, instance=True)
    reader.for_owner.return_value.get_run.return_value = run
    return reader, run


async def next_publication(changes):
    while True:
        notice = await anext(changes)
        if isinstance(notice, Notification) and notice.details.get(
            "automation_execution_id"
        ):
            return notice


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.NEEDS_ATTENTION,
    ],
)
async def test_settled_execution_opens_once_without_waiting_for_title(
    database, notifications, status
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    thread_pk = await register_pending(database, identity)
    reader, _ = execution_reader(identity, status)
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    async with notifications.subscribe(
        scope=NotificationScope("ns_1"), topics=("studio.conversation.changed",)
    ) as changes:
        try:
            await publications.start()
            notice = await next_publication(changes)
            assert notice.key == identity.thread_id
            assert notice.details["automation_project_id"] == "project-1"
            async with database.session() as session:
                repository = ConversationRepository(session)
                thread = await repository.get_accessible_thread(
                    user_id=1, thread_id="thread"
                )
                assert thread is not None and thread.published_at is not None
                assert thread.title_generation_status == "idle"
                assert not await repository.unpublished_automations()
                stored = await repository.get_thread_by_pk(thread_pk)
                assert stored is not None and stored.published_at == thread.published_at
        finally:
            await publications.aclose()
    reader.for_owner.assert_called_with("1:project-1")


async def test_close_cancels_observation_without_cancelling_execution(
    database, notifications
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, run = execution_reader(identity, ExecutionStatus.RUNNING)
    observing, released = asyncio.Event(), asyncio.Event()

    async def wait(**kwargs):
        observing.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    run.wait.side_effect = wait
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    await publications.start()
    await observing.wait()
    await publications.aclose()
    await publications.aclose()
    assert released.is_set()
    run.cancel.assert_not_called()
    async with database.session() as session:
        assert (
            await ConversationRepository(session).get_accessible_thread(
                user_id=1, thread_id="thread"
            )
            is None
        )


async def test_publication_write_failure_recovers_on_controlled_repair(
    database, notifications, monkeypatch
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, _ = execution_reader(identity, ExecutionStatus.SUCCEEDED)
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    failed = asyncio.Event()
    persist = ConversationRepository.publish_automation

    async def fail_once(self, *args, **kwargs):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("提交暂不可用")
        return await persist(self, *args, **kwargs)

    monkeypatch.setattr(ConversationRepository, "publish_automation", fail_once)
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications, clock=clock
    )
    async with notifications.subscribe(
        scope=NotificationScope("ns_1"), topics=("studio.conversation.changed",)
    ) as changes:
        try:
            await publications.start()
            await failed.wait()
            async with database.session() as session:
                assert (
                    await ConversationRepository(session).get_accessible_thread(
                        user_id=1, thread_id="thread"
                    )
                    is None
                )
            await clock.advance(timedelta(seconds=30))
            await next_publication(changes)
        finally:
            await publications.aclose()


async def test_recovery_and_competing_instances_publish_monotonically(
    database, notifications
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, _ = execution_reader(identity, ExecutionStatus.SUCCEEDED)
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    first = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications, clock=clock
    )
    second = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications, clock=clock
    )
    async with notifications.subscribe(
        scope=NotificationScope("ns_1"), topics=("studio.conversation.changed",)
    ) as changes:
        try:
            await first.start()
            await second.start()
            await next_publication(changes)
        finally:
            await first.aclose()
            await clock.advance(timedelta(days=1))
            await second.aclose()
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_accessible_thread(user_id=1, thread_id="thread")
        assert thread is not None
        assert thread.published_at == datetime(2030, 1, 1)
        assert not await repository.unpublished_automations()


async def test_observation_capacity_leaves_backlog_in_database(database, notifications):
    reader = create_autospec(Automation, instance=True)
    owner = create_autospec(AutomationOwner, instance=True)
    reader.for_owner.return_value = owner
    releases = [asyncio.Event() for _ in range(17)]
    observing: list[int] = []
    capacity_reached, backlog_started = asyncio.Event(), asyncio.Event()
    executions = {}
    for index in range(17):
        identity = RunIdentity(
            namespace="ns_1", thread_id=f"thread-{index}", run_id=f"run-{index}"
        )
        await register_pending(database, identity, execution_id=f"execution-{index}")
        _, run = execution_reader(identity, ExecutionStatus.RUNNING)
        executions[f"execution-{index}"] = run

        async def wait(*, timeout, item=index, handle=run):
            observing.append(item)
            if len(observing) == 16:
                capacity_reached.set()
            if item == 16:
                backlog_started.set()
            await releases[item].wait()
            handle.status = ExecutionStatus.SUCCEEDED
            return handle

        run.wait.side_effect = wait

    async def get_run(execution_id):
        return executions[execution_id]

    owner.get_run.side_effect = get_run
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    try:
        await publications.start()
        await capacity_reached.wait()
        assert len(observing) == 16
        assert not backlog_started.is_set()
        async with database.session() as session:
            assert (
                len(await ConversationRepository(session).unpublished_automations())
                == 17
            )
        releases[0].set()
        await backlog_started.wait()
        async with database.session() as session:
            assert (
                len(await ConversationRepository(session).unpublished_automations())
                == 16
            )
    finally:
        await publications.aclose()


async def test_caller_cancellation_waits_for_observer_cleanup(database, notifications):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, run = execution_reader(identity, ExecutionStatus.RUNNING)
    observing, cleaning, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait(**kwargs):
        observing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await allow_close.wait()

    run.wait.side_effect = wait
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    await publications.start()
    await observing.wait()
    closing = asyncio.create_task(publications.aclose())
    try:
        await cleaning.wait()
        closing.cancel()
    finally:
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    await publications.aclose()
    run.cancel.assert_not_called()


def contains_error(error, expected):
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is expected:
            return True
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend([current.__cause__, current.__context__])
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return False


async def test_real_wait_preserves_subscription_cleanup_failure(
    database, notifications, monkeypatch
):
    from tinkerfin_automation import MemoryAutomationStore
    from tinkerfin_notifications import NotificationSubscription

    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    store = MemoryAutomationStore(clock=clock, notifications=notifications)
    reader = Automation(namespace="studio_automation", store=store, clock=clock)
    reader.remote_target("remote", execution_namespace="ns_1")
    try:
        async with reader:
            run = await reader.for_owner("1:project-1").run("remote")
            await register_pending(database, run.identity, execution_id=run.id)
            entered = asyncio.Event()
            subscription_enter = NotificationSubscription.__aenter__
            subscription_exit = NotificationSubscription.__aexit__
            cleanup_failure = OSError("通知订阅清理失败")

            async def enter(self):
                value = await subscription_enter(self)
                entered.set()
                return value

            async def leave(self, *args):
                await subscription_exit(self, *args)
                raise cleanup_failure

            monkeypatch.setattr(NotificationSubscription, "__aenter__", enter)
            monkeypatch.setattr(NotificationSubscription, "__aexit__", leave)
            publications = AutomationConversationPublications(
                database=database,
                automation=reader,
                notifications=notifications,
                clock=clock,
            )
            await publications.start()
            await entered.wait()
            with pytest.raises(BaseException) as failure:
                await publications.aclose()
            assert contains_error(failure.value, cleanup_failure)
    finally:
        await store.close()


async def test_close_failure_retains_callers_cancellation(database, notifications):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, run = execution_reader(identity, ExecutionStatus.RUNNING)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup_failure = OSError("订阅清理失败")
    control = BaseException("进程控制")

    async def wait(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise BaseExceptionGroup("关闭失败", [control, cleanup_failure]) from None

    run.wait.side_effect = wait
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    await publications.start()
    await entered.wait()

    closer = asyncio.create_task(publications.aclose())
    await cleaning.wait()
    closer.cancel("调用者取消")
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()
    release.set()
    with pytest.raises(BaseExceptionGroup) as failure:
        await closer
    assert contains_error(failure.value, cleanup_failure)
    assert contains_error(failure.value, control)
    assert any(
        isinstance(error, asyncio.CancelledError) for error in failure.value.exceptions
    )


async def test_close_preserves_repair_wait_cleanup_failure(database, notifications):
    entered = asyncio.Event()
    cleanup_failure = OSError("补偿等待清理失败")

    class Clock(ManualClock):
        async def wait_until(self, when):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise cleanup_failure

    publications = AutomationConversationPublications(
        database=database,
        automation=create_autospec(Automation, instance=True),
        notifications=notifications,
        clock=Clock(datetime(2030, 1, 1, tzinfo=UTC)),
    )
    await publications.start()
    await entered.wait()
    with pytest.raises(BaseException) as failure:
        await publications.aclose()
    assert contains_error(failure.value, cleanup_failure)


async def test_final_reconciliation_retains_control_and_caller_cancel(
    database, notifications
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    await register_pending(database, identity)
    reader, _ = execution_reader(identity, ExecutionStatus.RUNNING)
    entered, release = asyncio.Event(), asyncio.Event()
    control = BaseException("校准控制信号")

    async def get_run(*args):
        entered.set()
        await release.wait()
        raise control

    reader.for_owner.return_value.get_run.side_effect = get_run
    publications = AutomationConversationPublications(
        database=database, automation=reader, notifications=notifications
    )
    closer = asyncio.create_task(publications.aclose())
    await entered.wait()
    closer.cancel("校准调用者取消")
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()
    release.set()
    with pytest.raises(BaseExceptionGroup) as failure:
        await closer
    assert contains_error(failure.value, control)
    assert any(
        isinstance(error, asyncio.CancelledError) and error.args == ("校准调用者取消",)
        for error in failure.value.exceptions
    )


@pytest.mark.parametrize("operation", ["scan", "publish"])
async def test_database_cleanup_failure_stops_publication_on_close(
    database, notifications, monkeypatch, operation
):
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    if operation == "publish":
        await register_pending(database, identity)
    reader, _ = execution_reader(identity, ExecutionStatus.SUCCEEDED)
    entered = asyncio.Event()
    cleanup_failure = OSError("数据库取消清理失败")
    method = "unpublished_automations" if operation == "scan" else "publish_automation"
    original = getattr(ConversationRepository, method)
    calls = 0

    async def execute(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (2 if operation == "scan" else 1):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise cleanup_failure
        return await original(self, *args, **kwargs)

    class Clock(ManualClock):
        async def wait_until(self, when):
            if operation == "scan" and entered.is_set():
                raise AssertionError("关闭期间的扫描失败不得重新等待")
            await super().wait_until(when)

    monkeypatch.setattr(ConversationRepository, method, execute)
    publications = AutomationConversationPublications(
        database=database,
        automation=reader,
        notifications=notifications,
        clock=Clock(datetime(2030, 1, 1, tzinfo=UTC)),
    )
    await publications.start()
    await entered.wait()
    with pytest.raises(BaseException) as failure:
        await publications.aclose()
    assert contains_error(failure.value, cleanup_failure)
