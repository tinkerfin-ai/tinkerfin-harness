from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import create_autospec

import pytest
from ag_ui.core import BaseEvent, RunStartedEvent

from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ThreadIdentity,
)
from tinkerfin_messaging import MessageChannel, Messaging, RunNotFound
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.errors import RunProducerFailed
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.models import ConversationRunRegistration
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import Tracer, TraceRunNotFound, TraceThread


async def _messaging_channel() -> tuple[
    Messaging,
    MessageChannel[BaseEvent, BaseEvent],
]:
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(
        name="studio-conversation-agui",
        codec=AgUiCodec(),
    )
    return messaging, channel


class _MissingRunBarrierChannel:
    """在 missing 观测后暂停，让 owner preflight 与回收删除精确交错"""

    def __init__(self) -> None:
        self.checked = asyncio.Event()
        self.release = asyncio.Event()

    async def get_run_status(self, *, identity: RunIdentity) -> str:
        self.checked.set()
        await self.release.wait()
        raise RunNotFound(identity=identity)


class _DelayedTraceLookup:
    """先报告目标 Run 尚未写入，再返回同一权威 Trace"""

    def __init__(self, trace: TraceThread) -> None:
        self._trace = trace
        self.attempts = 0

    async def get(
        self,
        identity: ThreadIdentity,
        *,
        head_run_id: str | None = None,
        projections: tuple[str, ...] = (),
    ) -> TraceThread:
        self.attempts += 1
        if self.attempts <= 2:
            raise TraceRunNotFound(
                "Selected Run does not exist in this Trace generation",
                context={"head_run_id": head_run_id},
            )
        assert identity == self._trace.key.thread
        assert head_run_id == self._trace.head_run_id
        return self._trace


async def _setup_run(database, *, thread_id: str, run_id: str):
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            thread_id=thread_id,
            title="协调器测试",
            model_id="model-main",
        )
        await repository.create_run_registration(
            thread_id=thread.id,
            run_id=run_id,
            parent_run_id=None,
            model_id="model-main",
            input_json={"runId": run_id},
        )
        thread.last_run_id = run_id
        thread.status = "running"
        await repository.commit()
        thread_pk = thread.id
    context = RunSourceContext(
        identity=RunIdentity(namespace="ns_1", thread_id=thread_id, run_id=run_id),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"id": "user-1", "role": "user", "content": "执行任务"}]},
        config={},
    )
    trace_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await trace_session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await trace_session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return tracer, context, trace_session, thread_pk


async def _thread(database, thread_pk: int):
    async with database.session() as session:
        return await ConversationRepository(session).get_thread_by_pk(thread_pk)


async def test_delayed_same_run_snapshot_cannot_restore_stale_running_status(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立协调器较晚提交的旧快照不能覆盖相同事件前缀的失活观测"""
    tracer, context, trace_session, thread_pk = await _setup_run(
        database, thread_id="thread-late-snapshot", run_id="run-late-snapshot"
    )
    old_view = await tracer.get(
        context.identity.thread, projections=("studio.conversation.failures",)
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    original_get = tracer.get

    async def delayed_get(
        identity: ThreadIdentity,
        *,
        head_run_id: str | None = None,
        projections: tuple[str, ...] = (),
    ):
        if not read_started.is_set():
            read_started.set()
            await release_read.wait()
            return old_view
        return await original_get(
            identity,
            head_run_id=head_run_id,
            projections=("studio.conversation.failures",),
        )

    monkeypatch.setattr(tracer, "get", delayed_get)
    messaging, channel = await _messaging_channel()
    delayed = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )
    current = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )
    pending = asyncio.create_task(
        delayed.reconcile(thread_pk=thread_pk, identity=context.identity)
    )
    try:
        await read_started.wait()
        await trace_session.aclose()
        await current.reconcile(thread_pk=thread_pk, identity=context.identity)
        closed = await _thread(database, thread_pk)
        assert closed is not None and closed.status == "error"
        release_read.set()
        await pending
        settled = await _thread(database, thread_pk)
        assert settled is not None and settled.status == "error"
    finally:
        release_read.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await current.aclose()
        await delayed.aclose()
        await messaging.aclose()


async def _finish(context, trace_session) -> None:
    now = datetime.now(UTC)
    await trace_session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await trace_session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await trace_session.aclose()


async def test_summary_timestamp_collision_requires_a_fresh_trace_observation(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同游标的不同内容不能直接覆盖，协调器必须取得更新的存储观测"""
    tracer, context, trace_session, thread_pk = await _setup_run(
        database,
        thread_id="thread-clock-collision",
        run_id="run-clock-collision",
    )
    await trace_session.aclose()
    collided = await tracer.get(
        context.identity.thread, projections=("studio.conversation.failures",)
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        await repository.update_trace_summary(
            thread_pk=thread_pk,
            run_id=context.identity.run_id,
            status="running",
            message_count=collided.summary.message_count,
            tool_call_count=collided.summary.tool_call_count,
            has_pending_interrupt=False,
            pending_interaction_kind=None,
            terminal_outcome=None,
            updated_at=collided.summary.last_occurred_at.replace(tzinfo=None),
            trace_generation=collided.key.generation,
            trace_as_of_seq=collided.as_of_seq,
            trace_observed_at=collided.observed_at.replace(tzinfo=None),
        )
        await repository.commit()
    original_get = tracer.get
    reads = 0

    async def get(
        identity: ThreadIdentity,
        *,
        head_run_id: str | None = None,
        projections: tuple[str, ...] = (),
    ) -> TraceThread:
        nonlocal reads
        reads += 1
        if reads == 1:
            return collided
        return await original_get(
            identity,
            head_run_id=head_run_id,
            projections=("studio.conversation.failures",),
        )

    monkeypatch.setattr(tracer, "get", get)
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )
    try:
        assert (
            await coordinator.reconcile(
                thread_pk=thread_pk,
                identity=context.identity,
            )
            == collided.as_of_seq
        )
        assert reads >= 2
        async with database.session() as session:
            repository = ConversationRepository(session)
            registration = await repository.get_run(
                thread_pk=thread_pk,
                run_id=context.identity.run_id,
            )
            thread = await repository.get_thread_by_pk(thread_pk)
            assert registration is not None and thread is not None
            assert registration.trace_observed_at is not None
            assert registration.trace_observed_at > collided.observed_at.replace(
                tzinfo=None
            )
            assert registration.terminal_outcome is None
            assert registration.finished_at is None
            assert thread.status == "error"
    finally:
        await coordinator.aclose()
        await messaging.aclose()


async def test_cancel_after_producer_failure_reconciles_missing_trace_tail(
    database,
) -> None:
    """已失败 producer 的停止请求返回未取消，并按 Trace 清除列表运行态"""
    tracer, context, trace_session, thread_pk = await _setup_run(
        database,
        thread_id="thread-failed-cancel",
        run_id="run-failed-cancel",
    )
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )

    class FailedSource:
        def __init__(self) -> None:
            self._stream = self._events()

        def __aiter__(self) -> AsyncIterator[BaseEvent]:
            return self._stream

        async def _events(self) -> AsyncGenerator[BaseEvent, None]:
            yield RunStartedEvent(
                thread_id=context.identity.thread_id,
                run_id=context.identity.run_id,
            )
            raise RuntimeError("producer stopped")

        async def aclose(self) -> None:
            await self._stream.aclose()

    try:
        subscription = await channel.wrap(FailedSource(), identity=context.identity)
        with pytest.raises(RunProducerFailed):
            async for _message in subscription:
                pass
        await subscription.aclose()
        await trace_session.aclose()
        resources = create_autospec(ApplicationResources, instance=True)
        resources.conversation_channel = channel
        resources.conversation_trace = coordinator
        async with database.session() as session:
            service = ConversationChatService(
                session,
                user=UserContext(
                    user_id=1,
                    username="user",
                    display_name="用户",
                    roles=(),
                    disabled=False,
                ),
                resources=resources,
            )
            response = await service.cancel(
                thread_id=context.identity.thread_id,
                run_id=context.identity.run_id,
            )
        assert response.cancelled is False
        current = await _thread(database, thread_pk)
        assert current is not None and current.status == "error"
        assert (
            await tracer.get(
                context.identity.thread,
                projections=("studio.conversation.failures",),
            )
        ).status.execution == "unknown"
    finally:
        await trace_session.aclose()
        await coordinator.aclose()
        await messaging.aclose()


async def test_recover_preparing_deletes_empty_thread_without_trace(database) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            thread_id="thread-stale",
            title="过期会话",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id="run-stale",
            parent_run_id=None,
            model_id="model-main",
            input_json={"runId": "run-stale"},
        )
        registration.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            minutes=1
        )
        thread.last_run_id = registration.run_id
        await repository.commit()
        thread_pk = thread.id
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )

    await coordinator.recover_preparing(thread_pk=thread_pk)

    assert await _thread(database, thread_pk) is None
    await coordinator.aclose()
    await messaging.aclose()


async def test_recover_preparing_removes_only_a_missing_new_run_from_existing_trace(
    database,
) -> None:
    tracer, old_context, old_session, thread_pk = await _setup_run(
        database,
        thread_id="thread-existing-trace",
        run_id="run-existing-trace",
    )
    await _finish(old_context, old_session)
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        assert thread is not None
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id="run-missing-trace",
            parent_run_id="run-existing-trace",
            model_id="model-main",
            input_json={"runId": "run-missing-trace"},
        )
        registration.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            minutes=1
        )
        thread.last_run_id = registration.run_id
        await repository.commit()

    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )

    await coordinator.recover_preparing(thread_pk=thread_pk)

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        missing = await repository.get_run(
            thread_pk=thread_pk,
            run_id="run-missing-trace",
        )
        existing = await repository.get_run(
            thread_pk=thread_pk,
            run_id="run-existing-trace",
        )
    assert thread is not None
    assert missing is None
    assert existing is not None
    await coordinator.aclose()
    await messaging.aclose()


async def test_owner_preflight_cas_fences_a_stale_recovery_delete(database) -> None:
    """missing 检查后的 owner 激活必须让延迟删除 CAS 失效"""

    identity = RunIdentity(
        namespace="ns_1", thread_id="thread-preflight-race", run_id="run-preflight-race"
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            thread_id=identity.thread_id,
            title="激活竞态",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="model-main",
            input_json={"runId": identity.run_id},
        )
        registration.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            minutes=1
        )
        thread.last_run_id = identity.run_id
        await repository.commit()
        thread_pk = thread.id
        run_pk = registration.id

    barrier = _MissingRunBarrierChannel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=Tracer(
            projections=(ConversationFailureProjection(),),
        ),
        conversation_channel=cast(
            MessageChannel[BaseEvent, BaseEvent],
            barrier,
        ),
    )
    recovery = asyncio.create_task(coordinator.recover_preparing(thread_pk=thread_pk))
    await asyncio.wait_for(barrier.checked.wait(), timeout=2)
    async with database.session() as session:
        repository = ConversationRepository(session)
        assert await repository.activate_run_registration(
            thread_pk=thread_pk,
            run_pk=run_pk,
            run_id=identity.run_id,
        )
        await repository.commit()
    barrier.release.set()
    await asyncio.wait_for(recovery, timeout=2)

    async with database.session() as session:
        repository = ConversationRepository(session)
        run = await repository.get_run(thread_pk=thread_pk, run_id=identity.run_id)
        assert run is not None
        assert run.status == "starting"

    await coordinator.aclose()


async def test_abandoned_trace_settles_the_complete_claim_batch_as_cancelled(
    database,
) -> None:
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    identity = RunIdentity(
        namespace="ns_1", thread_id="thread-abandon", run_id="run-abandon"
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            thread_id=identity.thread_id,
            title="放弃恢复",
            model_id="model-main",
        )
        registration = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id="run-interrupted",
            model_id="model-main",
            input_json={"runId": identity.run_id},
        )
        await repository.create_interrupt_claims(
            thread_pk=thread.id,
            source_run_id="run-interrupted",
            claimed_run_id=identity.run_id,
            interrupt_ids=("interrupt-1", "interrupt-2"),
        )
        thread.last_run_id = identity.run_id
        await repository.commit()
        thread_pk = thread.id
        registration_pk = registration.id
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="abandon",
        parent_run_id="run-interrupted",
        input={},
        config={},
        resume=(
            RunResumeSummary(interrupt_id="interrupt-1", status="cancelled"),
            RunResumeSummary(interrupt_id="interrupt-2", status="cancelled"),
        ),
    )
    trace_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await trace_session.observe(
        RunStartedObservation(
            identity=identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await trace_session.observe(
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    await trace_session.observe(
        RunTerminalObservation(
            identity=identity,
            outcome="abandoned",
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        RunClosedObservation(
            identity=identity,
            outcome="abandoned",
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await trace_session.aclose()
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database,
        tracer=tracer,
        conversation_channel=channel,
    )

    await coordinator.reconcile(thread_pk=thread_pk, identity=identity)

    async with database.session() as session:
        repository = ConversationRepository(session)
        claims = await repository.list_claims_for_update(
            thread_pk=thread_pk,
            interrupt_ids=frozenset({"interrupt-1", "interrupt-2"}),
        )
        stored_registration = await session.get(
            ConversationRunRegistration,
            registration_pk,
        )
    assert {claim.status for claim in claims} == {"cancelled"}
    assert all(claim.resolution_id is not None for claim in claims)
    assert stored_registration is not None
    assert stored_registration.status == "abandoned"
    await coordinator.aclose()
    await messaging.aclose()


async def test_initialization_error_code_is_persisted(database):
    tracer, context, source, thread_pk = await _setup_run(
        database, thread_id="thread-setup-error", run_id="run-setup-error"
    )
    await source.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="failed",
            code="runtime_initialization_error",
            error_type="builtins.RuntimeError",
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await source.aclose()
    messaging, channel = await _messaging_channel()
    coordinator = ConversationTraceCoordinator(
        database=database, tracer=tracer, conversation_channel=channel
    )
    try:
        await coordinator.reconcile(thread_pk=thread_pk, identity=context.identity)
        async with database.session() as session:
            run = await ConversationRepository(session).get_run(
                thread_pk=thread_pk, run_id=context.identity.run_id
            )
            assert run is not None
            assert run.error_code == "runtime_initialization_error"
            assert run.terminal_outcome == "failed"
    finally:
        await coordinator.aclose()
        await messaging.__aexit__(None, None, None)
