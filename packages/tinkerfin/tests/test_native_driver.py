"""Native Driver and opt-in provider reasoning contracts."""

from __future__ import annotations

import inspect

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from pydantic import JsonValue

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
from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeToolCallChunk,
    RunSourceContext,
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


def test_v2_driver_keeps_idless_followup_tool_data_as_a_chunk() -> None:
    frame = DeepAgentsV2StreamDriver().normalize(
        _message_part(
            AIMessageChunk(
                id="message-1",
                content="",
                tool_call_chunks=[
                    {
                        "name": None,
                        "args": "{",
                        "id": None,
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


def test_v2_driver_still_rejects_a_complete_tool_call_without_an_id() -> None:
    with pytest.raises(ValueError, match="complete Tool calls require a stable ID"):
        DeepAgentsV2StreamDriver().normalize(
            _message_part(
                AIMessage(
                    id="message-1",
                    content="",
                    tool_calls=[{"name": "ls", "args": {}, "id": None}],
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
