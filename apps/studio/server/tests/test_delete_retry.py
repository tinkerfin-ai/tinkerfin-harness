"""Trace 权威跨存储删除失败后的幂等重试契约"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import create_autospec

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import RunIdentity, TinkerFinLifecycleError
from tinkerfin_contracts import (
    RunClosedObservation,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ThreadIdentity,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.command import ConversationCommandService
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import Tracer, TraceThreadNotFound


async def _completed_trace(tracer: Tracer, identity: RunIdentity) -> None:
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"id": "user-1", "role": "user", "content": "删除"}]},
        config={},
    )
    trace_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    for observation in (
        RunStartedObservation(
            identity=identity,
            observed_at=now,
            monotonic_ns=1,
        ),
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=3,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=4,
        ),
    ):
        await trace_session.observe(observation)
    await trace_session.aclose()


@pytest.mark.parametrize("failure_stage", ["checkpoint", "messaging", "database"])
async def test_delete_retries_each_destructive_stage_without_restoring_old_authority(
    notifications,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id=f"thread-delete-{failure_stage}",
        title="删除重试",
        model_id="main",
    )
    registration = await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-delete",
        parent_run_id=None,
        model_id="main",
        input_json={"runId": "run-delete"},
    )
    registration.status = "succeeded"
    registration.terminal_outcome = "succeeded"
    thread.last_run_id = registration.run_id
    thread.status = "idle"
    await repository.commit()
    thread_pk = thread.id
    thread_id = thread.thread_id
    identity = RunIdentity(
        namespace="ns_7", thread_id=thread_id, run_id=registration.run_id
    )
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    await _completed_trace(tracer, identity)
    calls = {"messaging": 0, "checkpoint": 0, "database": 0}

    class Channel:
        async def get_run_status(self, *, identity: RunIdentity):
            del identity
            return "completed"

        async def delete_stream(self, *, identity: RunIdentity) -> None:
            del identity
            calls["messaging"] += 1
            if failure_stage == "messaging" and calls["messaging"] == 1:
                raise RuntimeError("messaging delete failed")

    class Checkpointer(InMemorySaver):
        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            calls["checkpoint"] += 1
            if failure_stage == "checkpoint" and calls["checkpoint"] == 1:
                raise RuntimeError("checkpoint delete failed")

    original_delete = repository.delete_thread_cascade

    async def delete_database(target_thread_pk: int) -> None:
        calls["database"] += 1
        if failure_stage == "database" and calls["database"] == 1:
            raise RuntimeError("database delete failed")
        await original_delete(target_thread_pk)

    monkeypatch.setattr(repository, "delete_thread_cascade", delete_database)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            conversation_trace=create_autospec(
                ConversationTraceCoordinator, instance=True
            ),
            tracer=tracer,
            notifications=notifications,
            conversation_channel=Channel(),
            agent_persistence=SimpleNamespace(checkpointer=Checkpointer()),
        ),
    )
    service = ConversationCommandService(repository, user_id=7, resources=resources)

    with pytest.raises(RuntimeError) as failure:
        await service.delete(thread_id=thread_id)
    if failure_stage == "checkpoint":
        assert isinstance(failure.value, TinkerFinLifecycleError)
        assert isinstance(failure.value.cause, RuntimeError)
        assert str(failure.value.cause) == "checkpoint delete failed"
    else:
        assert str(failure.value) == f"{failure_stage} delete failed"

    retained = await repository.get_thread_by_pk(thread_pk)
    assert retained is not None
    assert retained.status == "deleting"
    with pytest.raises(TraceThreadNotFound):
        await tracer.get(ThreadIdentity(namespace="ns_7", thread_id=thread_id))

    await service.delete(thread_id=thread_id)

    assert await repository.get_thread_by_pk(thread_pk) is None
    assert calls["messaging"] >= 1
    assert calls["checkpoint"] >= 1
    assert calls["database"] >= 1


async def test_delete_refuses_an_active_trace_and_restores_summary_status(
    notifications,
    session: AsyncSession,
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-delete-active",
        title="运行中",
        model_id="main",
    )
    registration = await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-active",
        parent_run_id=None,
        model_id="main",
        input_json={"runId": "run-active"},
    )
    registration.status = "waiting"
    thread.last_run_id = registration.run_id
    thread.status = "waiting_approval"
    await repository.commit()
    tracer = Tracer(
        projections=(ConversationFailureProjection(),),
    )
    identity = RunIdentity(
        namespace="ns_7", thread_id=thread.thread_id, run_id=registration.run_id
    )
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    active_session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await active_session.observe(
        RunStartedObservation(
            identity=identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await active_session.observe(
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )

    class Channel:
        async def get_run_status(self, *, identity: RunIdentity):
            del identity
            return "completed"

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            conversation_trace=create_autospec(
                ConversationTraceCoordinator, instance=True
            ),
            tracer=tracer,
            conversation_channel=Channel(),
            notifications=notifications,
        ),
    )
    service = ConversationCommandService(repository, user_id=7, resources=resources)

    with pytest.raises(BusinessException) as captured:
        await service.delete(thread_id=thread.thread_id)

    assert captured.value.error_code is ConversationErrorCode.DELETE_CONFLICT
    stored = await repository.get_thread_by_pk(thread.id)
    assert stored is not None
    assert stored.status == "waiting_approval"
    await active_session.aclose()
