"""Deep Agents invocation Drivers and host-defined reasoning extraction."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol, cast, runtime_checkable

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langgraph.types import Command, StreamMode
from pydantic import JsonValue, TypeAdapter, ValidationError

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    NativeObservation,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeTaskObservation,
    RunIdentity,
    RunSourceContext,
)
from tinkerfin_native_stream import (
    NativeExtraStreamPart,
    NativeMessageStreamPart,
    NativeStreamFrame,
    NativeStreamPart,
    NativeTasksStreamPart,
    NativeTaskStartPayload,
    NativeUpdatesStreamPart,
    NativeValidatedStreamPart,
    NativeValuesStreamPart,
    qualified_name,
    validate_native_stream_part,
)
from tinkerfin_native_stream.public import (
    normalize_operational_data,
    public_extra_data,
    sanitize_public_data,
)

from ._agui_lineage_state import RESUME_METADATA_KEY, RUNTIME_PROFILE_METADATA_KEY
from ._observation import native_observation
from .errors import TinkerFinStreamProtocolError

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

_REQUIRED_MODES: tuple[StreamMode, ...] = ("messages", "tasks", "values")
_SUPPORTED_EXTRA_MODES: frozenset[StreamMode] = frozenset(
    {"updates", "checkpoints", "debug", "custom"}
)


def _validate_run_state(value: object) -> None:
    # Recovery evidence is saver-owned. Reject host state and Command updates that
    # try to introduce a same-named Graph channel, before any execution or I/O.
    if isinstance(value, Command):
        value = cast(Command[object], value).update
    if isinstance(value, Mapping):
        keys = list(cast(Mapping[object, object], value))
    elif isinstance(value, (list, tuple)):
        keys: list[object] = []
        for raw_item in cast(Sequence[object], value):
            if isinstance(raw_item, (list, tuple)):
                pair = cast(Sequence[object], raw_item)
                if len(pair) == 2:
                    keys.append(pair[0])
    else:
        return
    if RESUME_METADATA_KEY in keys:
        raise ValueError("input cannot replace framework-owned resume evidence")


def _bind_deep_agents_invocation(
    signature: inspect.Signature,
    args: tuple[object, ...],
    options: Mapping[str, object],
    *,
    identity: RunIdentity | None,
    runtime_profile: str | None,
    require_semantic_modes: bool,
    version: Literal["v2", "v3"],
) -> inspect.BoundArguments:
    """Apply one explicit Deep Agents stream contract before source creation."""

    if identity is not None and not isinstance(identity, RunIdentity):
        raise TypeError("identity must be a RunIdentity or None")
    if (identity is None) != (runtime_profile is None):
        raise TypeError("identity and runtime_profile must be supplied together")
    label = f"Deep Agents {version}"
    bound = signature.bind(*args, **dict(options))
    _validate_run_state(bound.arguments.get("input"))
    parameters = signature.parameters
    variable_keyword = next(
        (
            name
            for name, parameter in parameters.items()
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        ),
        None,
    )

    def read(name: str) -> object | None:
        parameter = parameters.get(name)
        if (
            parameter is not None
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ):
            return bound.arguments.get(name)
        if variable_keyword is None:
            return None
        raw_values = bound.arguments.get(variable_keyword)
        if not isinstance(raw_values, Mapping):
            return None
        return cast(Mapping[object, object], raw_values).get(name)

    def supplied(name: str) -> bool:
        parameter = parameters.get(name)
        if (
            parameter is not None
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ):
            return name in bound.arguments
        raw_values = (
            None if variable_keyword is None else bound.arguments.get(variable_keyword)
        )
        return isinstance(raw_values, Mapping) and name in raw_values

    def write(name: str, value: object) -> None:
        parameter = parameters.get(name)
        if (
            parameter is not None
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ):
            bound.arguments[name] = value
            return
        if variable_keyword is None:
            raise TypeError(f"Graph astream must accept the {name!r} option")
        raw_values = bound.arguments.get(variable_keyword)
        values: dict[object, object] = (
            dict(cast(Mapping[object, object], raw_values))
            if isinstance(raw_values, Mapping)
            else {}
        )
        values[name] = value
        bound.arguments[variable_keyword] = values

    raw_config = read("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(cast(Mapping[str, object], raw_config))
    else:
        raise TypeError("config must be a mapping or None")
    raw_configurable = config.get("configurable")
    if raw_configurable is None:
        configurable: dict[str, object] = {}
    elif isinstance(raw_configurable, Mapping):
        configurable = dict(cast(Mapping[str, object], raw_configurable))
    else:
        raise TypeError("config.configurable must be a mapping")
    if any(
        isinstance(key, str) and key.startswith("__pregel_")
        for values in (config, configurable)
        for key in values
    ):
        raise ValueError("config cannot replace framework-owned execution resources")
    if identity is not None:
        configured_thread = configurable.get("thread_id")
        if configured_thread is not None and configured_thread != identity.thread_id:
            raise ValueError("config thread_id must equal identity.thread_id")
        configurable["thread_id"] = identity.thread_id
        configured_profile = configurable.get(RUNTIME_PROFILE_METADATA_KEY)
        if configured_profile is not None and configured_profile != runtime_profile:
            raise ValueError(
                "config Runtime Profile must equal the selected TinkerFin Profile"
            )
        configurable[RUNTIME_PROFILE_METADATA_KEY] = runtime_profile
    config["configurable"] = configurable
    write("config", config)

    if read("output_keys") is not None:
        raise ValueError(
            f"{label} output_keys must be None so values remains a complete state "
            "snapshot"
        )

    raw_modes = read("stream_mode")
    if supplied("stream_mode") and raw_modes is None:
        raise ValueError(
            f"{label} stream_mode must include messages, tasks, and values"
        )
    if raw_modes is None:
        modes: tuple[object, ...] = ()
    elif isinstance(raw_modes, str):
        modes = (raw_modes,)
    elif isinstance(raw_modes, Sequence):
        modes = tuple(cast(Sequence[object], raw_modes))
    else:
        raise TypeError(f"{label} stream_mode must be a supported mode or sequence")
    duplicate = tuple(mode for index, mode in enumerate(modes) if mode in modes[:index])
    if duplicate:
        raise ValueError(f"{label} stream_mode contains duplicate modes: {duplicate!r}")
    supported = frozenset((*_REQUIRED_MODES, *_SUPPORTED_EXTRA_MODES))
    unsupported = tuple(
        mode for mode in modes if not isinstance(mode, str) or mode not in supported
    )
    if unsupported:
        raise ValueError(
            f"{label} stream_mode contains unsupported modes: {unsupported!r}"
        )
    if supplied("stream_mode") and require_semantic_modes:
        missing = tuple(mode for mode in _REQUIRED_MODES if mode not in modes)
        if missing:
            raise ValueError(
                f"{label} stream_mode is missing required modes: {missing!r}"
            )
    extra_modes = tuple(mode for mode in modes if mode not in _REQUIRED_MODES)
    write("stream_mode", (*_REQUIRED_MODES, *extra_modes))

    requested_version = read("version")
    if requested_version is not None and requested_version != version:
        raise ValueError(f"{label} Driver requires version={version!r}")
    write("version", version)
    subgraphs = read("subgraphs")
    if subgraphs is not None and subgraphs is not True:
        raise ValueError(f"{label} Driver requires subgraphs=True")
    write("subgraphs", True)
    return bound


def _public_source_value(value: object, recorded: JsonValue | None) -> JsonValue:
    """Preserve an established opaque omission without serializing host resources."""

    if (
        isinstance(recorded, dict)
        and set(recorded) == {"$type", "class"}
        and recorded["$type"] == "omitted"
        and recorded["class"] == qualified_name(value)
    ):
        return recorded
    return normalize_operational_data(value)


def _public_message_record(
    message: BaseMessage, record: NativeMessageRecord
) -> dict[str, JsonValue]:
    payload = cast(dict[str, JsonValue], record.model_dump(mode="json", by_alias=True))
    payload["content"] = _public_source_value(message.content, record.content)
    payload["responseMetadata"] = _public_source_value(
        message.response_metadata, record.response_metadata
    )
    payload["usageMetadata"] = _public_source_value(
        getattr(message, "usage_metadata", None), record.usage_metadata
    )
    if isinstance(message, ToolMessage):
        payload["artifact"] = _public_source_value(message.artifact, record.artifact)
    if isinstance(message, AIMessage) and not isinstance(message, AIMessageChunk):
        payload["toolCalls"] = [
            {
                **call.model_dump(mode="json", by_alias=True),
                "arguments": normalize_operational_data(raw["args"]),
            }
            for call, raw in zip(record.tool_calls, message.tool_calls, strict=True)
        ]
    return payload


def _canonical_replay(
    part: NativeValidatedStreamPart,
    observation: NativeObservation,
) -> NativeStreamPart:
    """Build finite public replay independently of diagnostic capture tags.

    Replay deliberately excludes Runtime identity and timestamps because the stream
    source owns those separately. The observation supplies typed record structure
    and established omissions for opaque host resources. Supported public values use
    the same plain JSON normalization as live protocol conversion; literal business
    type tags are never interpreted as serialized Python objects.
    """

    # Extra modes deliberately remain metadata-only in the user Trace Ledger, but
    # Native replay is a transport contract and must retain their finite payload.
    # Normalizing here keeps that provider-shaped value inside the Driver boundary;
    # downstream SSE and Messaging consumers receive only the detached result.
    payload = (
        public_extra_data(part)
        if isinstance(part, NativeExtraStreamPart | NativeUpdatesStreamPart)
        else cast(
            JsonValue,
            observation.model_dump(
                mode="json",
                by_alias=True,
                exclude={
                    "identity",
                    "kind",
                    "monotonic_ns",
                    "graph_namespace",
                    "graph_origin",
                    "subagent_requests",
                    "observed_at",
                },
            ),
        )
    )
    if isinstance(payload, dict):
        if isinstance(part, NativeMessageStreamPart) and isinstance(
            observation, NativeMessageObservation
        ):
            payload["message"] = _public_message_record(
                part.data.message, observation.message
            )
            payload["metadata"] = normalize_operational_data(dict(part.data.metadata))
        elif isinstance(part, NativeTasksStreamPart) and isinstance(
            observation, NativeTaskObservation
        ):
            if isinstance(part.data, NativeTaskStartPayload):
                value = _public_source_value(part.data.input, observation.input)
                payload["input"] = (
                    value if part.data.name == "tools" else sanitize_public_data(value)
                )
                payload["metadata"] = (
                    {}
                    if part.data.metadata is None
                    else sanitize_public_data(dict(part.data.metadata))
                )
            else:
                payload["result"] = sanitize_public_data(
                    _public_source_value(part.data.result, observation.result)
                )
        elif isinstance(part, NativeValuesStreamPart) and isinstance(
            observation, NativeStateObservation
        ):
            payload["state"] = sanitize_public_data(
                {
                    key: _public_source_value(part.data[key], value)
                    for key, value in observation.state.items()
                }
            )
            raw_messages = part.data.get("messages", ())
            if not isinstance(raw_messages, (tuple, list)):
                raise TypeError("Native state messages must be a sequence")
            messages: list[JsonValue] = []
            for message, record in zip(
                cast(Sequence[object], raw_messages), observation.messages, strict=True
            ):
                if not isinstance(message, BaseMessage):
                    raise TypeError("Native state messages must be live messages")
                messages.append(_public_message_record(message, record))
            payload["messages"] = messages
    interrupt_records = (
        observation.interrupts
        if isinstance(observation, NativeTaskObservation | NativeStateObservation)
        else ()
    )
    interrupts = tuple(
        cast(JsonValue, item.model_dump(mode="json", by_alias=True))
        for item in interrupt_records
    )
    return NativeStreamPart(
        type=part.type,
        ns=part.ns,
        data=payload,
        interrupts=interrupts,
    )


@runtime_checkable
class ReasoningExtractor(Protocol):
    """Synchronously extract one host-verified reasoning value from a message.

    Each call receives a detached message copy. Implementations must perform no I/O and
    return only standard JSON values; the Driver validates and detaches the result again
    before it can enter a Runtime observation.
    """

    @property
    def name(self) -> str:
        """Return the stable extractor identity used in observations."""

        ...

    def extract(
        self,
        message: BaseMessage,
        *,
        provider: str | None,
    ) -> JsonValue | None:
        """Return reasoning content only when the provider and message both match."""

        ...


@runtime_checkable
class NativeStreamDriver(Protocol):
    """Bind and normalize one concrete third-party Native stream profile.

    The Driver owns upstream invocation requirements as well as per-part conversion.
    TinkerFin never probes an event to guess a Driver and never changes Drivers during
    one Run. Every implementation must map its source into the same canonical frame.
    """

    def bind_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
        runtime_profile: str,
    ) -> inspect.BoundArguments:
        """Bind one upstream call and apply this Driver's complete stream profile.

        Args:
            signature: Concrete upstream ``astream`` signature.
            args: Positional arguments supplied by the Runtime caller.
            options: Keyword arguments supplied by the Runtime caller.
            identity: Canonical Run identity that must own the upstream thread.
            runtime_profile: Canonical Profile identity persisted with checkpoints.

        Returns:
            Bound arguments ready for lineage preparation and invocation.

        Raises:
            TypeError: The upstream signature or an option has an invalid shape.
            ValueError: Caller options conflict with the Driver profile.
        """

        ...

    def validate(self, part: object) -> NativeValidatedStreamPart:
        """Return the immutable validated representation of one upstream part."""

        ...

    def normalize(
        self,
        part: object,
        *,
        context: RunSourceContext,
    ) -> NativeStreamFrame:
        """Return validated data and ordered protocol-neutral observations."""

        ...


class DeepAgentsV2StreamDriver:
    """Own the locked Deep Agents 0.7.5 and LangGraph v2 stream profile.

    The Driver fixes ``messages/tasks/values``, third-party ``version="v2"``, complete
    state snapshots, and subgraph streaming. The version identifier remains confined
    to this integration boundary and never becomes a TinkerFin protocol field.
    ``test_v2_profile_owns_factory_identity_and_complete_invocation_binding`` and the
    locked Deep Agents 0.7.5 corpus protect these upstream assumptions; downstream
    consumers are instead tested against ``NativeStreamFrame``.

    Args:
        reasoning_extractors: Explicit provider extractors. Omitting the tuple means
            no provider reasoning observation can be emitted.

    Raises:
        TypeError: An extractor does not implement ``ReasoningExtractor``.
        ValueError: Extractor names are non-canonical or duplicated.
    """

    def __init__(
        self,
        *,
        reasoning_extractors: tuple[ReasoningExtractor, ...] = (),
    ) -> None:
        """Freeze and validate the ordered provider reasoning extractor registry."""

        names: list[str] = []
        for extractor in reasoning_extractors:
            if not isinstance(extractor, ReasoningExtractor):
                raise TypeError("reasoning extractor must implement ReasoningExtractor")
            name = extractor.name
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("reasoning extractor name must be canonical text")
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError("reasoning extractor names must be unique")
        self._reasoning_extractors = reasoning_extractors

    @property
    def reasoning_extractors(self) -> tuple[ReasoningExtractor, ...]:
        """Return the immutable ordered extractor registration."""

        return self._reasoning_extractors

    def bind_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
        runtime_profile: str,
    ) -> inspect.BoundArguments:
        """Bind one call to the complete locked LangGraph v2 profile.

        Args:
            signature: Concrete upstream ``astream`` signature.
            args: Positional invocation values.
            options: Keyword invocation values, including supported extra modes.
            identity: Canonical identity whose thread ID is injected into config.
            runtime_profile: Selected Profile identity injected into private config.

        Returns:
            Bound arguments containing the required modes, v2, and subgraphs.

        Raises:
            TypeError: Config or stream options have invalid container types.
            ValueError: Identity, modes, output keys, version, or subgraphs conflict
                with the current Driver contract.
        """

        return _bind_deep_agents_invocation(
            signature,
            args,
            options,
            identity=identity,
            runtime_profile=runtime_profile,
            require_semantic_modes=True,
            version="v2",
        )

    def bind_graph_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
    ) -> inspect.BoundArguments:
        """Bind direct Graph streaming to the locked v2 envelope contract.

        Unlike a managed Runtime call, this boundary does not inject TinkerFin identity
        or Profile checkpoint metadata. Required semantic modes remain internal routing
        inputs and are filtered by ``DeepAgentGraph`` when the caller requested a subset.

        Args:
            signature: Concrete upstream ``astream`` signature.
            args: Positional direct Graph values.
            options: Keyword direct Graph options.

        Returns:
            Bound v2 Graph arguments without managed Runtime identity state.
        """

        return _bind_deep_agents_invocation(
            signature,
            args,
            options,
            identity=None,
            runtime_profile=None,
            require_semantic_modes=False,
            version="v2",
        )

    def validate(self, part: object) -> NativeValidatedStreamPart:
        """Validate one live part against the current Deep Agents v2 contract."""

        return validate_native_stream_part(part)

    def normalize(
        self,
        part: object,
        *,
        context: RunSourceContext,
    ) -> NativeStreamFrame:
        """Validate one part and produce ordered Runtime observations.

        Args:
            part: Live upstream StreamPart object.
            context: Immutable Run identity, lineage, and privacy context.

        Returns:
            One frame containing the validated part, its core observation, optional
            explicitly enabled reasoning observations, and root interrupt IDs.

        Raises:
            TinkerFinStreamProtocolError: Multiple extractors claim one message or a
                reasoning message lacks a stable ID.
            NativeStreamContractError: The current upstream part is malformed.
        """

        canonical = self.validate(part)
        primary = native_observation(canonical, context=context)
        reasoning = self._reasoning_observations(
            canonical,
            context=context,
            observed_at=primary.observed_at,
            monotonic_ns=primary.monotonic_ns,
        )
        root_interrupt_ids = (
            tuple(value.id for value in canonical.interrupts)
            if isinstance(canonical, NativeValuesStreamPart) and not canonical.ns
            else ()
        )
        return NativeStreamFrame(
            canonical=canonical,
            observations=(primary, *reasoning),
            replay=_canonical_replay(canonical, primary),
            root_interrupt_ids=root_interrupt_ids,
        )

    def _reasoning_observations(
        self,
        part: NativeValidatedStreamPart,
        *,
        context: RunSourceContext,
        observed_at: datetime,
        monotonic_ns: int,
    ) -> tuple[NativeObservation, ...]:
        if not self._reasoning_extractors:
            return ()
        messages: tuple[tuple[BaseMessage, str | None], ...]
        if isinstance(part, NativeMessageStreamPart):
            raw_provider = (part.data.metadata.model_extra or {}).get("ls_provider")
            provider = raw_provider if isinstance(raw_provider, str) else None
            messages = ((part.data.message, provider),)
        elif isinstance(part, NativeValuesStreamPart):
            raw_messages = part.data.get("messages", ())
            if not isinstance(raw_messages, Sequence) or isinstance(
                raw_messages,
                (str, bytes, bytearray),
            ):
                raise TypeError("values messages must be a sequence")
            values = cast(Sequence[object], raw_messages)
            if any(not isinstance(value, BaseMessage) for value in values):
                raise TypeError("values messages must contain LangChain messages")
            messages = tuple(
                (
                    message,
                    (
                        raw_provider
                        if isinstance(
                            raw_provider := message.response_metadata.get(
                                "model_provider"
                            ),
                            str,
                        )
                        else None
                    ),
                )
                for message in cast(Sequence[BaseMessage], values)
            )
        else:
            return ()

        observations: list[NativeObservation] = []
        for message, provider in messages:
            matches: list[tuple[str, JsonValue]] = []
            for extractor in self._reasoning_extractors:
                value = extractor.extract(
                    message.model_copy(deep=True),
                    provider=provider,
                )
                if value is None:
                    continue
                try:
                    detached = _JSON_VALUE_ADAPTER.validate_python(value)
                except ValidationError as error:
                    raise TinkerFinStreamProtocolError(
                        "reasoning extractors must return standard JSON values"
                    ) from error
                matches.append((extractor.name, detached))
            if len(matches) > 1:
                raise TinkerFinStreamProtocolError(
                    "multiple reasoning extractors matched one message"
                )
            if not matches:
                continue
            message_id = message.id
            if not isinstance(message_id, str) or not message_id:
                raise TinkerFinStreamProtocolError(
                    "reasoning observations require a stable message ID"
                )
            extractor_name, content = matches[0]
            observations.append(
                NativeReasoningObservation(
                    identity=context.identity,
                    graph_namespace=part.ns,
                    message_id=message_id,
                    extractor=extractor_name,
                    content=content,
                    snapshot=not isinstance(message, AIMessageChunk),
                    observed_at=observed_at,
                    monotonic_ns=monotonic_ns,
                )
            )
        return tuple(observations)


class DeepAgentsV3StreamDriver(DeepAgentsV2StreamDriver):
    """Normalize canonical parts carried by LangGraph's explicit v3 event stream.

    The matching Runtime Profile owns conversion from public v3 ``ProtocolEvent``
    objects into the same live canonical part boundary used by the rest of TinkerFin.
    This Driver keeps invocation selection explicit and reuses the shared semantic
    normalization without exposing v3 to Runtime observers or Trace consumers.
    """

    def bind_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
        runtime_profile: str,
    ) -> inspect.BoundArguments:
        """Bind a managed Graph call to the explicit v3 Profile source."""

        return _bind_deep_agents_invocation(
            signature,
            args,
            options,
            identity=identity,
            runtime_profile=runtime_profile,
            require_semantic_modes=True,
            version="v3",
        )

    def bind_graph_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
    ) -> inspect.BoundArguments:
        """Bind direct Graph streaming to the explicit v3 Profile source."""

        return _bind_deep_agents_invocation(
            signature,
            args,
            options,
            identity=None,
            runtime_profile=None,
            require_semantic_modes=False,
            version="v3",
        )


__all__ = [
    "DeepAgentsV2StreamDriver",
    "DeepAgentsV3StreamDriver",
    "NativeStreamDriver",
    "NativeStreamFrame",
    "ReasoningExtractor",
]
