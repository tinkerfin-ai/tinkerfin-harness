"""Mixed resolved and cancelled Deep Agents Tool resume contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import pytest
from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    ToolCallResultEvent,
)
from ag_ui.core.types import ResumeEntry
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from tinkerfin import (
    AgUiResumeBinding,
    AgUiResumeRequest,
    RunIdentity,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin._hitl import HITL_CONTRACT_ID
from tinkerfin_agui_adapter import ResumeMapper, ScopedIdCodec


class _ToolBindingModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


def _terminal(events: Sequence[BaseEvent]) -> RunFinishedEvent:
    terminals = [event for event in events if isinstance(event, RunFinishedEvent)]
    assert len(terminals) == 1
    return terminals[0]


def _interrupt_outcome(
    events: Sequence[BaseEvent],
) -> RunFinishedInterruptOutcome:
    outcome = _terminal(events).outcome
    assert isinstance(outcome, RunFinishedInterruptOutcome)
    return outcome


def _assert_success(events: Sequence[BaseEvent]) -> None:
    assert isinstance(_terminal(events).outcome, RunFinishedSuccessOutcome)


@pytest.mark.asyncio
async def test_mixed_resume_executes_resolved_tool_and_settles_cancelled_tool_once() -> (
    None
):
    approved_calls: list[str] = []
    cancelled_calls: list[str] = []

    @tool
    async def approved_tool(value: str) -> str:
        """Record one approved Tool execution."""

        approved_calls.append(value)
        return f"approved:{value}"

    @tool
    async def cancelled_tool(value: str) -> str:
        """Fail the test if a cancelled Tool reaches execution."""

        cancelled_calls.append(value)
        return f"cancelled:{value}"

    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "A"},
                        "id": "call-approved",
                        "type": "tool_call",
                    },
                    {
                        "name": "cancelled_tool",
                        "args": {"value": "B"},
                        "id": "call-cancelled",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="done", id="final-message"),
        ]
    )
    saver = InMemorySaver()
    definition = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .build(
            model=model,
            tools=[approved_tool, cancelled_tool],
            subagents=cast(Any, [_external_subagent(declared=False)]),
            interrupt_on={"approved_tool": True, "cancelled_tool": True},
        )
    )
    first_identity = RunIdentity(
        namespace="test", thread_id="thread-mixed", run_id="run-review"
    )
    first_runtime = definition
    first_events = [
        event
        async for event in first_runtime.open_agui_run(
            thread_id=first_identity.thread_id,
            run_id=first_identity.run_id,
            input={"messages": [HumanMessage(content="Run both tools")]},
        )
    ]
    interrupts = tuple(_interrupt_outcome(first_events).interrupts)
    assert len(interrupts) == 2

    entries = (
        ResumeEntry.model_validate(
            {
                "interruptId": interrupts[1].id,
                "status": "cancelled",
            }
        ),
        ResumeEntry.model_validate(
            {
                "interruptId": interrupts[0].id,
                "status": "resolved",
                "payload": {"type": "approve"},
            }
        ),
    )
    translation = ResumeMapper().map_agui(
        entries=entries,
        interrupts=interrupts,
    )
    assert translation.mode == "custom"
    assert translation.kind == "tool"

    resume_identity = RunIdentity(
        namespace="test", thread_id="thread-mixed", run_id="run-resume"
    )
    binding = AgUiResumeBinding.from_agui(
        entries=entries,
        interrupts=interrupts,
    )
    assert binding.contains_cancellations is True
    assert len(binding.prior_tool_call_ids) == 2
    native_parts: list[Mapping[str, object]] = []

    async def observe(part: Mapping[str, object]) -> None:
        native_parts.append(part)

    resume_runtime = definition
    resumed_events = [
        event
        async for event in resume_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            resume=AgUiResumeRequest(entries=entries),
            on_native_part=observe,
        )
    ]

    _assert_success(resumed_events)
    assert approved_calls == ["A"]
    assert cancelled_calls == []
    results = [
        event for event in resumed_events if isinstance(event, ToolCallResultEvent)
    ]
    assert {event.tool_call_id for event in results} == {
        interrupt.tool_call_id for interrupt in interrupts
    }
    cancelled_messages = [
        message
        for part in native_parts
        if part.get("type") == "values" and part.get("ns") == ()
        if isinstance((data := part.get("data")), Mapping)
        for message in data.get("messages", ())
        if isinstance(message, ToolMessage) and message.tool_call_id == "call-cancelled"
    ]
    assert cancelled_messages
    cancelled_message = cancelled_messages[-1]
    assert cancelled_message.status == "error"
    assert cancelled_message.additional_kwargs["tinkerfin"] == {
        "schema": HITL_CONTRACT_ID,
        "outcome": "cancelled",
        "executed": False,
    }

    retry_runtime = definition
    retry_events = [
        event
        async for event in retry_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            resume=AgUiResumeRequest(entries=entries),
        )
    ]
    _assert_success(retry_events)
    assert approved_calls == ["A"]
    assert cancelled_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_type", ["general-purpose", "worker"])
async def test_mixed_resume_is_injected_into_supported_subagents(
    subagent_type: str,
) -> None:
    approved_calls: list[str] = []
    cancelled_calls: list[str] = []

    @tool
    async def child_approved(value: str) -> str:
        """Record one approved child Tool execution."""

        approved_calls.append(value)
        return f"approved:{value}"

    @tool
    async def child_cancelled(value: str) -> str:
        """Fail the test if a cancelled child Tool reaches execution."""

        cancelled_calls.append(value)
        return f"cancelled:{value}"

    task_call = {
        "name": "task",
        "args": {
            "description": "Run the reviewed child tools",
            "subagent_type": subagent_type,
        },
        "id": f"task-{subagent_type}",
        "type": "tool_call",
    }
    model = _ToolBindingModel(
        responses=[
            AIMessage(content="", tool_calls=[task_call]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "child_approved",
                        "args": {"value": "A"},
                        "id": "child-approved-call",
                        "type": "tool_call",
                    },
                    {
                        "name": "child_cancelled",
                        "args": {"value": "B"},
                        "id": "child-cancelled-call",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="root done"),
        ]
    )
    subagents: list[dict[str, object]] | None = (
        None
        if subagent_type == "general-purpose"
        else [
            {
                "name": "worker",
                "description": "Run reviewed child tools",
                "system_prompt": "Run the requested tools.",
                "tools": [child_approved, child_cancelled],
            }
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .build(
            model=model,
            tools=[child_approved, child_cancelled],
            subagents=cast(
                Any, [*(subagents or []), _external_subagent(declared=False)]
            ),
            interrupt_on={"child_approved": True, "child_cancelled": True},
        )
    )
    review_identity = RunIdentity(
        namespace="test",
        thread_id=f"thread-{subagent_type}",
        run_id="run-review",
    )
    review_runtime = definition
    review_events = [
        event
        async for event in review_runtime.open_agui_run(
            thread_id=review_identity.thread_id,
            run_id=review_identity.run_id,
            input={"messages": [HumanMessage(content="Delegate the work")]},
        )
    ]
    interrupts = tuple(_interrupt_outcome(review_events).interrupts)
    assert len(interrupts) == 2
    for pending_interrupt in interrupts:
        assert pending_interrupt.tool_call_id is not None
        assert ScopedIdCodec().decode(pending_interrupt.tool_call_id)[1]

    entries = (
        ResumeEntry.model_validate(
            {
                "interruptId": interrupts[0].id,
                "status": "resolved",
                "payload": {"type": "approve"},
            }
        ),
        ResumeEntry.model_validate(
            {"interruptId": interrupts[1].id, "status": "cancelled"}
        ),
    )
    resume_identity = RunIdentity(
        namespace="test",
        thread_id=f"thread-{subagent_type}",
        run_id="run-resume",
    )
    binding = AgUiResumeBinding.from_agui(
        entries=entries,
        interrupts=interrupts,
    )
    assert binding.source_agent_names == (subagent_type,)
    resume_runtime = definition
    resumed = [
        event
        async for event in resume_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            resume=AgUiResumeRequest(entries=entries),
        )
    ]

    _assert_success(resumed)
    assert approved_calls == ["A"]
    assert cancelled_calls == []


@pytest.mark.asyncio
async def test_permission_interrupt_uses_the_same_mixed_cancellation_contract() -> None:
    approved_calls: list[str] = []

    @tool
    async def permission_peer(value: str) -> str:
        """Record the resolved peer of one permission-gated cancellation."""

        approved_calls.append(value)
        return f"approved:{value}"

    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/protected/result.txt",
                            "content": "must not be written",
                        },
                        "id": "permission-write",
                        "type": "tool_call",
                    },
                    {
                        "name": "permission_peer",
                        "args": {"value": "approved"},
                        "id": "permission-peer",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    definition = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .build(
            model=model,
            tools=[permission_peer],
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/protected/**"], mode="interrupt"
                )
            ],
            interrupt_on={"permission_peer": True},
        )
    )
    review_identity = RunIdentity(
        namespace="test", thread_id="thread-permission", run_id="run-review"
    )
    review_runtime = definition
    review_events = [
        event
        async for event in review_runtime.open_agui_run(
            thread_id=review_identity.thread_id,
            run_id=review_identity.run_id,
            input={"messages": [HumanMessage(content="Write and run peer")]},
        )
    ]
    interrupts = tuple(_interrupt_outcome(review_events).interrupts)
    by_name = {}
    for pending_interrupt in interrupts:
        assert isinstance(pending_interrupt.metadata, Mapping)
        deepagents_metadata = pending_interrupt.metadata.get("deepagents")
        assert isinstance(deepagents_metadata, Mapping)
        tool_name = deepagents_metadata.get("toolName")
        assert isinstance(tool_name, str)
        by_name[tool_name] = pending_interrupt
    assert set(by_name) == {"write_file", "permission_peer"}
    entries = (
        ResumeEntry.model_validate(
            {
                "interruptId": by_name["permission_peer"].id,
                "status": "resolved",
                "payload": {"type": "approve"},
            }
        ),
        ResumeEntry.model_validate(
            {
                "interruptId": by_name["write_file"].id,
                "status": "cancelled",
            }
        ),
    )
    resume_identity = RunIdentity(
        namespace="test", thread_id="thread-permission", run_id="run-resume"
    )
    native_parts: list[Mapping[str, object]] = []

    async def observe(part: Mapping[str, object]) -> None:
        native_parts.append(part)

    resume_runtime = definition
    resumed = [
        event
        async for event in resume_runtime.open_agui_run(
            thread_id=resume_identity.thread_id,
            run_id=resume_identity.run_id,
            resume=AgUiResumeRequest(entries=entries),
            on_native_part=observe,
        )
    ]

    _assert_success(resumed)
    assert approved_calls == ["approved"]
    root_values = [
        data
        for part in native_parts
        if part.get("type") == "values" and part.get("ns") == ()
        if isinstance((data := part.get("data")), Mapping)
    ]
    assert all(
        "/protected/result.txt" not in data.get("files", {}) for data in root_values
    )


def _external_subagent(*, declared: bool) -> dict[str, object]:
    def done(state: MessagesState) -> dict[str, list[AIMessage]]:
        del state
        return {"messages": [AIMessage(content="done")]}

    builder = StateGraph(MessagesState)
    builder.add_node("done", done)
    builder.add_edge(START, "done")
    builder.add_edge("done", END)
    spec: dict[str, object] = {
        "name": "external",
        "description": "Externally compiled child",
        "runnable": builder.compile(),
    }
    if declared:
        spec["tinkerfin_hitl_contract"] = HITL_CONTRACT_ID
    return spec


@pytest.mark.asyncio
@pytest.mark.parametrize("named_source", [True, False])
async def test_external_subagent_without_contract_rejects_mixed_resume_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    named_source: bool,
) -> None:
    from deepagents import DeepAgentState, create_deep_agent

    executed: list[str] = []

    @tool
    async def external_one() -> str:
        """Execute the first external action."""
        executed.append("one")
        return "one"

    @tool
    async def external_two() -> str:
        """Execute the second external action."""
        executed.append("two")
        return "two"

    class ExternalState(DeepAgentState, total=False):
        _tinkerfin_lineage: dict[str, object]
        _tinkerfin_resume: dict[str, object]

    child_model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "external_one",
                        "args": {},
                        "id": "external-call-one",
                        "type": "tool_call",
                    },
                    {
                        "name": "external_two",
                        "args": {},
                        "id": "external-call-two",
                        "type": "tool_call",
                    },
                ],
            )
        ]
    )
    child = create_deep_agent(
        model=child_model,
        tools=[external_one, external_two],
        interrupt_on={"external_one": True, "external_two": True},
        state_schema=ExternalState,
    )
    model = _ToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "subagent_type": "external",
                            "description": "Run the external actions",
                        },
                        "id": "delegate-external",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    saver = InMemorySaver()
    runtime = (
        TinkerFin(checkpointer=saver)
        .with_namespace("test")
        .build(
            model=model,
            subagents=[
                {
                    "name": "external",
                    "description": "External actions",
                    "runnable": child,
                }
            ],
        )
    )
    events = [
        event
        async for event in runtime.open_agui_run(
            thread_id="external-thread",
            run_id="review",
            input={"messages": [HumanMessage(content="Delegate", id="user")]},
        )
    ]
    if not named_source:
        read = saver.aget_tuple

        async def untagged(config):
            checkpoint = await read(config)
            if checkpoint is not None and checkpoint.config.get("configurable", {}).get(
                "checkpoint_ns"
            ):
                metadata = dict(checkpoint.metadata)
                metadata.pop("lc_agent_name", None)
                metadata.pop("ls_agent_type", None)
                return checkpoint._replace(metadata=metadata)
            return checkpoint

        monkeypatch.setattr(saver, "aget_tuple", untagged)
    pending = _interrupt_outcome(events).interrupts
    assert len(pending) == 2
    request = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": pending[0].id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                },
                {"interruptId": pending[1].id, "status": "cancelled"},
            ]
        }
    )
    resumed = runtime.open_agui_run(
        thread_id="external-thread", run_id="resume", resume=request
    )
    result = [event async for event in resumed]
    assert result[-1].type.value == "RUN_ERROR"
    assert isinstance(resumed.error, TinkerFinLifecycleError)
    assert "tool review support" in str(resumed.error)
    assert executed == []
    abandon = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": item.id, "status": "cancelled"} for item in pending
            ]
        }
    )
    cancelled = [
        event
        async for event in runtime.open_agui_run(
            thread_id="external-thread", run_id="abandon", resume=abandon
        )
    ]
    terminal = cancelled[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "resume_cancelled"
    assert executed == []


def test_subagent_declaration_cannot_grant_tool_cancellation_support() -> None:
    with pytest.raises(
        TypeError, match="unsupported subagent fields: tinkerfin_hitl_contract"
    ):
        TinkerFin(checkpointer=InMemorySaver()).with_namespace("test").build(
            model=_ToolBindingModel(responses=[AIMessage(content="unused")]),
            tools=[],
            subagents=cast(Any, [_external_subagent(declared=True)]),
        )
