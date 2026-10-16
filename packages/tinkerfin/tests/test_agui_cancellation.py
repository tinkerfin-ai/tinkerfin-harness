"""Abandoned approval requests replay without executing or splitting history."""

from __future__ import annotations

import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_agui_history import _ToolModel, save_report

from tinkerfin import AgUiResumeReceipt, AgUiResumeRequest, TinkerFin
from tinkerfin_tracing import InMemoryTraceStore, Tracer


def _proposal(kind: str) -> AIMessage:
    if kind in {"tool", "tools"}:
        return AIMessage(
            content="",
            tool_calls=[
                {"id": f"save-call-{index}", "name": "save_report", "args": {}}
                for index in range(2 if kind == "tools" else 1)
            ],
        )
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "plan-call",
                "name": "submit_plan" if kind == "review" else "ask_user_question",
                "args": {
                    "content": {
                        "goal": "Prepare a report",
                        "assumptions": [],
                        "steps": [
                            {
                                "id": "prepare",
                                "title": "Prepare",
                                "description": "Check the sources",
                                "verification": ["Totals match"],
                            }
                        ],
                        "acceptance_criteria": ["The report reconciles"],
                    }
                }
                if kind == "review"
                else {
                    "form": {
                        "questions": [
                            {
                                "id": "audience",
                                "answer_type": "text",
                                "prompt": "Who will read the report?",
                                "required": True,
                            }
                        ]
                    }
                },
            }
        ],
    )


@pytest.mark.parametrize("kind", ["review", "clarification", "tool", "tools"])
@pytest.mark.parametrize("explicit_parent", [False, True])
async def test_abandonment_replays_and_preserves_one_history_through_a_later_reply(
    kind: str, explicit_parent: bool
) -> None:
    saver = InMemorySaver()
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("cancellation")
        .with_observer(tracer)
        .with_plan()
        .build(
            model=_ToolModel(
                responses=[_proposal(kind), AIMessage(content="Complete")]
            ),
            tools=[save_report],
            interrupt_on={"save_report": True},
        )
    )
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id="thread",
            run_id="before",
            mode="default" if kind in {"tool", "tools"} else "plan",
            messages=[{"id": "user", "role": "user", "content": "Prepare a report"}],
        )
    ]
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt_ids = tuple(item.id for item in terminal.outcome.interrupts)
    reader = runtime.agui.history(tracer)
    original = (await reader.get("thread")).snapshot.interactions[0]
    parent = "before" if explicit_parent else None
    cancel = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": interrupt_id, "status": "cancelled"}
                for interrupt_id in interrupt_ids
            ]
        }
    )
    saved: list[AgUiResumeReceipt] = []

    async def record_saved(receipt: AgUiResumeReceipt) -> None:
        saved.append(receipt)

    for run_id in ("cancel-first", "cancel-second"):
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            parent_run_id=parent,
            resume=cancel,
            on_resume_saved=record_saved,
        )
        cancelled = [event async for event in stream]
        assert stream.error is None
        assert [event.type.value for event in cancelled] == ["RUN_STARTED", "RUN_ERROR"]
        assert isinstance(cancelled[-1], RunErrorEvent)
        assert cancelled[-1].code == "resume_cancelled"
        history = (await reader.get("thread")).snapshot
        assert history.available_heads == (run_id,)
        settled = next(item for item in history.interactions if item.id == original.id)
        assert settled.kind == original.kind
        assert settled.agui == (() if kind in {"tool", "tools"} else original.agui)
        assert settled.payload == original.payload
        assert settled.status == "cancelled"
        before_replay = history.model_dump(exclude={"observed_at"})
        replay = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            parent_run_id=parent,
            resume=AgUiResumeRequest(entries=tuple(reversed(cancel.entries))),
            on_resume_saved=record_saved,
        )
        repeated = [event async for event in replay]
        assert replay.error is None
        assert isinstance(repeated[-1], RunErrorEvent)
        assert repeated[-1].code == "resume_cancelled"
        assert (await reader.get("thread")).snapshot.model_dump(
            exclude={"observed_at"}
        ) == before_replay
    assert saved == []

    answer = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"}
                    if kind in {"tool", "tools"}
                    else {"type": "approve", "baseRevision": 1}
                    if kind == "review"
                    else {
                        "type": "respond",
                        "answers": {
                            "audience": {
                                "status": "answered",
                                "answerType": "text",
                                "answer": "The finance team",
                            }
                        },
                    },
                }
                for interrupt_id in interrupt_ids
            ]
        }
    )
    changed = runtime.open_agui_run(
        thread_id="thread", run_id="cancel-first", parent_run_id=parent, resume=answer
    )
    invalid = [event async for event in changed]
    assert isinstance(invalid[-1], RunErrorEvent)
    assert invalid[-1].code == "runtime_initialization_error"
    responded = runtime.open_agui_run(
        thread_id="thread",
        run_id="answer",
        parent_run_id=parent,
        resume=answer,
        on_resume_saved=record_saved,
    )
    result = [event async for event in responded]
    assert responded.error is None
    assert isinstance(result[-1], RunFinishedEvent)
    assert len(saved) == 1
    final = (await reader.get("thread")).snapshot
    assert final.available_heads == ("answer",)
    assert final.interactions[0].kind == original.kind
    assert final.interactions[0].agui == (
        () if kind in {"tool", "tools"} else original.agui
    )
    assert final.interactions[0].status == "resolved"
    if kind in {"tool", "tools"}:
        tool_messages = [
            message for message in final.messages if message.role == "tool"
        ]
        assert len(tool_messages) == len(interrupt_ids)
        assert all(message.content == "Report saved" for message in tool_messages)
        assert all(not message.content_omitted for message in tool_messages)

    restarted_tracer = Tracer(store=store)
    restarted = (
        TinkerFin(checkpointer=saver)
        .with_namespace("cancellation")
        .with_observer(restarted_tracer)
        .with_plan()
        .build(model=_ToolModel(responses=[AIMessage(content="Must not execute")]))
    )
    replay = restarted.open_agui_run(
        thread_id="thread", run_id="cancel-first", parent_run_id=parent, resume=cancel
    )
    repeated = [event async for event in replay]
    assert replay.error is None
    assert isinstance(repeated[-1], RunErrorEvent)
    assert repeated[-1].code == "resume_cancelled"
    assert (
        await restarted.agui.history(restarted_tracer).get("thread")
    ).snapshot.model_dump(exclude={"observed_at"}) == final.model_dump(
        exclude={"observed_at"}
    )
