"""Managed Native origins preserve proposal identity across requests."""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import RawEvent
from deepagents import create_deep_agent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, StreamMode
from test_durable_delegation import _ReviewCase

from tinkerfin import (
    NativeRunStream,
    RunIdentity,
    RunObservationError,
    TinkerFin,
    TinkerFinStreamProtocolError,
)
from tinkerfin.native_driver import DeepAgentsV2StreamDriver, NativeStreamDriver
from tinkerfin.runtime_profile import DeepAgentsV2RuntimeProfile
from tinkerfin_agui_adapter import AttachmentMessagesSnapshotEvent, DeepAgentAgUiAdapter
from tinkerfin_contracts import (
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RuntimeObservation,
)
from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeStreamFrame,
    NativeStreamPart,
    NativeTasksStreamPart,
    NativeValuesStreamPart,
    validate_native_stream_part,
)
from tinkerfin_tracing import CapturePolicy, ModelCallFact, Tracer


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


@pytest.mark.parametrize("delegated", [False, True])
async def test_new_proposal_cannot_reuse_a_prior_tool_identity(
    delegated: bool,
) -> None:
    executed: list[str] = []

    @tool
    async def work(value: str) -> str:
        """Return the requested value."""
        executed.append(value)
        return value

    responses: list[BaseMessage] = []
    for number in (1, 2):
        responses.extend(
            [
                AIMessage(
                    id=f"proposal-{number}",
                    content="",
                    tool_calls=[
                        {
                            "id": "repeated",
                            "name": "task" if delegated else "work",
                            "args": {
                                "description": f"job {number}",
                                "subagent_type": "worker",
                            }
                            if delegated
                            else {"value": f"job {number}"},
                        }
                    ],
                ),
                AIMessage(id=f"done-{number}", content=f"complete {number}"),
            ]
        )
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("proposal-identity")
        .with_observer(tracer)
        .build(
            model=_Model(responses=responses),
            tools=[work],
            subagents=[
                {
                    "name": "worker",
                    "description": "Complete work",
                    "system_prompt": "Complete work",
                    "model": _Model(
                        responses=[
                            AIMessage(id="child-result", content="child complete")
                        ]
                    ),
                }
            ]
            if delegated
            else [],
        )
    )
    first = runtime.open_run(
        thread_id="thread",
        run_id="first",
        input={"messages": [{"id": "user-1", "role": "user", "content": "first"}]},
    )
    _ = [part async for part in first]
    assert first.error is None
    reader = runtime.agui.history(tracer)
    before = (await reader.get("thread")).snapshot
    results = [message for message in before.messages if message.role == "tool"]
    assert len(results) == 1 and results[0].content == (
        "child complete" if delegated else "job 1"
    )

    second = runtime.open_run(
        thread_id="thread",
        run_id="second",
        input={"messages": [{"id": "user-2", "role": "user", "content": "second"}]},
    )
    with pytest.raises(TinkerFinStreamProtocolError):
        _ = [part async for part in second]
    assert isinstance(second.error, TinkerFinStreamProtocolError)
    after = (await reader.get("thread")).snapshot
    assert [message for message in after.messages if message.run_id == "first"] == list(
        before.messages
    )
    assert [node for node in after.graph.nodes if node.run_id == "first"] == list(
        before.graph.nodes
    )
    assert executed == ([] if delegated else ["job 1"])


@pytest.mark.parametrize("native", [False, True])
async def test_named_ordinary_graph_inherits_one_logical_delegate(native: bool) -> None:
    external = create_deep_agent(
        model=_Model(responses=[AIMessage(content="foreign answer")]), name="foreign"
    )

    @tool
    async def call_nested() -> str:
        """Execute an ordinary named Graph inside delegated work."""
        await external.ainvoke({"messages": [{"role": "user", "content": "Work"}]})
        return "nested complete"

    tracer = Tracer()
    runtime = (
        TinkerFin()
        .with_namespace("nested-name")
        .with_observer(tracer)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "delegate",
                                "name": "task",
                                "args": {
                                    "description": "Work",
                                    "subagent_type": "worker",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Work",
                    "system_prompt": "Work",
                    "model": _Model(
                        responses=[
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"id": "nested", "name": "call_nested", "args": {}}
                                ],
                            ),
                            AIMessage(content="worker complete"),
                        ]
                    ),
                    "tools": [call_nested],
                }
            ],
        )
    )
    messages = [{"id": "user", "role": "user", "content": "Work"}]
    stream = (
        runtime.open_run(
            thread_id="thread",
            run_id="run",
            input=InputAgentState(messages=[dict(message) for message in messages]),
        )
        if native
        else runtime.open_agui_run(thread_id="thread", run_id="run", messages=messages)
    )
    _ = [part async for part in stream]
    assert stream.error is None
    view = (await runtime.agui.history(tracer).get("thread")).snapshot
    delegates = [node for node in view.graph.nodes if node.kind == "subagent"]
    assert len(delegates) == 1 and delegates[0].status == "succeeded"
    nested = [
        node
        for node in view.graph.nodes
        if node.kind == "model" and len(node.graph_namespace) > 1
    ]
    assert nested and all(node.parent_subagent_id == delegates[0].id for node in nested)


class _StreamingChild(_Model):
    entered: asyncio.Event
    release: asyncio.Event
    closed: asyncio.Event

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        self.entered.set()
        try:
            yield ChatGenerationChunk(
                message=AIMessageChunk(id="child", content="partial")
            )
            await self.release.wait()
        finally:
            self.closed.set()


class _RejectingScopeDriver(DeepAgentsV2StreamDriver):
    def normalize(
        self, part: object, *, context: RunSourceContext
    ) -> NativeStreamFrame:
        frame = super().normalize(part, context=context)
        if isinstance(frame.canonical, NativeMessageStreamPart) and frame.canonical.ns:
            return replace(
                frame,
                canonical=frame.canonical.model_copy(
                    update={
                        "ns": ("unproved:task",),
                    }
                ),
            )
        return frame


class _RejectingScopeProfile(DeepAgentsV2RuntimeProfile):
    @property
    def stream_driver(self) -> NativeStreamDriver:
        return _RejectingScopeDriver()


class _FailureSession:
    def __init__(self) -> None:
        self.failure = asyncio.get_running_loop().create_future()
        self.closed = False

    async def observe(self, observation: RuntimeObservation) -> None:
        del observation

    async def force(self, boundary: ObservationBoundary) -> None:
        del boundary

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        return self.failure

    async def aclose(self) -> None:
        self.closed = True


class _FailureObserver:
    def __init__(self, session: _FailureSession) -> None:
        self.session = session

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        del context
        return self.session


@pytest.mark.parametrize("ending", ["cancel", "source_rejected", "observer_failed"])
async def test_child_provider_callbacks_close_on_abnormal_stream_exit(
    ending: str,
    recwarn: pytest.WarningsRecorder,
) -> None:
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = _FailureSession()
    runtime = (
        TinkerFin(
            runtime_profile=_RejectingScopeProfile()
            if ending == "source_rejected"
            else None
        )
        .with_namespace("callback-close")
        .with_observer(_FailureObserver(session))
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "delegate",
                                "name": "task",
                                "args": {
                                    "description": "Work",
                                    "subagent_type": "worker",
                                },
                            }
                        ],
                    )
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Work",
                    "system_prompt": "Work",
                    "model": _StreamingChild(
                        responses=[], entered=entered, release=release, closed=closed
                    ),
                }
            ],
        )
    )
    stream = runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Work"}]},
    )

    async def consume() -> None:
        async for _ in stream:
            pass

    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        if ending == "cancel":
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        elif ending == "observer_failed":
            session.failure.set_result(ValueError("observer stopped"))
            with pytest.raises(RunObservationError):
                await consumer
        else:
            with pytest.raises(TinkerFinStreamProtocolError):
                await consumer
        assert closed.is_set()
        assert session.closed
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()
    gc.collect()
    assert not [
        warning for warning in recwarn if "never awaited" in str(warning.message)
    ]


@pytest.mark.parametrize("transport", ["capture", "sse"])
async def test_durable_native_replay_preserves_sources_and_review_resume(
    transport: str,
) -> None:
    case = _ReviewCase()

    async def capture(stream: NativeRunStream) -> list[NativeStreamPart]:
        if transport == "sse":
            frames = [frame async for frame in stream.to_sse()]
            return [
                NativeStreamPart.model_validate_json(
                    frame.split(b"data: ", 1)[1].strip()
                )
                for frame in frames
            ]
        values: list[NativeStreamPart] = []
        async for raw in stream:
            values.append(
                NativeStreamPart.model_validate_json(
                    stream.messaging_codec_input(raw).model_dump_json(by_alias=True)
                )
            )
        return values

    modes: list[StreamMode] = [
        "messages",
        "tasks",
        "values",
        "updates",
        "checkpoints",
        "debug",
    ]
    first = case.build().open_run(
        thread_id="thread",
        run_id="first",
        input={"messages": [{"role": "user", "content": "Do work"}]},
        stream_mode=modes,
    )
    before = await capture(first)
    assert first.error is None
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="durable", thread_id="thread", run_id="first")
    )
    events = [event for part in before for event in adapter.process(part)]
    events.extend(adapter.finish())
    outcome = adapter.main_outcome()
    assert outcome is not None and outcome.type == "interrupt"
    snapshots = [
        event for event in events if isinstance(event, AttachmentMessagesSnapshotEvent)
    ]
    assert snapshots
    assert {item.tool_call_id for item in outcome.interrupts} <= {
        call.id
        for message in snapshots[-1].messages
        if isinstance(message, AgUiAssistantMessage)
        for call in message.tool_calls or ()
    }
    roots = [
        part
        for part in before
        if part.mode == "values" and not part.graph_namespace and part.interrupts
    ]
    assert roots
    raw_interrupt = roots[-1].interrupts[0]
    assert isinstance(raw_interrupt, dict)
    resumed = case.build().open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(
            resume={str(raw_interrupt["id"]): {"decisions": [{"type": "approve"}]}}
        ),
        stream_mode=modes,
    )
    after = await capture(resumed)
    assert resumed.error is None and case.executed == ["approved"]
    continued = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="durable", thread_id="thread", run_id="resume")
    )
    events.extend(event for part in after for event in continued.process(part))
    events.extend(continued.finish())
    assert continued.main_outcome().type == "success"
    delegated = [
        part
        for part in [*before, *after]
        if part.graph_origin.subagent_request is not None
    ]
    assert (
        len(
            {
                part.graph_origin.subagent_request.id
                for part in delegated
                if part.graph_origin.subagent_request
            }
        )
        == 1
    )
    assert all(part.graph_origin.parent_task is not None for part in delegated)
    assert any(len(part.graph_namespace) == 2 for part in delegated)
    public = json.dumps(
        [part.model_dump(mode="json", by_alias=True) for part in [*before, *after]]
    )
    public += json.dumps(
        [event.model_dump(mode="json", by_alias=True) for event in events]
    )
    assert "record_digest" not in public and "attempt_key" not in public
    assert all(
        not (
            part.mode == "tasks"
            and isinstance(part.data, dict)
            and part.data.get("name") == "delegation_attempt"
        )
        for part in [*before, *after]
    )


@pytest.mark.parametrize("extra_modes", [False, True])
async def test_public_task_failures_omit_provider_detail_without_erasing_trace(
    extra_modes: bool,
) -> None:
    marker = "provider request contained private detail"
    case = _ReviewCase()
    case.child.failure = ValueError(marker)
    case.child.failures = 3
    case.policy.on_failure = "continue"
    tracer = Tracer(
        capture_policy=CapturePolicy.public_history(include_error_messages=True)
    )
    case.observers = (tracer,)
    runtime = case.build()
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="failed-child",
        messages=[{"id": "user", "role": "user", "content": "Do work"}],
        stream_mode=["messages", "tasks", "values", "checkpoints", "debug"]
        if extra_modes
        else None,
    )
    events = [event async for event in stream]
    assert stream.error is None
    raw = [event for event in events if isinstance(event, RawEvent)]
    assert raw
    assert all(marker not in event.model_dump_json() for event in raw)
    view = await runtime.agui.history(tracer).get("thread")
    facts = (await view.trace.events(limit=1000)).items
    assert any(
        isinstance(event.fact, ModelCallFact)
        and event.fact.phase == "failed"
        and event.fact.error_message is not None
        and event.fact.error_message.value == marker
        for event in facts
    )


async def test_parallel_private_attempts_match_their_exact_delegation_requests() -> (
    None
):
    cases = {name: _ReviewCase() for name in ("alpha", "beta")}
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("parallel-retry")
        .with_observer(tracer)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": f"delegate-{name}",
                                "name": "task",
                                "args": {"description": "Work", "subagent_type": name},
                            }
                            for name in cases
                        ],
                    ),
                    AIMessage(content="complete"),
                ]
            ),
            subagents=[
                {
                    "name": name,
                    "description": "Work",
                    "system_prompt": "Work",
                    "model": case.child,
                    "tools": [case.work],
                    "interrupt_on": {"work": True},
                }
                for name, case in cases.items()
            ],
            middleware=[cases["alpha"].policy],
        )
    )
    first = runtime.open_run(
        thread_id="thread",
        run_id="first",
        input={"messages": [{"role": "user", "content": "Work"}]},
    )
    before = [validate_native_stream_part(raw) async for raw in first]
    assert first.error is None
    interrupts = {
        item.id: item
        for part in before
        if isinstance(part, NativeValuesStreamPart) and not part.ns
        for item in part.interrupts
    }
    assert len(interrupts) == 2
    resumed = runtime.open_run(
        thread_id="thread",
        run_id="resume",
        input=Command(
            resume={key: {"decisions": [{"type": "approve"}]} for key in interrupts}
        ),
    )
    after = [validate_native_stream_part(raw) async for raw in resumed]
    assert resumed.error is None
    assert all(case.executed == ["approved"] for case in cases.values())
    assert all(
        part.data.name != "delegation_attempt"
        for part in [*before, *after]
        if isinstance(part, NativeTasksStreamPart)
    )
    view = (await runtime.agui.history(tracer).get("thread")).snapshot
    delegates = [node for node in view.graph.nodes if node.kind == "subagent"]
    assert len(delegates) == 2
    assert {node.source_id for node in delegates} == {"delegate-alpha", "delegate-beta"}
    assert all(node.status == "succeeded" for node in delegates)
