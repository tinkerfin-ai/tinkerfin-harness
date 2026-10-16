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
from pydantic import Field

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
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
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
