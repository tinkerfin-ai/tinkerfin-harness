"""Structural integration with request-scoped TinkerFin Deep Agent streams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from ag_ui.core import (
    ActivitySnapshotEvent,
    AssistantMessage,
    BaseEvent,
    Context,
    FunctionCall,
    Interrupt,
    MessagesSnapshotEvent,
    RawEvent,
    ResumeEntry,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    TextMessageStartEvent,
    Tool,
    ToolCall,
    ToolCallStartEvent,
    UserMessage,
)
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_stream_writer

from tinkerfin import (
    AgentRuntime,
    NativeRunStream,
    RunIdentity,
    TinkerFin,
)
from tinkerfin_contracts import PreparedWorkspace
from tinkerfin_messaging import (
    MessageCodecInputSource,
    MessageSubscription,
    Messaging,
    NativeStreamPart,
    create_agui_run_source,
)


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


def _run_agent_input(identity: RunIdentity) -> RunAgentInput:
    return RunAgentInput(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="parent-1",
        state={"status": "ready"},
        messages=[UserMessage(id="user-1", content="hello")],
        tools=[
            Tool(
                name="search",
                description="Search records",
                parameters={"type": "object"},
            )
        ],
        context=[Context(description="tenant", value="tenant-1")],
        forwarded_props={"surface": "studio"},
        resume=[
            ResumeEntry(
                interrupt_id="interrupt-1",
                status="resolved",
                payload={"type": "approve"},
            )
        ],
    )


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    """Keep the public fake model executable when Deep Agents binds tools."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _RejectedAgUiSource:
    messaging_cancel_waits_for_first_item = True
    messaging_codec_profile = "agui.event"
    messaging_source_type = BaseEvent
    messaging_replay_type = BaseEvent

    def __init__(self, *, close_error: BaseException) -> None:
        self.messaging_identity = _identity(
            thread_id="different-thread",
            run_id="different-run",
        )
        self._close_error = close_error

    @property
    def messaging_cancel_callback(self):
        async def cancel() -> None:
            return None

        return cancel

    def __aiter__(self) -> AsyncIterator[BaseEvent]:
        async def events() -> AsyncIterator[BaseEvent]:
            if False:
                yield RawEvent(event={}, source="unused")

        return events()

    async def aclose(self) -> None:
        raise self._close_error


class _StaticAgUiSource:
    messaging_cancel_waits_for_first_item = True
    messaging_codec_profile = "agui.event"
    messaging_source_type = BaseEvent
    messaging_replay_type = BaseEvent

    def __init__(self, identity: RunIdentity, event: BaseEvent) -> None:
        self.messaging_identity = identity
        self._event = event
        self.closed = False

    @property
    def messaging_cancel_callback(self):
        async def cancel() -> None:
            return None

        return cancel

    def __aiter__(self) -> AsyncIterator[BaseEvent]:
        async def events() -> AsyncIterator[BaseEvent]:
            yield self._event

        return events()

    async def aclose(self) -> None:
        self.closed = True


async def _native_parts(
    subscription: MessageSubscription[NativeStreamPart],
) -> list[NativeStreamPart]:
    return [message.data async for message in subscription]


async def _events(
    subscription: MessageSubscription[BaseEvent],
) -> list[BaseEvent]:
    return [message.data async for message in subscription]


def _definition(*, custom: bool = False) -> AgentRuntime[None]:
    model = _ToolBindingFakeModel(
        responses=[
            *(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "emit_progress",
                                "args": {"value": 1},
                                "id": "call-progress",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
                if custom
                else []
            ),
            AIMessage(content="answer"),
        ]
    )
    return (
        TinkerFin()
        .with_namespace("test")
        .build(
            model=model,
            tools=[_emit_progress] if custom else [],
            system_prompt="Answer briefly.",
        )
    )


async def test_agui_stream_is_a_directly_iterable_message_source() -> None:
    events = _definition().open_agui_run(
        thread_id=_identity().thread_id,
        run_id=_identity().run_id,
        input=InputAgentState(messages=[HumanMessage(content="AG-UI")]),
    )

    aiter(events)
    await events.aclose()


async def test_native_and_agui_streams_wrap_without_runtime_parameters() -> None:
    definition = _definition()

    async with Messaging() as messaging:
        native_channel = messaging.channel(name="native-events")
        native_identity = _identity(thread_id="native-thread", run_id="native-run")
        native_source = definition.open_run(
            thread_id=native_identity.thread_id,
            run_id=native_identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="Native")]),
            stream_mode=("messages", "tasks", "values"),
        )
        assert isinstance(native_source, NativeRunStream)
        assert isinstance(native_source, MessageCodecInputSource)
        assert native_source.messaging_identity == native_identity
        assert native_source.messaging_codec_profile == "tinkerfin.native-stream"
        assert native_source.messaging_source_type is Mapping
        assert native_source.messaging_codec_input_type is NativeStreamPart
        assert native_source.messaging_replay_type is NativeStreamPart
        native = await native_channel.wrap(
            native_source,
            after=0,
        )

        agui_channel = messaging.channel(name="agui-events")
        agui_identity = _identity(thread_id="agui-thread")
        event_source = definition.open_agui_run(
            thread_id=agui_identity.thread_id,
            run_id=agui_identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="AG-UI")]),
        )
        assert event_source.messaging_identity == agui_identity
        assert event_source.messaging_codec_profile == "agui.event"
        agui = await agui_channel.wrap(
            event_source,
            after=0,
        )

        native_values = await _native_parts(native)
        agui_events = await _events(agui)

    assert native_values
    assert {part.mode for part in native_values} == {"messages", "tasks", "values"}
    assert agui_events[0].type == "RUN_STARTED"
    assert agui_events[-1].type == "RUN_FINISHED"


async def test_agui_run_source_prepares_only_the_selected_owner() -> None:
    prepared: list[RunIdentity] = []
    transformed: list[str] = []

    class Workspace:
        @asynccontextmanager
        async def prepare(
            self, identity: RunIdentity
        ) -> AsyncIterator[PreparedWorkspace[None, StateBackend]]:
            prepared.append(identity)
            yield PreparedWorkspace(None, StateBackend())

    definition = (
        TinkerFin()
        .with_namespace("test")
        .build(
            model=_ToolBindingFakeModel(responses=[AIMessage(content="answer")]),
            backend=Workspace(),
        )
    )
    identity = _identity(thread_id="managed-thread", run_id="managed-run")

    async def transform_event(event: BaseEvent) -> BaseEvent:
        transformed.append(event.type.value)
        return event

    source = create_agui_run_source(
        definition.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="AG-UI")]),
        ),
        transform_event=transform_event,
    )
    candidate = create_agui_run_source(
        definition.open_agui_run(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=InputAgentState(messages=[HumanMessage(content="AG-UI")]),
        ),
        transform_event=transform_event,
    )
    assert prepared == []
    async with Messaging() as messaging:
        channel = messaging.channel(name="managed-agui")
        owner = await channel.wrap(source, after=0)
        owner_events = await _events(owner)
        transformed_before_replay = list(transformed)
        attachment = await channel.wrap(candidate, after=0)
        attachment_events = await _events(attachment)

    assert prepared == [identity]
    assert transformed == transformed_before_replay
    assert owner_events == attachment_events
    assert owner_events[0].type == "RUN_STARTED"
    assert owner_events[-1].type == "RUN_FINISHED"


@pytest.mark.parametrize(
    ("event", "transform", "error_type"),
    (
        (
            RunStartedEvent(thread_id="thread-1", run_id="run-1"),
            lambda _event: RunStartedEvent(
                thread_id="thread-other",
                run_id="run-other",
            ),
            ValueError,
        ),
        (
            RunStartedEvent(thread_id="thread-1", run_id="run-1"),
            lambda event: event.model_copy(update={"threadId": "thread-other"}),
            ValueError,
        ),
        (
            RunStartedEvent(thread_id="thread-1", run_id="run-1"),
            lambda _event: RunErrorEvent(message="changed lifecycle"),
            TypeError,
        ),
        (
            TextMessageStartEvent(message_id="message-1", role="assistant"),
            lambda event: event.model_copy(update={"message_id": "message-other"}),
            ValueError,
        ),
        (
            TextMessageStartEvent(message_id="message-1", role="assistant"),
            lambda event: event.model_copy(update={"messageId": "message-other"}),
            ValueError,
        ),
        (
            ToolCallStartEvent(
                tool_call_id="tool-1",
                tool_call_name="search",
            ),
            lambda event: event.model_copy(update={"tool_call_id": "tool-other"}),
            ValueError,
        ),
        (
            ToolCallStartEvent(
                tool_call_id="tool-1",
                tool_call_name="search",
            ),
            lambda event: event.model_copy(update={"toolCallId": "tool-other"}),
            ValueError,
        ),
        (
            MessagesSnapshotEvent(
                messages=[
                    AssistantMessage(
                        id="message-1",
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="tool-1",
                                function=FunctionCall(
                                    name="search",
                                    arguments='{"query":"value"}',
                                ),
                            )
                        ],
                    )
                ]
            ),
            lambda event: event.model_copy(
                update={
                    "messages": [
                        AssistantMessage(
                            id="message-1",
                            content="",
                            tool_calls=[
                                ToolCall(
                                    id="tool-other",
                                    function=FunctionCall(
                                        name="search",
                                        arguments='{"query":"value"}',
                                    ),
                                )
                            ],
                        )
                    ]
                }
            ),
            ValueError,
        ),
        (
            RunFinishedEvent(
                thread_id="thread-1",
                run_id="run-1",
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[
                        Interrupt(
                            id="interrupt-1",
                            reason="tool_call",
                            tool_call_id="tool-1",
                        )
                    ]
                ),
            ),
            lambda _event: RunFinishedEvent(
                thread_id="thread-1",
                run_id="run-1",
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[
                        Interrupt(
                            id="interrupt-other",
                            reason="tool_call",
                            tool_call_id="tool-1",
                        )
                    ]
                ),
            ),
            ValueError,
        ),
        (
            ActivitySnapshotEvent(
                message_id="activity-1",
                activity_type="progress",
                content={"status": "running"},
                replace=True,
            ),
            lambda event: event.model_copy(update={"replace": False}),
            ValueError,
        ),
        (
            RunFinishedEvent(
                thread_id="thread-1",
                run_id="run-1",
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[
                        Interrupt(
                            id="interrupt-1",
                            reason="tool_call",
                            tool_call_id="tool-1",
                            response_schema={"type": "approve"},
                            expires_at="2026-09-01T00:00:00Z",
                            metadata={"runtimeInterrupt": {"id": "native-1"}},
                        )
                    ]
                ),
            ),
            lambda _event: RunFinishedEvent(
                thread_id="thread-1",
                run_id="run-1",
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[
                        Interrupt(
                            id="interrupt-1",
                            reason="tool_call",
                            tool_call_id="tool-1",
                            response_schema={"type": "reject"},
                            expires_at="1999-01-01T00:00:00Z",
                            metadata={"runtimeInterrupt": {"id": "native-other"}},
                        )
                    ]
                ),
            ),
            ValueError,
        ),
    ),
)
async def test_agui_run_source_rejects_protocol_identity_mutation(
    event: BaseEvent,
    transform: Callable[[BaseEvent], BaseEvent],
    error_type: type[Exception],
) -> None:
    identity = _identity()
    opened = _StaticAgUiSource(identity, event)

    source = create_agui_run_source(
        opened,
        transform_event=transform,
    )

    with pytest.raises(error_type):
        await anext(aiter(source))

    await source.aclose()
    assert opened.closed


@pytest.mark.parametrize(
    ("field", "mutate_input"),
    (
        (
            "thread_id",
            lambda value: value.model_copy(update={"thread_id": "thread-other"}),
        ),
        ("run_id", lambda value: value.model_copy(update={"run_id": "run-other"})),
        (
            "parent_run_id",
            lambda value: value.model_copy(update={"parent_run_id": "parent-other"}),
        ),
        (
            "state",
            lambda value: value.model_copy(update={"state": {"status": "other"}}),
        ),
        (
            "messages",
            lambda value: value.model_copy(
                update={"messages": [UserMessage(id="user-other", content="changed")]}
            ),
        ),
        (
            "tools",
            lambda value: value.model_copy(
                update={
                    "tools": [
                        Tool(
                            name="delete",
                            description="Delete records",
                            parameters={"type": "object"},
                        )
                    ]
                }
            ),
        ),
        (
            "context",
            lambda value: value.model_copy(
                update={
                    "context": [Context(description="tenant", value="tenant-other")]
                }
            ),
        ),
        (
            "forwarded_props",
            lambda value: value.model_copy(
                update={"forwarded_props": {"surface": "other"}}
            ),
        ),
        ("resume", lambda value: value.model_copy(update={"resume": None})),
    ),
)
async def test_agui_run_source_preserves_the_complete_run_input(
    field: str,
    mutate_input: Callable[[RunAgentInput], RunAgentInput],
) -> None:
    identity = _identity()
    event = RunStartedEvent(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id="parent-1",
        input=_run_agent_input(identity),
    )
    opened = _StaticAgUiSource(identity, event)

    def change_input(source: BaseEvent) -> BaseEvent:
        assert isinstance(source, RunStartedEvent)
        assert source.input is not None
        return source.model_copy(update={"input": mutate_input(source.input)})

    source = create_agui_run_source(
        opened,
        transform_event=change_input,
    )

    with pytest.raises(ValueError, match="protocol identity"):
        await anext(aiter(source))

    await source.aclose()
    assert opened.closed, field


async def test_agui_run_source_allows_product_metadata_without_identity_changes() -> (
    None
):
    identity = _identity()
    opened = _StaticAgUiSource(
        identity,
        RunStartedEvent(thread_id=identity.thread_id, run_id=identity.run_id),
    )

    def add_title(event: BaseEvent) -> BaseEvent:
        return event.model_copy(update={"title": "Product title"})

    source = create_agui_run_source(
        opened,
        transform_event=add_title,
    )

    transformed = await anext(aiter(source))

    assert isinstance(transformed, RunStartedEvent)
    assert transformed.thread_id == identity.thread_id
    assert transformed.run_id == identity.run_id
    assert transformed.model_extra == {"title": "Product title"}
    await source.aclose()


async def test_agui_run_source_allows_activity_content_without_replace_changes() -> (
    None
):
    identity = _identity()
    opened = _StaticAgUiSource(
        identity,
        ActivitySnapshotEvent(
            message_id="activity-1",
            activity_type="progress",
            content={"status": "starting"},
            replace=True,
        ),
    )

    def update_content(event: BaseEvent) -> BaseEvent:
        return event.model_copy(update={"content": {"status": "running"}})

    source = create_agui_run_source(
        opened,
        transform_event=update_content,
    )

    transformed = await anext(aiter(source))

    assert isinstance(transformed, ActivitySnapshotEvent)
    assert transformed.content == {"status": "running"}
    assert transformed.replace is True
    await source.aclose()


async def test_agui_run_source_allows_additive_interrupt_product_metadata() -> None:
    identity = _identity()
    event = RunFinishedEvent(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        outcome=RunFinishedInterruptOutcome(
            interrupts=[
                Interrupt(
                    id="interrupt-1",
                    reason="tool_call",
                    tool_call_id="tool-1",
                    response_schema={"type": "object"},
                    expires_at="2026-09-01T00:00:00Z",
                    metadata={"runtimeInterrupt": {"id": "native-1"}},
                )
            ]
        ),
    )
    opened = _StaticAgUiSource(identity, event)

    def add_product_metadata(source: BaseEvent) -> BaseEvent:
        assert isinstance(source, RunFinishedEvent)
        assert isinstance(source.outcome, RunFinishedInterruptOutcome)
        interrupt = source.outcome.interrupts[0].model_copy(
            update={
                "metadata": {
                    "runtimeInterrupt": {"id": "native-1"},
                    "product": {"title": "Review file"},
                }
            }
        )
        return source.model_copy(
            update={
                "outcome": RunFinishedInterruptOutcome(interrupts=[interrupt]),
            }
        )

    source = create_agui_run_source(
        opened,
        transform_event=add_product_metadata,
    )

    transformed = await anext(aiter(source))

    assert isinstance(transformed, RunFinishedEvent)
    assert isinstance(transformed.outcome, RunFinishedInterruptOutcome)
    assert transformed.outcome.interrupts[0].metadata == {
        "runtimeInterrupt": {"id": "native-1"},
        "product": {"title": "Review file"},
    }
    await source.aclose()


@pytest.mark.parametrize(
    "event",
    (
        RunStartedEvent(thread_id="thread-other", run_id="run-other"),
        RunFinishedEvent(
            thread_id="thread-other",
            run_id="run-other",
            outcome=RunFinishedSuccessOutcome(),
        ),
        RunErrorEvent(
            message="failed",
            raw_event={"threadId": "thread-other", "runId": "run-other"},
        ),
        RunStartedEvent(
            thread_id="thread-1",
            run_id="run-1",
            parent_run_id="parent-1",
            input=_run_agent_input(
                _identity(thread_id="thread-other", run_id="run-other")
            ),
        ),
    ),
)
async def test_agui_run_source_rejects_events_from_another_run_without_transform(
    event: BaseEvent,
) -> None:
    identity = _identity()
    opened = _StaticAgUiSource(identity, event)

    source = create_agui_run_source(opened)

    with pytest.raises(ValueError, match="RunIdentity"):
        await anext(aiter(source))


@pytest.mark.parametrize("different_field", [None, "namespace", "thread_id", "run_id"])
async def test_agui_run_source_compares_complete_identity_values(
    different_field: str | None,
) -> None:
    identity = _identity(thread_id="identity-thread", run_id="identity-run")
    values = identity.model_dump()
    if different_field is not None:
        values[different_field] = "different"
    reconstructed = RunIdentity.model_validate(values)
    assert reconstructed is not identity
    event = RunStartedEvent(
        thread_id=reconstructed.thread_id, run_id=reconstructed.run_id
    )
    opened = _StaticAgUiSource(reconstructed, event)

    source = create_agui_run_source(opened)
    assert source.messaging_identity == reconstructed
    async with Messaging() as messaging:
        channel = messaging.channel(name="identity-agui")
        if different_field is None:
            subscription = await channel.wrap(source, identity=identity)
            assert await _events(subscription) == [event]
        else:
            with pytest.raises(ValueError, match="explicit identity must match"):
                await channel.wrap(source, identity=identity)
    await source.aclose()
    assert opened.closed


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit, RuntimeError],
)
async def test_agui_run_source_preserves_original_cleanup_failure(
    error_type: type[BaseException],
) -> None:
    error = error_type("cleanup stopped")
    cause = LookupError("provider cleanup cause")
    error.__cause__ = cause
    source = create_agui_run_source(_RejectedAgUiSource(close_error=error))
    with pytest.raises(error_type) as captured:
        await source.aclose()
    assert captured.value is error
    assert captured.value.__cause__ is cause


@tool("emit_progress")
def _emit_progress(value: int) -> str:
    """Emit one custom progress record for the active Deep Agent run."""

    get_stream_writer()({"progress": value})
    return f"emitted {value}"


async def test_real_custom_stream_is_consistent_across_all_consumers() -> None:
    def native_source(*, run_id: str):
        return _definition(custom=True).open_run(
            thread_id=_identity(thread_id="custom-thread", run_id=run_id).thread_id,
            run_id=_identity(thread_id="custom-thread", run_id=run_id).run_id,
            input=InputAgentState(messages=[HumanMessage(content="Report progress")]),
            stream_mode=("messages", "tasks", "values", "custom"),
        )

    direct = [part async for part in native_source(run_id="custom-direct")]
    assert any(
        part["type"] == "custom" and part["data"] == {"progress": 1} for part in direct
    )

    frames = [frame async for frame in native_source(run_id="custom-sse").to_sse()]
    frame_payloads = [
        json.loads(frame.split(b"data: ", maxsplit=1)[1]) for frame in frames
    ]
    assert any(
        payload["type"] == "custom" and payload["data"] == {"progress": 1}
        for payload in frame_payloads
    )

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="custom-native").wrap(
            native_source(run_id="custom-messaging"),
        )
        replay = await _native_parts(subscription)

    assert any(
        part.mode == "custom" and part.data == {"progress": 1} for part in replay
    )

    events = [
        event
        async for event in _definition(custom=True).open_agui_run(
            thread_id=_identity(
                thread_id="custom-thread", run_id="custom-agui"
            ).thread_id,
            run_id=_identity(thread_id="custom-thread", run_id="custom-agui").run_id,
            input=InputAgentState(messages=[HumanMessage(content="Report progress")]),
            stream_mode=("messages", "tasks", "values", "custom"),
        )
    ]
    raw_events = [
        event
        for event in events
        if isinstance(event, RawEvent) and event.source == "langgraph.custom"
    ]
    assert len(raw_events) == 1
    assert raw_events[0].source == "langgraph.custom"
    assert raw_events[0].event["data"] == {"progress": 1}


async def test_agui_run_source_accepts_typed_product_fields_and_rejects_changed_identity() -> (
    None
):
    class LabeledRunStart(RunStartedEvent):
        label: str

    identity = _identity()
    for change_identity in (False, True):
        opened = _StaticAgUiSource(
            identity,
            LabeledRunStart(
                thread_id=identity.thread_id, run_id=identity.run_id, label="Report"
            ),
        )

        def transform(event: BaseEvent) -> BaseEvent:
            return event.model_copy(
                update={"runId": "other"} if change_identity else {"label": "Chart"}
            )

        source = create_agui_run_source(opened, transform_event=transform)
        try:
            if change_identity:
                with pytest.raises(ValueError, match="protocol identity"):
                    await anext(aiter(source))
            else:
                event = await anext(aiter(source))
                assert event.model_extra == {"label": "Chart"}
        finally:
            await source.aclose()
        assert opened.closed
