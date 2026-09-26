"""Native Driver and opt-in provider reasoning contracts."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest
from ag_ui.core import ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    ToolMessage,
)
from langgraph.types import Interrupt
from pydantic import BaseModel, JsonValue, field_serializer

from tinkerfin import (
    RunIdentity,
    TinkerFinStreamProtocolError,
)
from tinkerfin.native_driver import DeepAgentsV2StreamDriver, DeepAgentsV3StreamDriver
from tinkerfin.runtime_profile import (
    DeepAgentsRuntimeProfile,
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV3RuntimeProfile,
)
from tinkerfin_agui_adapter import DeepAgentAgUiAdapter
from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeToolCallChunk,
    RunSourceContext,
)
from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeStreamPart,
    NativeValuesStreamPart,
)


class _ProviderReasoningExtractor:
    @property
    def name(self) -> str:
        return "fixture.provider_reasoning"

    def extract(
        self,
        message: BaseMessage,
        *,
        provider: str | None,
    ) -> JsonValue | None:
        if provider != "deepseek":
            return None
        value = message.additional_kwargs.get("reasoning_content")
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise TinkerFinStreamProtocolError("fixture reasoning must be a string")
        return value


def _context() -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(
            namespace="test", thread_id="thread-driver", run_id="run-driver"
        ),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )


def _message_part(
    message: BaseMessage,
    *,
    provider: str | None = None,
) -> dict[str, object]:
    metadata = {"langgraph_node": "model"}
    if provider is not None:
        metadata["ls_provider"] = provider
    return {
        "type": "messages",
        "ns": (),
        "data": (message, metadata),
    }


@pytest.mark.parametrize(
    "message",
    [
        AIMessageChunk(
            id="last-chunk",
            content="complete",
            chunk_position="last",
            additional_kwargs={"reasoning_content": "private"},
        ),
        ToolMessage(
            id="tool-result",
            tool_call_id="call",
            content="Saved",
            artifact={"record_digest": "business", "path": "report.md"},
        ),
        ChatMessage(id="chat", role="reviewer", content="Reviewed"),
    ],
)
def test_native_replay_restores_public_message_subtype_fields(
    message: BaseMessage,
) -> None:
    frame = DeepAgentsV2StreamDriver().normalize(
        _message_part(message), context=_context()
    )
    replay = NativeStreamPart.model_validate_json(
        frame.replay.model_dump_json(by_alias=True)
    )
    restored = replay.to_frame().canonical
    assert isinstance(restored, NativeMessageStreamPart)
    assert restored.data.message == message.model_copy(update={"additional_kwargs": {}})
    assert "private" not in replay.model_dump_json()
    live_adapter = DeepAgentAgUiAdapter(identity=_context().identity)
    replay_adapter = DeepAgentAgUiAdapter(identity=_context().identity)
    live = [*live_adapter.process_frame(frame), *live_adapter.finish()]
    replayed = [*replay_adapter.process(replay), *replay_adapter.finish()]
    assert [event.model_dump() for event in replayed] == [
        event.model_dump() for event in live
    ]


@pytest.mark.parametrize("has_messages", [False, True])
def test_native_replay_preserves_state_channel_presence_and_interrupts(
    has_messages: bool,
) -> None:
    values: dict[str, object] = {"answer": 42}
    if has_messages:
        values["messages"] = []
    frame = DeepAgentsV2StreamDriver().normalize(
        {
            "type": "values",
            "ns": (),
            "data": values,
            "interrupts": (Interrupt(value={"question": "Continue?"}, id="review"),),
        },
        context=_context(),
    )
    replay = NativeStreamPart.model_validate_json(
        frame.replay.model_dump_json(by_alias=True)
    )
    restored = replay.to_frame().canonical
    assert isinstance(restored, NativeValuesStreamPart)
    assert restored.data == values
    assert [(item.id, item.value) for item in restored.interrupts] == [
        ("review", {"question": "Continue?"})
    ]


def test_native_replay_and_live_task_failures_publish_the_same_safe_events() -> None:
    live = DeepAgentAgUiAdapter(identity=_context().identity)
    replayed = DeepAgentAgUiAdapter(identity=_context().identity)
    parts = [
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "model-task",
                "name": "model",
                "input": {},
                "triggers": ("branch:to:model",),
            },
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "model-task",
                "name": "model",
                "result": {},
                "interrupts": [],
                "error": ValueError("private provider detail"),
            },
        },
    ]
    for raw in parts:
        frame = DeepAgentsV2StreamDriver().normalize(raw, context=_context())
        record = NativeStreamPart.model_validate_json(
            frame.replay.model_dump_json(by_alias=True)
        )
        direct_events = live.process(raw)
        recorded_events = replayed.process(record)
        assert [event.model_dump() for event in recorded_events] == [
            event.model_dump() for event in direct_events
        ]
        assert all(
            "private provider detail" not in event.model_dump_json()
            for event in direct_events
        )


class _PublicPoint(BaseModel):
    coordinates: tuple[int, int]
    binary: bytes = b"\xff"
    secret: str = "private-model-field"

    @field_serializer("secret", when_used="json")
    def redact(self, value: str) -> str:
        return "redacted"


@dataclass(frozen=True, slots=True)
class _PublicLabel:
    names: tuple[str, ...]
    binary: bytes = b"\xff"


@pytest.mark.parametrize(
    "kind",
    [
        "values",
        "task_start",
        "task_result",
        "updates",
        "custom",
        "checkpoints",
        "debug_task",
        "debug_checkpoint",
    ],
)
def test_all_public_replay_modes_keep_json_values_and_literal_type_fields(
    kind: str,
) -> None:
    business = {
        "coordinates": (1, 2),
        "literal": {"$type": "tuple", "items": [3, 4]},
        "literalMessage": {"$type": "langchain.message", "value": {"type": "business"}},
        "point": _PublicPoint(coordinates=(5, 6)),
        "label": _PublicLabel(names=("one", "two")),
        "textBytes": b"visible",
        "binaryBytes": b"\xff",
        "bytearray": bytearray(b"\xff"),
        "literalBytes": {"$type": "bytes", "base64": "/w=="},
    }
    message = AIMessage(
        id="message",
        content="visible",
        additional_kwargs={"reasoning_content": "private-reasoning", "keep": "public"},
    )
    checkpoint = {
        "values": {**business, "messages": [message]},
        "next": ("model",),
        "tasks": [
            {
                "id": "task",
                "name": "model",
                "error": ValueError("private-error"),
                "result": business,
            }
        ],
    }
    start = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "task",
            "name": "model",
            "input": {**business, "messages": [message]},
            "triggers": ("branch:to:model",),
            "metadata": business,
        },
    }
    prefix: list[dict[str, object]] = []
    if kind == "values":
        part = {
            "type": "values",
            "ns": (),
            "data": {**business, "messages": [message]},
            "interrupts": (),
        }
    elif kind == "task_start":
        part = start
    elif kind == "task_result":
        prefix.append(start)
        part = {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task",
                "name": "model",
                "result": {**business, "messages": [message]},
                "interrupts": [],
                "error": None,
            },
        }
    elif kind == "updates":
        part = {
            "type": "updates",
            "ns": (),
            "data": {"node": {**business, "messages": [message]}},
        }
    elif kind == "custom":
        part = {"type": "custom", "ns": (), "data": business}
    elif kind == "checkpoints":
        part = {"type": "checkpoints", "ns": (), "data": checkpoint}
    elif kind == "debug_task":
        part = {
            "type": "debug",
            "ns": (),
            "data": {
                "type": "task",
                "step": 1,
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": start["data"],
            },
        }
    else:
        part = {
            "type": "debug",
            "ns": (),
            "data": {
                "type": "checkpoint",
                "step": 1,
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": checkpoint,
            },
        }
    live = DeepAgentAgUiAdapter(identity=_context().identity)
    replayed = DeepAgentAgUiAdapter(identity=_context().identity)
    for raw in [*prefix, part]:
        recorded = DeepAgentsV2StreamDriver().normalize(raw, context=_context()).replay
        decoded = NativeStreamPart.model_validate_json(
            recorded.model_dump_json(by_alias=True)
        )
        assert [event.model_dump() for event in live.process(raw)] == [
            event.model_dump() for event in replayed.process(decoded)
        ]
        serialized = recorded.model_dump_json()
        assert (
            "private-reasoning" not in serialized
            and "private-error" not in serialized
            and "private-model-field" not in serialized
        )


def _astream_shape(
    input: object,
    config: object = None,
    *,
    stream_mode: object = None,
    version: object = None,
    subgraphs: object = None,
    output_keys: object = None,
    **options: object,
) -> object:
    del input, config, stream_mode, version, subgraphs, output_keys, options
    raise AssertionError("signature-only fixture must not be invoked")


def test_v2_profile_owns_factory_identity_and_complete_invocation_binding() -> None:
    profile = DeepAgentsV2RuntimeProfile()
    driver = profile.stream_driver
    identity = RunIdentity(
        namespace="test", thread_id="thread-profile", run_id="run-profile"
    )

    assert isinstance(profile, DeepAgentsRuntimeProfile)
    assert profile.profile_id == "deepagents-v2"
    bound = driver.bind_invocation(
        inspect.signature(_astream_shape),
        ({"messages": []},),
        {
            "stream_mode": ("custom", "messages", "tasks", "values"),
            "context": {"tenant": "tenant-1"},
        },
        identity=identity,
        runtime_profile="deepagents-v2",
    )

    assert bound.arguments["config"] == {
        "configurable": {
            "thread_id": "thread-profile",
            "_tinkerfin_runtime_profile": "deepagents-v2",
        }
    }
    assert bound.arguments["stream_mode"] == (
        "messages",
        "tasks",
        "values",
        "custom",
    )
    assert bound.arguments["version"] == "v2"
    assert bound.arguments["subgraphs"] is True
    assert bound.arguments["options"] == {"context": {"tenant": "tenant-1"}}


def test_v3_profile_owns_explicit_event_stream_binding_without_fallback() -> None:
    profile = DeepAgentsV3RuntimeProfile()
    driver = profile.stream_driver
    identity = RunIdentity(
        namespace="test", thread_id="thread-profile-v3", run_id="run-profile-v3"
    )

    assert isinstance(profile, DeepAgentsRuntimeProfile)
    assert isinstance(driver, DeepAgentsV3StreamDriver)
    assert profile.profile_id == "deepagents-v3"
    bound = driver.bind_invocation(
        inspect.signature(_astream_shape),
        ({"messages": []},),
        {},
        identity=identity,
        runtime_profile="deepagents-v3",
    )

    assert bound.arguments["config"] == {
        "configurable": {
            "thread_id": "thread-profile-v3",
            "_tinkerfin_runtime_profile": "deepagents-v3",
        }
    }
    assert bound.arguments["stream_mode"] == ("messages", "tasks", "values")
    assert bound.arguments["version"] == "v3"
    assert bound.arguments["subgraphs"] is True
    with pytest.raises(TypeError, match="must expose astream_events"):
        profile.graph_stream(object())


def test_default_driver_emits_no_provider_reasoning_observation() -> None:
    frame = DeepAgentsV2StreamDriver().normalize(
        _message_part(
            AIMessageChunk(
                id="message-1",
                content="visible",
                additional_kwargs={"reasoning_content": "private"},
            )
        ),
        context=_context(),
    )

    assert [item.kind for item in frame.observations] == ["native.message"]
    encoded = frame.replay.model_dump_json(by_alias=True)
    assert "visible" in encoded
    assert "private" not in encoded


def test_native_replay_keeps_graph_position_outside_message_payload() -> None:
    part = _message_part(AIMessageChunk(id="message", content="visible"))
    part["ns"] = ("tools:child",)
    frame = DeepAgentsV2StreamDriver().normalize(part, context=_context())

    assert frame.replay.graph_namespace == ("tools:child",)
    assert isinstance(frame.replay.data, dict)
    assert "graphNamespace" not in frame.replay.data
    assert "identity" not in frame.replay.data
    assert frame.observations[0].identity.namespace == "test"


def test_v3_host_extractor_ignores_another_provider_reasoning_delta() -> None:
    frame = DeepAgentsV3StreamDriver(
        reasoning_extractors=(_ProviderReasoningExtractor(),)
    ).normalize(
        _message_part(
            AIMessageChunk(
                id="message-1",
                content="",
                additional_kwargs={"reasoning_content": "other-provider-private"},
            ),
            provider="openai",
        ),
        context=_context(),
    )

    assert [item.kind for item in frame.observations] == ["native.message"]
    assert "other-provider-private" not in frame.replay.model_dump_json(by_alias=True)


@pytest.mark.parametrize("tool_id", [None, ""])
@pytest.mark.parametrize("tool_name", [None, ""])
def test_v2_driver_keeps_idless_followup_tool_data_as_a_chunk(
    tool_id: str | None, tool_name: str | None
) -> None:
    frame = DeepAgentsV2StreamDriver().normalize(
        _message_part(
            AIMessageChunk(
                id="message-1",
                content="",
                tool_call_chunks=[
                    {
                        "name": tool_name,
                        "args": "{",
                        "id": tool_id,
                        "index": 0,
                        "type": "tool_call_chunk",
                    }
                ],
            )
        ),
        context=_context(),
    )

    observation = frame.observations[0]
    assert isinstance(observation, NativeMessageObservation)
    assert observation.message.tool_calls == ()
    assert observation.message.tool_call_chunks == (
        NativeToolCallChunk(index=0, arguments="{"),
    )
    assert isinstance(frame.replay.data, dict)
    replay_message = frame.replay.data["message"]
    assert isinstance(replay_message, dict)
    assert replay_message["toolCalls"] == []
    assert replay_message["toolCallChunks"] == [
        {"index": 0, "id": None, "name": None, "arguments": "{"}
    ]
    assert (
        NativeStreamPart.model_validate_json(frame.replay.model_dump_json())
        == frame.replay
    )


@pytest.mark.parametrize(
    "driver_type",
    [None, DeepAgentsV2StreamDriver, DeepAgentsV3StreamDriver],
    ids=["standalone", "v2_frame", "v3_frame"],
)
def test_tool_argument_stream_keeps_its_identity_across_empty_provider_fields(
    driver_type: type[DeepAgentsV2StreamDriver] | None,
) -> None:
    driver = None if driver_type is None else driver_type()
    context = _context()
    adapter = DeepAgentAgUiAdapter(identity=context.identity)
    messages = [
        AIMessageChunk(
            id="message-1",
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "call-1",
                    "name": "execute",
                    "args": '{"command":',
                }
            ],
        ),
        AIMessageChunk(
            id="message-1",
            content="",
            tool_call_chunks=[
                {
                    "index": 0,
                    "id": "",
                    "name": "",
                    "args": '"python analyze.py"}',
                }
            ],
        ),
        AIMessageChunk(id="message-1", content="", chunk_position="last"),
    ]
    originals = [message.model_copy(deep=True) for message in messages]
    events = [
        event
        for message in messages
        for event in (
            adapter.process(_message_part(message))
            if driver is None
            else adapter.process_frame(
                driver.normalize(_message_part(message), context=context)
            )
        )
    ]
    events.extend(adapter.finish())
    assert [type(event) for event in events] == [
        ToolCallStartEvent,
        ToolCallArgsEvent,
        ToolCallArgsEvent,
        ToolCallEndEvent,
    ]
    assert (
        len(
            {
                event.tool_call_id
                for event in events
                if isinstance(
                    event, (ToolCallStartEvent, ToolCallArgsEvent, ToolCallEndEvent)
                )
            }
        )
        == 1
    )
    assert (
        "".join(event.delta for event in events if isinstance(event, ToolCallArgsEvent))
        == '{"command":"python analyze.py"}'
    )
    assert messages == originals


def test_v2_driver_preserves_unindexed_tool_calls_from_ollama() -> None:
    # langchain-ollama 1.1.0 supplies complete calls; langchain-core 1.6.1
    # AIMessageChunk.init_tool_calls gives those calls index=None.
    message = AIMessageChunk(
        id="message-ollama",
        content="",
        tool_calls=[
            {"id": "call-echo", "name": "diagnostic_echo", "args": {"value": "hello"}},
            {"id": "call-other", "name": "diagnostic_echo", "args": {"value": "other"}},
        ],
    )
    frame = DeepAgentsV2StreamDriver().normalize(
        _message_part(message, provider="ollama"), context=_context()
    )
    observation = frame.observations[0]
    assert isinstance(observation, NativeMessageObservation)
    assert [chunk.index for chunk in observation.message.tool_call_chunks] == [
        None,
        None,
    ]
    assert [chunk.id for chunk in observation.message.tool_call_chunks] == [
        "call-echo",
        "call-other",
    ]
    assert (
        NativeMessageObservation.model_validate_json(observation.model_dump_json())
        == observation
    )


@pytest.mark.parametrize("tool_id", [None, ""])
def test_v2_driver_rejects_a_complete_tool_call_without_an_id(
    tool_id: str | None,
) -> None:
    with pytest.raises(ValueError, match="complete Tool calls require a stable ID"):
        DeepAgentsV2StreamDriver().normalize(
            _message_part(
                AIMessage(
                    id="message-1",
                    content="",
                    tool_calls=[{"name": "ls", "args": {}, "id": tool_id}],
                )
            ),
            context=_context(),
        )


@pytest.mark.parametrize(
    ("tool_id", "tool_name"), [("", "execute"), ("call-1", ""), ("", "")]
)
def test_v2_driver_rejects_unindexed_tool_chunks_without_identity(
    tool_id: str,
    tool_name: str,
) -> None:
    with pytest.raises(
        ValueError, match="unindexed tool calls require both id and name"
    ):
        DeepAgentsV2StreamDriver().normalize(
            _message_part(
                AIMessageChunk(
                    id="message-1",
                    content="",
                    tool_call_chunks=[
                        {"id": tool_id, "name": tool_name, "args": "{}", "index": None}
                    ],
                )
            ),
            context=_context(),
        )


def test_explicit_host_extractor_emits_delta_and_snapshot() -> None:
    driver = DeepAgentsV2StreamDriver(
        reasoning_extractors=(_ProviderReasoningExtractor(),)
    )
    delta = driver.normalize(
        _message_part(
            AIMessageChunk(
                id="message-1",
                content="",
                additional_kwargs={"reasoning_content": "first"},
            ),
            provider="deepseek",
        ),
        context=_context(),
    )
    snapshot = driver.normalize(
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="message-1",
                        content="visible",
                        additional_kwargs={"reasoning_content": "first second"},
                        response_metadata={"model_provider": "deepseek"},
                    )
                ],
                "reasoning_content": "business value",
            },
            "interrupts": (),
        },
        context=_context(),
    )

    delta_reasoning = delta.observations[1]
    snapshot_reasoning = snapshot.observations[1]
    assert isinstance(delta_reasoning, NativeReasoningObservation)
    assert delta_reasoning.content == "first"
    assert delta_reasoning.snapshot is False
    assert isinstance(snapshot_reasoning, NativeReasoningObservation)
    assert snapshot_reasoning.content == "first second"
    assert snapshot_reasoning.snapshot is True
    state = snapshot.observations[0]
    assert isinstance(state, NativeStateObservation)
    assert state.state["reasoning_content"] == "business value"
    snapshot_replay = snapshot.replay.model_dump_json(by_alias=True)
    assert "business value" in snapshot_replay
    assert "first second" not in snapshot_replay


class _MatchingExtractor:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def extract(
        self,
        message: BaseMessage,
        *,
        provider: str | None,
    ) -> JsonValue | None:
        del message, provider
        return "matched"


def test_driver_rejects_ambiguous_reasoning_extractors() -> None:
    driver = DeepAgentsV2StreamDriver(
        reasoning_extractors=(
            _MatchingExtractor("first"),
            _MatchingExtractor("second"),
        )
    )

    with pytest.raises(
        TinkerFinStreamProtocolError,
        match="multiple reasoning extractors",
    ):
        driver.normalize(
            _message_part(AIMessageChunk(id="message-1", content="")),
            context=_context(),
        )


def test_driver_rejects_duplicate_or_invalid_extractor_names() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        DeepAgentsV2StreamDriver(
            reasoning_extractors=(
                _MatchingExtractor("same"),
                _MatchingExtractor("same"),
            )
        )
    with pytest.raises(ValueError, match="canonical"):
        DeepAgentsV2StreamDriver(
            reasoning_extractors=(_MatchingExtractor(" invalid "),)
        )


def test_host_extractor_rejects_an_unverified_value_shape() -> None:
    driver = DeepAgentsV2StreamDriver(
        reasoning_extractors=(_ProviderReasoningExtractor(),)
    )

    with pytest.raises(TinkerFinStreamProtocolError, match="must be a string"):
        driver.normalize(
            _message_part(
                AIMessageChunk(
                    id="message-1",
                    content="",
                    additional_kwargs={"reasoning_content": {"unexpected": True}},
                ),
                provider="deepseek",
            ),
            context=_context(),
        )
