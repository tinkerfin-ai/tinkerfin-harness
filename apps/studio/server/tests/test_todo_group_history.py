"""任务轨迹 public Trace 查询测试"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.todo_groups import (
    TODO_PROJECTION,
    TaskTraceSnapshot,
    TodoGroupProjection,
    TodoGroupProjectionResult,
    render_task_trace,
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
    return Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection()),
        store=store,
    ), writer


async def test_registered_projection_renders_the_fixed_trace_prefix() -> None:
    tracer, writer = await _tracer_with_facts()
    try:
        for _ in range(2):
            trace = await tracer.get(
                ThreadIdentity(namespace="test", thread_id="thread:first-success"),
                head_run_id="run-1",
                projections=(TODO_PROJECTION,),
            )
            assert (
                render_task_trace(
                    TodoGroupProjectionResult.model_validate(
                        trace.projections[TODO_PROJECTION]
                    ),
                    status=trace.status,
                    completeness=trace.completeness,
                )
                == _expected_snapshot()
            )

        start = await tracer.get(
            ThreadIdentity(namespace="test", thread_id="thread:first-success"),
            head_run_id="run-1",
            at_run_start=True,
            projections=(TODO_PROJECTION,),
        )
        assert render_task_trace(
            TodoGroupProjectionResult.model_validate(
                start.projections[TODO_PROJECTION]
            ),
            status=start.status,
            completeness=start.completeness,
        ) == TaskTraceSnapshot(status="ready", todo_groups=())

        without_todos = await tracer.get(
            ThreadIdentity(namespace="test", thread_id="thread:first-success"),
            head_run_id="run-1",
        )
        assert TODO_PROJECTION not in without_todos.projections
    finally:
        await writer.aclose()


async def test_todo_follow_applies_facts_then_calibrates_writer_loss() -> None:
    tracer, writer = await _tracer_with_facts()
    try:
        trace = await tracer.get(
            ThreadIdentity(namespace="test", thread_id="thread:first-success"),
            head_run_id="run-1",
            projections=(TODO_PROJECTION,),
        )
        initial = TodoGroupProjectionResult.model_validate(
            trace.projections[TODO_PROJECTION]
        )
        async with trace.follow() as updates:
            await writer.append(
                (
                    StateRevisionFact(
                        source_observation_id="state-progress",
                        identity=_IDENTITY,
                        occurred_at=_OCCURRED_AT + timedelta(seconds=9),
                        monotonic_ns=7,
                        revision_id="state-progress",
                        changes=_capture(
                            {
                                "todos": [
                                    {
                                        "id": "todo-1",
                                        "content": "实现 Server",
                                        "status": "completed",
                                    },
                                    {
                                        "id": "todo-2",
                                        "content": "实现 Web",
                                        "status": "in_progress",
                                    },
                                ]
                            }
                        ),
                    ),
                )
            )
            progress = await anext(updates)
            projected = TodoGroupProjectionResult.model_validate(
                progress.projections[TODO_PROJECTION]
            )
            visible = render_task_trace(
                projected,
                status=progress.summary.status,
                completeness=progress.summary.completeness,
            )
            assert [todo.status for todo in visible.todo_groups[0].todos] == [
                "completed",
                "running",
            ]

            await writer.aclose()
            lost = await anext(updates)
            assert lost.as_of_seq == progress.as_of_seq
            assert not lost.events
            lost_result = TodoGroupProjectionResult.model_validate(
                lost.projections[TODO_PROJECTION]
            )
            assert lost_result == projected
            lost_view = render_task_trace(
                lost_result,
                status=lost.summary.status,
                completeness=lost.summary.completeness,
            )
            assert lost_view.todo_groups[0].status == "failed"
            assert [todo.status for todo in lost_view.todo_groups[0].todos] == [
                "completed",
                "failed",
            ]
        assert (
            render_task_trace(
                initial, status=trace.status, completeness=trace.completeness
            )
            == _expected_snapshot()
        )
    finally:
        await writer.aclose()


@pytest.mark.parametrize("cancelled", ["owner", "waiter"])
async def test_initial_admission_releases_capacity_on_cancellation(
    cancelled: Literal["owner", "waiter"],
) -> None:
    admission = HistoryQueryAdmission(capacity=1)
    owner_entered = asyncio.Event()
    waiter_attempted = asyncio.Event()
    waiter_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_capacity(*, owner: bool) -> None:
        if not owner:
            waiter_attempted.set()
        async with admission.admit():
            (owner_entered if owner else waiter_entered).set()
            await release.wait()

    owner = asyncio.create_task(hold_capacity(owner=True))
    tasks = [owner]
    try:
        await owner_entered.wait()
        waiter = asyncio.create_task(hold_capacity(owner=False))
        tasks.append(waiter)
        await waiter_attempted.wait()
        assert admission.borrowed_tokens == 1
        assert not waiter_entered.is_set()
        victim = owner if cancelled == "owner" else waiter
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        if cancelled == "owner":
            await waiter_entered.wait()
        else:
            assert not waiter_entered.is_set()
        assert admission.borrowed_tokens == 1
        release.set()
        await (waiter if cancelled == "owner" else owner)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert admission.borrowed_tokens == 0
    async with admission.admit():
        assert admission.borrowed_tokens == 1
    assert admission.borrowed_tokens == 0
