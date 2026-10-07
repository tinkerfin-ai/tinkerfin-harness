"""运行终止、业务摘要恢复和删除并发的联合契约"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from unittest.mock import create_autospec

import pytest
from ag_ui.core import RunStartedEvent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

import tinkerfin_studio.conversation.coordinator as coordinator_module
from tinkerfin import TinkerFin
from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_gateway import CommittedRunEvent, StartRun
from tinkerfin_messaging import AgUiChannel, MemoryBackend, Messaging
from tinkerfin_notifications import Notifications
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.command import ConversationCommandService
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.resources import (
    ApplicationResources,
    _enter_conversation_resources,
    _LifespanOutcome,
    _settle_lifespan_stack,
)
from tinkerfin_tracing import RunFact, Tracer, TraceThreadNotFound


class _PausedModel(FakeMessagesListChatModel):
    """由执行信号确认模型在途，由调用者关闭结束生成"""

    started: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
    stopped: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)

    # LangChain 的可扩展工具和调用参数属于未类型化的第三方边界
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self

    async def _agenerate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any
    ) -> ChatResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("模型只能通过取消结束")
        finally:
            self.stopped.set()


async def _register(database: Database, identity: RunIdentity) -> int:
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-7",
            user_id=7,
            thread_id=identity.thread_id,
            title="执行结算",
            model_id="main",
        )
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="main",
            input_json={"runId": identity.run_id},
        )
        thread.last_run_id = run.run_id
        thread.status = run.status = "running"
        await repository.commit()
        return thread.id


async def _terminal(
    tracer: Tracer,
    identity: RunIdentity,
    outcome: Literal["succeeded", "cancelled", "failed"] = "cancelled",
) -> None:
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    observer = await tracer.open_run(context)
    stamp = datetime(2026, 9, 29, tzinfo=UTC)
    for event in (
        RunStartedObservation(identity=identity, observed_at=stamp, monotonic_ns=1),
        RunInputObservation(
            identity=identity, source=context, observed_at=stamp, monotonic_ns=2
        ),
        RunTerminalObservation(
            identity=identity, outcome=outcome, observed_at=stamp, monotonic_ns=3
        ),
        RunClosedObservation(
            identity=identity, outcome=outcome, observed_at=stamp, monotonic_ns=4
        ),
    ):
        await observer.observe(event)
    await observer.aclose()


@pytest.mark.parametrize("summary_failure", [False, True])
async def test_application_shutdown_settles_execution_before_final_summary(
    database: Database,
    notifications: Notifications,
    monkeypatch: pytest.MonkeyPatch,
    summary_failure: bool,
) -> None:
    identity = RunIdentity(namespace="ns_7", thread_id="shutdown", run_id="run")
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    model = _PausedModel(responses=[AIMessage(content="unused")])
    runtime = TinkerFin().with_namespace("ns_7").with_observer(tracer).build(model)
    backend = MemoryBackend()
    stack = AsyncExitStack()
    channel, gateway, coordinator = await _enter_conversation_resources(
        stack,
        _LifespanOutcome(),
        messaging=Messaging(backend=backend),
        database=database,
        notifications=notifications,
        tracer=tracer,
    )
    thread_pk = await _register(database, identity)

    async def committed(event: CommittedRunEvent) -> None:
        if isinstance(event.event, RunStartedEvent):
            coordinator.ensure(thread_pk=thread_pk, identity=identity)

    try:
        await gateway.start(
            runtime,
            StartRun(
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                messages=({"id": "question", "role": "user", "content": "等待"},),
            ),
            on_committed=committed,
        )
        await model.started.wait()
        await coordinator.reconcile(thread_pk=thread_pk, identity=identity)
        if summary_failure:

            async def unavailable() -> None:
                raise OSError("summary unavailable")

            # 无论跟随是否已结算终态，关闭仍必须尝试最后校准并交付失败
            monkeypatch.setattr(coordinator, "settle", unavailable)
            with pytest.raises(OSError, match="summary unavailable"):
                await stack.aclose()
        else:
            await stack.aclose()
    finally:
        await stack.aclose()

    assert model.stopped.is_set()
    trace = await tracer.get(identity.thread, head_run_id=identity.run_id)
    assert trace.summary.status.execution == "cancelled"
    assert not any(
        task.get_name() == f"studio-trace-summary:{identity.run_id}"
        for task in asyncio.all_tasks()
        if not task.done()
    )
    if not summary_failure:
        async with database.session() as session:
            repository = ConversationRepository(session)
            thread = await repository.get_thread_by_pk(thread_pk)
            run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
            assert thread is not None and run is not None
            assert thread.status == "idle"
            assert run.status == run.terminal_outcome == "cancelled"
            page = await trace.events()
            terminal_seq = next(
                event.trace_seq
                for event in page.items
                if isinstance(event.fact, RunFact) and event.fact.phase == "terminal"
            )
            assert run.trace_as_of_seq is not None
            # 终态已可结算，随后 writer 的 closed 不改变业务执行结果
            assert terminal_seq <= run.trace_as_of_seq <= trace.as_of_seq
            assert run.finished_at is not None
    async with Messaging(backend=backend) as observer:
        assert (
            await observer.agui_channel(name=channel.name).get_run_status(
                identity=identity
            )
            == "failed"
        )


@pytest.mark.parametrize("registration_status", ["preparing", "starting", "running"])
@pytest.mark.parametrize("outcome", ["succeeded", "cancelled", "failed"])
async def test_startup_recovers_running_registrations_from_trace(
    database: Database,
    notifications: Notifications,
    outcome: Literal["succeeded", "cancelled", "failed"],
    registration_status: str,
) -> None:
    identity = RunIdentity(namespace="ns_7", thread_id="recover", run_id="run")
    thread_pk = await _register(database, identity)
    async with database.session() as session:
        repository = ConversationRepository(session)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert run is not None
        run.status = registration_status
        await repository.commit()
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    await _terminal(tracer, identity, outcome)
    async with AsyncExitStack() as stack:
        await _enter_conversation_resources(
            stack,
            _LifespanOutcome(),
            messaging=Messaging(),
            database=database,
            notifications=notifications,
            tracer=tracer,
        )
        async with database.session() as session:
            repository = ConversationRepository(session)
            thread = await repository.get_thread_by_pk(thread_pk)
            run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
            assert thread is not None and run is not None
            assert thread.status == ("error" if outcome == "failed" else "idle")
            assert run.status == run.terminal_outcome == outcome


@pytest.mark.parametrize("trace_exists", [True, False])
async def test_delete_calibrates_stale_summary_and_refuses_missing_authority(
    database: Database,
    notifications: Notifications,
    trace_exists: bool,
) -> None:
    identity = RunIdentity(namespace="ns_7", thread_id="delete", run_id="run")
    thread_pk = await _register(database, identity)
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    if trace_exists:
        await _terminal(tracer, identity)
    channel = create_autospec(AgUiChannel, instance=True)
    channel.get_run_status.return_value = "failed"
    coordinator = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=tracer,
        conversation_channel=channel,
    )
    resources = create_autospec(ApplicationResources, instance=True)
    resources.tracer = tracer
    resources.conversation_channel = channel
    resources.conversation_trace = coordinator
    resources.notifications = notifications
    resources.agent_persistence = create_autospec(AgentPersistence, instance=True)
    resources.agent_persistence.checkpointer = InMemorySaver()

    async def settled_during_delete(*, identity: RunIdentity) -> str:
        await coordinator.reconcile(thread_pk=thread_pk, identity=identity)
        async with database.session() as check:
            marked = await ConversationRepository(check).get_thread_by_pk(thread_pk)
            assert marked is not None and marked.status == "deleting"
        return "failed"

    channel.get_run_status.side_effect = settled_during_delete
    try:
        async with database.session() as session:
            repository = ConversationRepository(session)
            command = ConversationCommandService(
                repository, user_id=7, resources=resources
            )
            if trace_exists:
                await command.delete(thread_id=identity.thread_id)
                assert await repository.get_thread_by_pk(thread_pk) is None
                channel.delete_stream.assert_awaited_once_with(identity=identity)
            else:
                with pytest.raises(SystemException) as captured:
                    await command.delete(thread_id=identity.thread_id)
                assert (
                    captured.value.error_code is ConversationErrorCode.TRACE_UNAVAILABLE
                )
                thread = await repository.get_thread_by_pk(thread_pk)
                assert thread is not None and thread.status == "running"
                channel.delete_stream.assert_not_awaited()
    finally:
        await coordinator.aclose()


async def test_delete_rejects_a_new_completed_head_after_authority_was_checked(
    database: Database,
    notifications: Notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = RunIdentity(namespace="ns_7", thread_id="delete-race", run_id="run")
    thread_pk = await _register(database, identity)
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    await _terminal(tracer, identity)
    channel = create_autospec(AgUiChannel, instance=True)
    coordinator = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=tracer,
        conversation_channel=channel,
    )
    resources = create_autospec(ApplicationResources, instance=True)
    resources.tracer = tracer
    resources.conversation_channel = channel
    resources.conversation_trace = coordinator
    try:
        async with database.session() as session:
            repository = ConversationRepository(session)
            original_lock = repository.lock_thread

            async def newer_head(target_pk: int):
                async with database.session() as other:
                    writer = ConversationRepository(other)
                    thread = await writer.get_thread_by_pk(target_pk)
                    assert thread is not None
                    run = await writer.create_run_registration(
                        thread_id=thread.id,
                        run_id="new-completed-run",
                        parent_run_id=None,
                        model_id="main",
                        input_json={"runId": "new-completed-run"},
                    )
                    run.status = run.terminal_outcome = "succeeded"
                    thread.status = "idle"
                    thread.last_run_id = run.run_id
                    await writer.commit()
                return await original_lock(target_pk)

            monkeypatch.setattr(repository, "lock_thread", newer_head)
            command = ConversationCommandService(
                repository, user_id=7, resources=resources
            )
            with pytest.raises(BusinessException) as captured:
                await command.delete(thread_id=identity.thread_id)
            assert captured.value.error_code is ConversationErrorCode.DELETE_CONFLICT
            channel.delete_stream.assert_not_awaited()
        assert (
            await tracer.get(identity.thread)
        ).summary.status.execution == "cancelled"
        async with database.session() as check:
            thread = await ConversationRepository(check).get_thread_by_pk(thread_pk)
            assert thread is not None and thread.last_run_id == "new-completed-run"
    finally:
        await coordinator.aclose()


async def _starting_registration(database, identity):
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-7",
            user_id=7,
            thread_id=identity.thread_id,
            title="关闭结算",
            model_id="main",
        )
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="main",
            input_json={"runId": identity.run_id},
        )
        await repository.activate_run_registration(
            thread_pk=thread.id, run_pk=run.id, run_id=run.run_id
        )
        await repository.commit()
        return thread.id


async def test_shutdown_before_first_summary_settles_starting(
    database, notifications, monkeypatch, fixed_utc_time
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-close", run_id="run")
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    original_get = tracer.get
    first_summary = asyncio.Event()

    async def pending_first_summary(*args, **kwargs):
        if not first_summary.is_set():
            first_summary.set()
            await asyncio.Event().wait()
        return await original_get(*args, **kwargs)

    monkeypatch.setattr(tracer, "get", pending_first_summary)
    model = _PausedModel(responses=[AIMessage(content="unused")])
    runtime = TinkerFin().with_namespace("ns_7").with_observer(tracer).build(model)
    stack = AsyncExitStack()
    _, gateway, coordinator = await _enter_conversation_resources(
        stack,
        _LifespanOutcome(),
        messaging=Messaging(),
        database=database,
        notifications=notifications,
        tracer=tracer,
    )
    thread_pk = await _starting_registration(database, identity)

    async def committed(event: CommittedRunEvent):
        if isinstance(event.event, RunStartedEvent):
            coordinator.ensure(thread_pk=thread_pk, identity=identity)

    try:
        await gateway.start(
            runtime,
            StartRun(
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                messages=({"id": "user", "role": "user", "content": "等待"},),
            ),
            on_committed=committed,
        )
        await model.started.wait()
        await first_summary.wait()
        await stack.aclose()
    finally:
        await stack.aclose()

    assert model.stopped.is_set()
    assert (await original_get(identity.thread)).status.execution == "cancelled"
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert thread is not None and run is not None
        after_shutdown = thread.status, run.status

    monkeypatch.setattr(tracer, "get", original_get)
    async with AsyncExitStack() as startup:
        await _enter_conversation_resources(
            startup,
            _LifespanOutcome(),
            messaging=Messaging(),
            database=database,
            notifications=notifications,
            tracer=tracer,
        )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert thread is not None and run is not None
        after_restart = thread.status, run.status
    assert (after_shutdown, after_restart) == (
        ("idle", "cancelled"),
        ("idle", "cancelled"),
    )


async def test_startup_tolerates_other_process_completed_delete(
    database, notifications, monkeypatch
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-delete", run_id="run")
    thread_pk = await _register(database, identity)
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    await _terminal(tracer, identity)
    channel = create_autospec(AgUiChannel, instance=True)
    channel.get_run_status.return_value = "failed"
    owner = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=tracer,
        conversation_channel=channel,
    )
    resources = create_autospec(ApplicationResources, instance=True)
    resources.tracer = tracer
    resources.conversation_trace = owner
    resources.conversation_channel = channel
    resources.notifications = notifications
    resources.agent_persistence = create_autospec(AgentPersistence, instance=True)
    resources.agent_persistence.checkpointer = InMemorySaver()
    original_list = ConversationRepository.list_pending_runs
    intercepted = False

    async def list_before_concurrent_delete(self, *, thread_pk=None):
        nonlocal intercepted
        rows = await original_list(self, thread_pk=thread_pk)
        if rows and not intercepted:
            intercepted = True
            await self.commit()
            async with database.session() as deleting_session:
                await ConversationCommandService(
                    ConversationRepository(deleting_session),
                    user_id=7,
                    resources=resources,
                ).delete(thread_id=identity.thread_id)
        return rows

    monkeypatch.setattr(
        ConversationRepository, "list_pending_runs", list_before_concurrent_delete
    )
    startup = AsyncExitStack()
    try:
        await _enter_conversation_resources(
            startup,
            _LifespanOutcome(),
            messaging=Messaging(),
            database=database,
            notifications=notifications,
            tracer=tracer,
        )
        async with database.session() as session:
            assert (
                await ConversationRepository(session).get_thread_by_pk(thread_pk)
                is None
            )
    finally:
        await startup.aclose()
        await owner.aclose()


@pytest.mark.parametrize("stale", [False, True])
async def test_missing_trace_preparing_with_live_owner_is_preserved(
    database, notifications, fixed_utc_time, stale
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-active", run_id="run")
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            project_id="project-7",
            user_id=7,
            thread_id=identity.thread_id,
            title="尚在准备",
            model_id="main",
        )
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="main",
            input_json={"runId": identity.run_id},
        )
        if stale:
            run.created_at = run.updated_at = fixed_utc_time.replace(
                tzinfo=None
            ) - timedelta(minutes=1)
        thread_pk = thread.id
        await repository.commit()
    channel = create_autospec(AgUiChannel, instance=True)
    channel.get_run_status.return_value = "running"
    coordinator = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=Tracer(projections=(ConversationFailureProjection(),)),
        conversation_channel=channel,
    )
    try:
        assert await coordinator.recover(thread_pk=thread_pk) == frozenset()
        async with database.session() as session:
            repository = ConversationRepository(session)
            assert await repository.get_thread_by_pk(thread_pk) is not None
            run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
            assert run is not None and run.status == "preparing"
    finally:
        await coordinator.aclose()


async def test_shutdown_cancellation_survives_summary_failure(
    database, notifications, monkeypatch
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-cancel", run_id="run")
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    messaging = Messaging()
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    await _enter_conversation_resources(
        stack,
        outcome,
        messaging=messaging,
        database=database,
        notifications=notifications,
        tracer=tracer,
    )
    await _register(database, identity)
    await _terminal(tracer, identity)
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = messaging._close_once

    async def closing():
        entered.set()
        await release.wait()
        await original_close()

    async def unavailable(*args, **kwargs):
        raise OSError("final summary failed")

    monkeypatch.setattr(messaging, "_close_once", closing)
    monkeypatch.setattr(tracer, "get", unavailable)
    closer = asyncio.create_task(_settle_lifespan_stack(stack, outcome))
    try:
        await entered.wait()
        closer.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
    finally:
        release.set()
        await asyncio.gather(closer, return_exceptions=True)
        await stack.aclose()


@pytest.mark.parametrize("execution_cancelled", [False, True])
@pytest.mark.parametrize("summary_failure", [False, True])
async def test_shutdown_keeps_follower_cancellation_cleanup_cause(
    database, notifications, monkeypatch, execution_cancelled, summary_failure
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-cause", run_id="run")
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    messaging = Messaging()
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    _, _, coordinator = await _enter_conversation_resources(
        stack,
        outcome,
        messaging=messaging,
        database=database,
        notifications=notifications,
        tracer=tracer,
    )
    thread_pk = await _register(database, identity)
    await _terminal(tracer, identity)
    entered = asyncio.Event()
    cleanup_failure = OSError("follow cleanup evidence")

    if execution_cancelled:
        original_close = messaging._close_once

        async def interrupted_execution_close():
            await original_close()
            raise asyncio.CancelledError("execution close cancellation")

        monkeypatch.setattr(messaging, "_close_once", interrupted_execution_close)

    async def cancelled_read(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            raise error from cleanup_failure

    monkeypatch.setattr(tracer, "get", cancelled_read)
    coordinator.ensure(thread_pk=thread_pk, identity=identity)
    await entered.wait()
    # 另一摘要写入者已结算业务状态，当前跟随仍在关闭外部读取
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert thread is not None and run is not None
        thread.status = "idle"
        run.status = run.terminal_outcome = "cancelled"
        await repository.commit()

    summary_error = OSError("final summary evidence")
    if summary_failure:

        async def unavailable() -> None:
            raise summary_error

        monkeypatch.setattr(coordinator, "settle", unavailable)

    try:
        with pytest.raises(asyncio.CancelledError) as captured:
            await _settle_lifespan_stack(stack, outcome)
        assert cleanup_failure in _exception_tree(captured.value)
        if summary_failure:
            assert summary_error in _exception_tree(captured.value)
        assert not any(
            task.get_name().startswith("studio-trace-summary")
            for task in asyncio.all_tasks()
            if not task.done()
        )
    finally:
        await stack.aclose()


def _exception_tree(error):
    seen = set()
    pending = [error]
    found = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
    return found


async def test_missing_trace_registration_lookup_can_recover(
    database, notifications, monkeypatch
):
    identity = RunIdentity(
        namespace="ns_7", thread_id="review-follow-retry", run_id="run"
    )
    thread_pk = await _starting_registration(database, identity)
    tracer = Tracer(projections=(ConversationFailureProjection(),))
    original_lookup = ConversationRepository.get_thread_by_pk
    failed = False

    async def fail_once(self, target_pk):
        nonlocal failed
        if not failed:
            failed = True
            await _terminal(tracer, identity)
            raise OSError("business lookup temporarily unavailable")
        return await original_lookup(self, target_pk)

    monkeypatch.setattr(ConversationRepository, "get_thread_by_pk", fail_once)
    monkeypatch.setattr(coordinator_module, "_FOLLOW_RETRY_INITIAL_SECONDS", 0)
    monkeypatch.setattr(coordinator_module, "_FOLLOW_RETRY_MAX_SECONDS", 0)
    coordinator = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=tracer,
        conversation_channel=create_autospec(AgUiChannel, instance=True),
    )
    coordinator.ensure(thread_pk=thread_pk, identity=identity)
    owned = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name() == f"studio-trace-summary:{identity.run_id}"
    )
    try:
        await owned
        async with database.session() as session:
            thread = await original_lookup(ConversationRepository(session), thread_pk)
            assert thread is not None and thread.status == "idle"
    finally:
        await coordinator.aclose()


async def test_missing_running_trace_fails_startup_without_leaks(
    database, notifications
):
    identity = RunIdentity(namespace="ns_7", thread_id="review-missing", run_id="run")
    thread_pk = await _register(database, identity)
    before = set(asyncio.all_tasks())
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    startup_failure = None
    try:
        await _enter_conversation_resources(
            stack,
            outcome,
            messaging=Messaging(),
            database=database,
            notifications=notifications,
            tracer=Tracer(projections=(ConversationFailureProjection(),)),
        )
    except TraceThreadNotFound as error:
        startup_failure = error
        outcome.capture(error)
    assert startup_failure is not None
    with pytest.raises(TraceThreadNotFound) as closed:
        await _settle_lifespan_stack(stack, outcome)
    assert closed.value is startup_failure
    assert not [task for task in asyncio.all_tasks() - before if not task.done()]
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert thread is not None and run is not None
        assert (thread.status, run.status) == ("running", "running")


pytestmark = pytest.mark.usefixtures("projects")
