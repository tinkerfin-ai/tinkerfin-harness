"""任务轨迹 public Trace 查询测试"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.todo_groups import (
    TaskTraceSnapshot,
    TodoGroupQueryExecutor,
)
from tinkerfin_tracing import (
    CapturedValue,
    InMemoryTraceStore,
    MessageFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    Tracer,
    TraceSemanticFact,
    TraceThread,
    TraceWriter,
    TurnFact,
)

_IDENTITY = RunIdentity(
    namespace="test", thread_id="thread:first-success", run_id="run-1"
)
_OCCURRED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _capture(value: JsonValue) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ),
        value=value,
    )


def _trace_facts() -> tuple[TraceSemanticFact, ...]:
    def occurred(seconds: int) -> datetime:
        return _OCCURRED_AT + timedelta(seconds=seconds)

    return (
        RunFact(
            source_observation_id="observation:run-started",
            identity=_IDENTITY,
            occurred_at=occurred(0),
            monotonic_ns=1,
            phase="started",
            input_kind="ordinary",
        ),
        TurnFact(
            source_observation_id="observation:turn-started",
            identity=_IDENTITY,
            occurred_at=occurred(1),
            monotonic_ns=2,
            turn_id="turn:run-1",
            user_message_id="source-user:run-1",
        ),
        MessageFact(
            source_observation_id="observation:user-message",
            identity=_IDENTITY,
            occurred_at=occurred(2),
            monotonic_ns=3,
            phase="reconciled",
            message_id="message:run-1",
            source_message_id="source-user:run-1",
            role="user",
            content=_capture("实现任务轨迹"),
        ),
        ToolFact(
            source_observation_id="observation:tool-started",
            identity=_IDENTITY,
            occurred_at=occurred(3),
            monotonic_ns=4,
            phase="started",
            tool_call_id="tool:root:call-1",
            source_tool_call_id="call-1",
            tool_name="write_todos",
        ),
        ToolFact(
            source_observation_id="observation:tool-result",
            identity=_IDENTITY,
            occurred_at=occurred(5),
            monotonic_ns=5,
            phase="result",
            tool_call_id="tool:root:call-1",
            source_tool_call_id="call-1",
            tool_name="write_todos",
            content=_capture("ok"),
            result_status="success",
        ),
        StateRevisionFact(
            source_observation_id="observation:state",
            identity=_IDENTITY,
            occurred_at=occurred(7),
            monotonic_ns=6,
            revision_id="revision:run-1",
            changes=_capture(
                {
                    "todos": [
                        {
                            "id": "todo-1",
                            "content": "实现 Server",
                            "status": "in_progress",
                        },
                        {
                            "id": "todo-2",
                            "content": "实现 Web",
                            "status": "pending",
                        },
                    ]
                }
            ),
        ),
    )


def _expected_snapshot() -> TaskTraceSnapshot:
    return TaskTraceSnapshot.model_validate_json(
        json.dumps(
            {
                "status": "ready",
                "todoGroups": [
                    {
                        "id": "todo-group:run-1",
                        "userMessageId": "message:run-1",
                        "userMessagePreview": "实现任务轨迹",
                        "groupToolCallId": "tool:root:call-1",
                        "createdAt": "2026-08-30T12:00:03.000Z",
                        "status": "running",
                        "todos": [
                            {
                                "id": "todo-1",
                                "content": "实现 Server",
                                "status": "running",
                            },
                            {
                                "id": "todo-2",
                                "content": "实现 Web",
                                "status": "pending",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )


async def _tracer_with_facts() -> tuple[Tracer, TraceWriter]:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_IDENTITY)
    await writer.append(_trace_facts())
    return Tracer(projections=(ConversationFailureProjection(),), store=store), writer


async def test_query_executor_projects_a_fixed_public_trace_prefix() -> None:
    tracer, writer = await _tracer_with_facts()
    trace = await tracer.get(
        ThreadIdentity(namespace="test", thread_id="thread:first-success"),
        head_run_id="run-1",
    )
    executor = TodoGroupQueryExecutor(capacity=1)

    projector = await executor.project(trace)
    try:
        assert (
            projector.snapshot(
                status=trace.status,
                completeness=trace.completeness,
            )
            == _expected_snapshot()
        )
        assert executor.borrowed_tokens == 0
    finally:
        projector.close()
        await writer.aclose()


class _BlockingTrace:
    def __init__(self) -> None:
        self.entered = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def events(self, **_kwargs: object) -> object:
        self.entered += 1
        self.first_entered.set()
        await self.release.wait()
        return SimpleNamespace(items=(), next_cursor=None)


async def test_query_limiter_only_bounds_initial_replay() -> None:
    trace = _BlockingTrace()
    executor = TodoGroupQueryExecutor(capacity=1, timeout_seconds=1)

    first = asyncio.create_task(executor.project(cast(TraceThread, trace)))
    await asyncio.wait_for(trace.first_entered.wait(), timeout=1)
    second = asyncio.create_task(executor.project(cast(TraceThread, trace)))
    await asyncio.sleep(0)

    assert trace.entered == 1
    assert executor.borrowed_tokens == 1
    trace.release.set()
    first_projector, second_projector = await asyncio.gather(first, second)
    try:
        assert trace.entered == 2
        assert executor.borrowed_tokens == 0
    finally:
        first_projector.close()
        second_projector.close()
