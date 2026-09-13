from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from langchain.agents.middleware.types import InputAgentState

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin.plan import AgentMode
from tinkerfin_automation import (
    AutomationExecution,
    AutomationTarget,
    ExecutionInterrupted,
    ExecutionLimits,
    ExecutionOrigin,
    ExecutionRequest,
    ExecutionStatus,
    TargetExecutionError,
    TinkerFinTarget,
)
from tinkerfin_contracts import RunIdentity
from tinkerfin_native_stream import NativeRuntimeInterrupt


def _execution(now: datetime) -> AutomationExecution:
    return AutomationExecution(
        execution_id="execution-1",
        task_id="task-1",
        namespace="app",
        owner_id="owner-1",
        identity=RunIdentity(namespace="app", thread_id="thread-1", run_id="run-1"),
        target="runtime",
        input={"messages": []},
        limits=ExecutionLimits(),
        origin=ExecutionOrigin.MANUAL,
        status=ExecutionStatus.RUNNING,
        attempt=1,
        retry_of=None,
        scheduled_for=None,
        queued_at=now,
        queue_deadline=now + timedelta(hours=1),
        execution_started_at=now,
        execution_deadline=now + timedelta(minutes=30),
        finished_at=None,
        failure_code=None,
        failure_message=None,
        result=None,
        interrupt_ids=(),
        start_authorized_at=now,
        start_token="token",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_runtime_target_uses_ainvoke_and_returns_interrupt_without_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[RunIdentity, object, AgentMode | None]] = []

    async def fake_ainvoke(
        self: AgentRuntime[None],
        *,
        thread_id: str,
        run_id: str,
        input: InputAgentState | None,
        mode: AgentMode | None = None,
        **_options: object,
    ) -> Mapping[str, object]:
        calls.append((self.run_identity(thread_id, run_id), input, mode))
        return {
            "messages": [],
            "__interrupt__": [
                NativeRuntimeInterrupt(id="interrupt-1", value={"action_requests": []})
            ],
        }

    monkeypatch.setattr(AgentRuntime, "ainvoke", fake_ainvoke)
    runtime = TinkerFin().with_namespace("app").build(model="provider:model")
    target: AutomationTarget = TinkerFinTarget(runtime, mode="default")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    execution = _execution(now)

    result = await target.run(
        ExecutionRequest(execution=execution, deadline=now + timedelta(minutes=30))
    )

    assert result == ExecutionInterrupted(("interrupt-1",))
    assert calls == [(execution.identity, {"messages": []}, "default")]
    assert not target.cancellation_is_final


@pytest.mark.parametrize("identity_only", [False, True])
async def test_runtime_target_rejects_foreign_namespace_before_preparing_resources(
    identity_only: bool,
) -> None:
    runtime = TinkerFin().with_namespace("app").build(model="provider:model")
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    if identity_only:
        with pytest.raises(ValueError, match="identity.namespace"):
            replace(
                _execution(now),
                identity=RunIdentity(
                    namespace="other", thread_id="thread-1", run_id="run-1"
                ),
            )
        return
    execution = replace(
        _execution(now),
        namespace="other",
        identity=RunIdentity(namespace="other", thread_id="thread-1", run_id="run-1"),
    )
    with pytest.raises(TargetExecutionError, match="namespaces must match"):
        await TinkerFinTarget(runtime).run(
            ExecutionRequest(execution=execution, deadline=now + timedelta(minutes=30))
        )
