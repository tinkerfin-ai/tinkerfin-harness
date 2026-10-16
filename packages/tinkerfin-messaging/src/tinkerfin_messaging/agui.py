"""Optional AG-UI event codec and native SSE renderer."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import ClassVar, Protocol, cast

from ag_ui.core import (
    ActivityMessage,
    AssistantMessage,
    BaseEvent,
    CustomEvent,
    DeveloperMessage,
    Event,
    Message,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedOutcome,
    RunStartedEvent,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from pydantic import BaseModel, JsonValue, TypeAdapter

from tinkerfin_contracts import RunIdentity

from ._identity import required_identity
from .errors import PublicationRejected
from .protocols import MessageCodec, ProfiledMessageSource, SseRenderer
from .sources import _MappedMessageSource

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_STABLE_EVENT_FIELDS = (
    "thread_id",
    "run_id",
    "parent_run_id",
    "message_id",
    "tool_call_id",
    "parent_message_id",
    "entity_id",
    "role",
    "name",
    "tool_call_name",
    "activity_type",
    "step_name",
    "subtype",
    "code",
    "source",
    "replace",
)


class _RawEventCarrier(Protocol):
    raw_event: object


def _protocol_model_type(value: BaseModel) -> type[BaseModel]:
    """Compare standard AG-UI identity while permitting typed extension fields.

    The durable AG-UI union reconstructs standard models, keeping extension data
    as extras. A declared subclass therefore has the identity of its nearest
    AG-UI model, without weakening discriminator or correlation field checks.
    """
    for model_type in type(value).__mro__:
        if model_type.__module__.startswith("ag_ui.core."):
            return model_type
    return type(value)


def _tool_call_identity(value: ToolCall) -> tuple[object, ...]:
    function = value.function
    return (
        _protocol_model_type(value),
        value.id,
        value.type,
        function.name,
        function.arguments,
    )


def _snapshot_message_identity(value: Message) -> tuple[object, ...]:
    raw_tool_calls = value.tool_calls if isinstance(value, AssistantMessage) else None
    tool_calls = (
        tuple(_tool_call_identity(item) for item in raw_tool_calls)
        if raw_tool_calls is not None
        else None
    )
    named_message = isinstance(
        value,
        DeveloperMessage | SystemMessage | AssistantMessage | UserMessage,
    )
    return (
        _protocol_model_type(value),
        value.id,
        value.role,
        value.name if named_message else None,
        value.tool_call_id if isinstance(value, ToolMessage) else None,
        value.activity_type if isinstance(value, ActivityMessage) else None,
        tool_calls,
    )


def _freeze_protocol_value(value: JsonValue) -> object:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _freeze_protocol_value(item)) for key, item in value.items()
            )
        )
    if isinstance(value, list):
        return tuple(_freeze_protocol_value(item) for item in value)
    return value


def _run_outcome_identity(value: RunFinishedOutcome) -> tuple[object, ...]:
    raw_interrupts = (
        value.interrupts if isinstance(value, RunFinishedInterruptOutcome) else None
    )
    interrupts = (
        tuple(
            (
                type(item),
                item.id,
                item.reason,
                item.tool_call_id,
                _freeze_protocol_value(
                    None
                    if item.response_schema is None
                    else _JSON_VALUE_ADAPTER.validate_python(item.response_schema)
                ),
                item.expires_at,
            )
            for item in raw_interrupts
        )
        if raw_interrupts is not None
        else None
    )
    return _protocol_model_type(value), value.type, interrupts


def _run_input_identity(event: BaseEvent) -> object:
    """Freeze the caller's complete input without collapsing alias extras."""

    if not isinstance(event, RunStartedEvent) or event.input is None:
        return None
    value = event.input.model_dump(
        mode="json",
        by_alias=False,
        exclude_none=False,
        serialize_as_any=True,
    )
    return _freeze_protocol_value(_JSON_VALUE_ADAPTER.validate_python(value))


def _interrupt_metadata(event: BaseEvent) -> tuple[JsonValue | None, ...]:
    if not isinstance(event, RunFinishedEvent) or not isinstance(
        event.outcome,
        RunFinishedInterruptOutcome,
    ):
        return ()
    return tuple(
        None
        if interrupt.metadata is None
        else _JSON_VALUE_ADAPTER.validate_python(deepcopy(interrupt.metadata))
        for interrupt in event.outcome.interrupts
    )


def _metadata_extends(expected: JsonValue, actual: JsonValue) -> bool:
    """Allow new product metadata without changing any existing metadata value."""

    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _metadata_extends(item, actual[key])
            for key, item in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _metadata_extends(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    return expected == actual


def _event_protocol_identity(event: BaseEvent) -> tuple[object, ...]:
    """Freeze correlation and lifecycle fields while leaving product content mutable."""

    fields = type(event).model_fields
    stable = tuple(
        (name, getattr(event, name)) for name in _STABLE_EVENT_FIELDS if name in fields
    )
    messages = (
        tuple(_snapshot_message_identity(message) for message in event.messages)
        if isinstance(event, MessagesSnapshotEvent)
        else None
    )
    outcome = (
        _run_outcome_identity(event.outcome)
        if isinstance(event, RunFinishedEvent) and event.outcome is not None
        else None
    )
    return (
        _protocol_model_type(event),
        event.type,
        stable,
        messages,
        outcome,
        _run_input_identity(event),
    )


def _validate_run_started_input(event: BaseEvent) -> None:
    """Keep an optional Run input on the same lineage as its start event."""

    if not isinstance(event, RunStartedEvent) or event.input is None:
        return
    run_input = event.input
    if (
        run_input.thread_id != event.thread_id
        or run_input.run_id != event.run_id
        or run_input.parent_run_id != event.parent_run_id
    ):
        raise ValueError("AG-UI event RunIdentity input does not match its start event")


def _canonical_event(
    event: BaseEvent,
    *,
    expected_type: type[BaseModel],
    expected_identity: tuple[object, ...],
    expected_interrupt_metadata: tuple[JsonValue | None, ...],
) -> BaseEvent:
    """Normalize through durable aliases and prove protocol identity is unchanged."""

    canonical = _EVENT_ADAPTER.validate_json(
        event.model_dump_json(
            by_alias=True,
            exclude_none=False,
            serialize_as_any=True,
        )
    )
    if _protocol_model_type(canonical) is not expected_type:
        raise TypeError("AG-UI event type changed during canonical encoding")
    if _event_protocol_identity(canonical) != expected_identity:
        raise ValueError("AG-UI event changed protocol identity")
    actual_interrupt_metadata = _interrupt_metadata(canonical)
    if len(actual_interrupt_metadata) != len(expected_interrupt_metadata) or any(
        expected is not None and not _metadata_extends(expected, actual)
        for expected, actual in zip(
            expected_interrupt_metadata,
            actual_interrupt_metadata,
            strict=True,
        )
    ):
        raise ValueError("AG-UI event changed existing interrupt metadata")
    _validate_run_started_input(canonical)
    return canonical


def _validate_event_run_identity(event: BaseEvent, identity: RunIdentity) -> None:
    if isinstance(event, RunStartedEvent | RunFinishedEvent) and (
        event.thread_id != identity.thread_id or event.run_id != identity.run_id
    ):
        raise ValueError("AG-UI event RunIdentity does not match its source")
    if not isinstance(event, RunErrorEvent):
        return
    raw_event_value = cast(_RawEventCarrier, event).raw_event
    if not isinstance(raw_event_value, dict):
        return
    raw_event = _JSON_VALUE_ADAPTER.validate_python(raw_event_value)
    if not isinstance(raw_event, dict):
        return
    for field, expected in (
        ("threadId", identity.thread_id),
        ("runId", identity.run_id),
    ):
        value = raw_event.get(field)
        if value is not None and value != expected:
            raise ValueError("AG-UI error RunIdentity does not match its source")


class _AgUiRunSource(_MappedMessageSource[BaseEvent, BaseEvent]):
    """Retain the AG-UI profile while validating live events and cancellation tails."""

    def __init__(
        self,
        source: ProfiledMessageSource[BaseEvent, BaseEvent],
        transform: Callable[[BaseEvent], Awaitable[BaseEvent]],
    ) -> None:
        super().__init__(source, transform)
        self._identity = required_identity(source.messaging_identity)

    @property
    def messaging_identity(self) -> RunIdentity:
        return self._identity

    @property
    def messaging_codec_profile(self) -> str:
        return "agui.event"

    @property
    def messaging_source_type(self) -> type[BaseEvent]:
        return BaseEvent

    @property
    def messaging_replay_type(self) -> type[BaseEvent]:
        return BaseEvent

    @staticmethod
    def prepare(
        source: ProfiledMessageSource[BaseEvent, BaseEvent],
        *,
        transform_event: (
            Callable[[BaseEvent], BaseEvent | Awaitable[BaseEvent]] | None
        ) = None,
    ) -> ProfiledMessageSource[BaseEvent, BaseEvent]:
        """Prepare an AG-UI event source for durable delivery and optional transformation.

        Creating the adapter does not prepare or consume the source. Messaging prepares
        only the selected producer before announcing readiness; replay closes an unused
        candidate without preparing it. Cancellation waits for the first transformed
        event or failed pull, then transforms the source's finite cancellation tail.

        Args:
            source: Unconsumed, closeable AG-UI source with a complete ``agui.event``
                profile, immutable run identity, and first-event cancellation support.
                The returned adapter owns its cleanup only after construction succeeds.
            transform_event: Optional synchronous or asynchronous content or metadata
                transform. It must preserve the event type, protocol identities,
                complete Run input, and existing interrupt metadata.

        Returns:
            A single-use source for an inferred AG-UI Messaging channel. Closing it
            settles active work and closes the input source. If cleanup fails, a later
            explicit ``aclose()`` can finish it; successful close is idempotent.

        Raises:
            TypeError: The source profile, cancellation capability, or transform is
                invalid. A synchronous construction failure leaves source cleanup to
                the caller.
            ValueError: An identity is invalid or a transformed event changes an
                established protocol contract.
            RuntimeError: A transform tries to close its own active adapter. Close it
                after the pull returns or from the consuming context's cleanup.
            BaseException: Source preparation, transformation, cancellation, or cleanup
                fails during use; cancellation and process control remain unchanged.
        """

        if not isinstance(source, ProfiledMessageSource):
            raise TypeError("source must be a profiled AG-UI MessageSource")
        if (
            source.messaging_codec_profile != "agui.event"
            or source.messaging_source_type is not BaseEvent
            or source.messaging_replay_type is not BaseEvent
        ):
            raise TypeError("source must publish the agui.event BaseEvent profile")
        identity = required_identity(source.messaging_identity)
        if getattr(source, "messaging_cancel_waits_for_first_item", None) is not True:
            raise TypeError(
                "AG-UI source must wait for its first item before cancellation"
            )
        if not callable(getattr(source, "messaging_cancel_callback", None)):
            raise TypeError("AG-UI source must publish cancellation")
        if transform_event is not None and not callable(transform_event):
            raise TypeError("transform_event must be a callable or None")

        async def transform(event: BaseEvent) -> BaseEvent:
            expected_type = _protocol_model_type(event)
            expected_identity = _event_protocol_identity(event)
            expected_metadata = _interrupt_metadata(event)
            value = event if transform_event is None else transform_event(event)
            if inspect.isawaitable(value):
                value = await value
            validated = _EVENT_ADAPTER.validate_python(value)
            if _protocol_model_type(validated) is not expected_type:
                raise TypeError("transform_event must preserve the AG-UI event type")
            # Canonical aliases protect correlation fields even when a product adds
            # typed extension fields to an AG-UI event. See the run-source contracts.
            transformed = _canonical_event(
                validated,
                expected_type=expected_type,
                expected_identity=expected_identity,
                expected_interrupt_metadata=expected_metadata,
            )
            _validate_event_run_identity(transformed, identity)
            return transformed

        return _AgUiRunSource(source, transform)


class AgUiCodec(
    MessageCodec[BaseEvent, BaseEvent],
    SseRenderer[BaseEvent],
):
    """Persist complete AG-UI events under a schema-stable codec identifier."""

    codec_id: ClassVar[str] = "agui.event"
    messaging_source_type: ClassVar[type[BaseEvent]] = BaseEvent
    messaging_replay_type: ClassVar[type[BaseEvent]] = BaseEvent

    def validate_publication(self, item: BaseEvent, *, identity: RunIdentity) -> None:
        """Allow custom notifications without injecting AG-UI source lifecycles."""
        if not isinstance(item, CustomEvent):
            raise PublicationRejected(identity=identity, reason="custom_event_required")

    def starts_publication(self, item: BaseEvent, *, identity: RunIdentity) -> bool:
        """Open publication only when the main run has started."""
        return (
            isinstance(item, RunStartedEvent)
            and item.thread_id == identity.thread_id
            and item.run_id == identity.run_id
        )

    def ends_publication(self, item: BaseEvent, *, identity: RunIdentity) -> bool:
        """Seal publication at the main terminal defined by AG-UI 0.1.19."""
        if isinstance(item, RunFinishedEvent):
            return (
                item.thread_id == identity.thread_id and item.run_id == identity.run_id
            )
        if isinstance(item, RunErrorEvent):
            raw = item.raw_event
            source = (
                cast(Mapping[object, object], raw).get("source")
                if isinstance(raw, dict)
                else None
            )
            return not (
                isinstance(source, dict)
                and cast(Mapping[object, object], source).get("agentType") == "subagent"
            )
        return False

    def encode(self, item: BaseEvent) -> bytes:
        """Validate and encode one event with protocol field aliases."""

        event = _EVENT_ADAPTER.validate_python(item)
        canonical = _canonical_event(
            event,
            expected_type=_protocol_model_type(event),
            expected_identity=_event_protocol_identity(event),
            expected_interrupt_metadata=_interrupt_metadata(event),
        )
        return canonical.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode()

    def decode(self, payload: bytes) -> BaseEvent:
        """Decode and validate one complete discriminated AG-UI event."""

        return _EVENT_ADAPTER.validate_json(payload)

    def render(self, *, seq: int, payload: BaseEvent) -> bytes:
        """Render the durable sequence and native AG-UI JSON as one SSE frame."""

        encoded = self.encode(payload)
        return b"id: " + str(seq).encode() + b"\ndata: " + encoded + b"\n\n"
