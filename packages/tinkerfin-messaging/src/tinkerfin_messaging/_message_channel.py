"""Message channel binding, replay, publication, and control operations."""

from __future__ import annotations

__all__ = [
    "_commit_inferred_binding",
    "_require_read_codec",
    "_resolve_binding",
    "_resolve_identity",
    "_validate_page",
    "_validate_profile_types",
    "_wrap",
    "get_run_status",
]

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, NoReturn, TypeAlias, TypeVar, cast
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from ._messaging_boundary import (
    _await_backend,
    _ContextCancelCallback,
    _normalize_cancel_callback,
    _validate_optional_cursor,
)
from ._messaging_ledger import BackendRunHandle, PreparedRun
from ._producer_runtime import _open_recoverable_source, _OwnerLease
from ._tasks import capture, join_owned_task, retain_failure, select_failure
from .backend import (
    RunStatus,
    is_active_run_status,
    is_failed_run_status,
    is_final_run_status,
)
from .errors import (
    BackendOwnershipLost,
    CodecMismatch,
    InvalidCursor,
    MessagingBackendProtocolError,
    RunProducerFailed,
    SourceProfileMismatch,
)
from .models import DecodedMessage, MessageEnvelope, RecoverableMessage
from .protocols import (
    MessageCodec,
    MessagePublicationPolicy,
    MessageSource,
    ProfiledMessageSource,
    RecoverableSource,
    SseRenderer,
)

if TYPE_CHECKING:
    from .messaging import (
        CancelCallback,
        CommittedCallback,
        MessageChannel,
        MessageSubscription,
        _PreflightRegistration,
    )

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProfileSourceT = TypeVar("ProfileSourceT")
ProfileReplayT = TypeVar("ProfileReplayT")
PreflightT = TypeVar("PreflightT")
_CodecInputTransform: TypeAlias = Callable[[object], object]
_DeliveryCallback: TypeAlias = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _RetainedPreflightOutcome(Generic[PreflightT]):
    """Carry a preflight result or BaseException without leaking a child-task error."""

    result: PreflightT | None = None
    error: BaseException | None = None


async def _retained_preflight(
    operation: Callable[[], Awaitable[PreflightT]],
    *,
    started: asyncio.Event | None = None,
) -> _RetainedPreflightOutcome[PreflightT]:
    """Run one preflight to settlement and return its complete outcome as data."""

    if started is not None:
        started.set()
    try:
        return _RetainedPreflightOutcome(result=await operation())
    except BaseException as error:  # noqa: BLE001 - the caller re-raises this outcome
        return _RetainedPreflightOutcome(error=error)


async def _await_retained_preflight(
    task: asyncio.Task[_RetainedPreflightOutcome[PreflightT]],
    *,
    cancel_requested: asyncio.Event,
    task_started: asyncio.Event,
    close_late_result: Callable[[PreflightT], Awaitable[None]],
) -> PreflightT:
    """Keep preflight owned across repeated cancellation and close late success."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    started_waiter = asyncio.create_task(
        task_started.wait(),
        name="tinkerfin-messaging-preflight-started",
    )
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            next_count = current.cancelling() if current is not None else 0
            if next_count > cancel_count:
                first_cancellation = caller_cancellation is None
                if first_cancellation:
                    caller_cancellation = cancellation
                cancel_requested.set()
                cancel_count = next_count
                if first_cancellation:
                    while not started_waiter.done() and not task.done():
                        try:
                            await asyncio.shield(started_waiter)
                        except asyncio.CancelledError as repeated:
                            repeated_count = (
                                current.cancelling() if current is not None else 0
                            )
                            if repeated_count > cancel_count:
                                cancel_count = repeated_count
                                continue
                            if started_waiter.done() or task.done():
                                break
                            raise repeated
                    if not task.done():
                        task.cancel()
                continue
            if task.done():
                break
            raise

    if not started_waiter.done():  # pragma: no cover - task start always sets the event
        started_waiter.cancel()
    await asyncio.gather(started_waiter, return_exceptions=True)
    outcome = task.result()
    if caller_cancellation is not None:
        failure: BaseException = caller_cancellation
        if outcome.result is not None:

            async def close_late() -> _RetainedPreflightOutcome[None]:
                return await _retained_preflight(
                    lambda: close_late_result(cast(PreflightT, outcome.result))
                )

            close_task = asyncio.create_task(
                close_late(),
                name="tinkerfin-messaging-late-delivery-close",
            )
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as repeated:
                    next_count = current.cancelling() if current is not None else 0
                    if next_count > cancel_count:
                        cancel_requested.set()
                        cancel_count = next_count
                        continue
                    if close_task.done():
                        break
                    raise repeated
            close_outcome = close_task.result()
            close_error = close_outcome.error
            if close_error is not None:
                failure = select_failure(failure, close_error)
                caller_cancellation.add_note(
                    "Late Messaging delivery cleanup also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
        if outcome.error is not None:
            failure = select_failure(failure, outcome.error)
            caller_cancellation.add_note(
                "Messaging preflight also failed: "
                f"{type(outcome.error).__name__}: {outcome.error}"
            )
            for note in getattr(outcome.error, "__notes__", ()):
                caller_cancellation.add_note(note)
        raise failure
    if outcome.error is not None:
        raise outcome.error.with_traceback(outcome.error.__traceback__)
    if outcome.result is None:  # pragma: no cover - every operation returns delivery
        raise RuntimeError("Messaging preflight produced no delivery")
    return outcome.result


def _validate_delivery_callback(
    name: str,
    callback: _DeliveryCallback | None,
) -> None:
    if callback is not None and not callable(callback):
        raise TypeError(f"{name} must be an async callable or None")


async def _settle_delivery_step(
    operation: Awaitable[PreflightT],
    *,
    preflight: _PreflightRegistration | None = None,
) -> PreflightT:
    """Complete accepted delivery work without abandoning it on caller cancellation."""

    task = asyncio.create_task(
        capture(operation), name="tinkerfin-messaging-delivery-step"
    )
    previous_owner = None if preflight is None else preflight.owner
    if preflight is not None:
        # The callback may close Messaging itself. Shutdown must recognize its
        # effective preflight owner rather than wait for the parent joining it.
        preflight.owner = cast(asyncio.Task[object], task)
    try:
        return await join_owned_task(task)
    finally:
        if preflight is not None and previous_owner is not None:
            preflight.owner = previous_owner


async def _invoke_delivery_callback(
    name: str,
    callback: _DeliveryCallback | None,
    *,
    preflight: _PreflightRegistration | None = None,
) -> None:
    if callback is None:
        return

    async def invoke() -> None:
        result = callback()
        if not inspect.isawaitable(result):
            raise TypeError(f"{name} must return an awaitable")
        await result

    await _settle_delivery_step(invoke(), preflight=preflight)


def _retain_settlement_failure(
    primary: BaseException,
    secondary: BaseException,
) -> BaseException:
    """Preserve setup and cleanup failures, with control ahead of cancellation.

    Notes aid diagnostics, but the original objects and causes remain in the
    exception graph. Source-preparation contracts cover combined failures.
    """

    outcome = select_failure(primary, secondary)
    additional = secondary if outcome is primary else primary
    outcome.add_note(
        "Messaging preflight settlement also failed: "
        f"{type(additional).__name__}: {additional}"
    )
    return outcome


def _settle_unregistered_delivery(
    primary: BaseException,
    *,
    source: MessageSource[object] | None,
    on_delivery_not_started: _DeliveryCallback | None,
) -> Coroutine[object, object, NoReturn]:
    """Settle delivery inputs rejected before preflight registration.

    A closed Messaging facade cannot retain a preflight task, but it still owns the
    unused ordinary source and the host's not-started outcome. Recoverable factories are
    never opened at this boundary, so only their host callback requires settlement.
    """

    async def settle() -> NoReturn:
        outcome = primary

        def retain(error: BaseException) -> None:
            nonlocal outcome
            outcome = _retain_settlement_failure(outcome, error)

        if source is not None:
            try:
                await source.aclose()
            except BaseException as close_error:  # noqa: BLE001 - settle remaining callback
                retain(close_error)
        try:
            _validate_delivery_callback(
                "on_delivery_not_started",
                on_delivery_not_started,
            )
            await _invoke_delivery_callback(
                "on_delivery_not_started",
                on_delivery_not_started,
            )
        except BaseException as callback_error:  # noqa: BLE001 - preserve failure priority
            retain(callback_error)
        raise outcome

    return _settle_delivery_step(settle())


def _raise_if_start_cancelled(cancel_requested: asyncio.Event) -> None:
    """Stop a retained preflight before it transfers ownership to a producer."""

    if cancel_requested.is_set():
        raise asyncio.CancelledError("Messaging delivery was cancelled before startup")


def _resolve_identity(source: object, identity: RunIdentity | None) -> RunIdentity:
    """Resolve one explicit or immutable source identity before side effects."""

    explicit = None if identity is None else required_identity(identity)
    source_identity = getattr(source, "messaging_identity", None)
    profiled = None if source_identity is None else required_identity(source_identity)
    if profiled is None:
        if explicit is None:
            raise TypeError(
                "source does not advertise messaging_identity; provide "
                "identity explicitly"
            )
        return explicit
    if explicit is not None and explicit != profiled:
        raise ValueError("explicit identity must match source messaging_identity")
    return profiled


def _resolve_binding(
    self: MessageChannel[SourceT, ReplayT],
    source: object,
) -> tuple[
    MessageCodec[SourceT, ReplayT],
    SseRenderer[ReplayT] | None,
    str | None,
    _CodecInputTransform | None,
]:
    """Resolve an explicit codec or one supported structural source profile."""

    if bool(getattr(source, "is_tinkerfin_sse_body", False)):
        raise TypeError("Messaging requires object events, not pre-encoded SSE frames")
    codec = self._codec
    inferred_profile = self._inferred_profile
    if codec is not None and inferred_profile is None:
        return codec, self._renderer, None, None
    profile = getattr(source, "messaging_codec_profile", None)
    if profile is None:
        if inferred_profile is not None:
            raise TypeError(
                "source does not advertise the channel's inferred codec profile"
            )
        raise TypeError(
            "source does not advertise a built-in codec; provide codec explicitly"
        )
    if not isinstance(profile, str):
        raise TypeError("source messaging_codec_profile must be a string")
    if inferred_profile is not None:
        if profile != inferred_profile:
            raise CodecMismatch(expected=inferred_profile, actual=profile)
        assert codec is not None
        codec_input = self._validate_profile_types(
            source=source,
            profile=profile,
            codec=codec,
        )
        return codec, self._renderer, profile, codec_input
    if profile == "agui.event":
        from .agui import AgUiCodec

        built_in = AgUiCodec()
    elif profile == "tinkerfin.native-stream":
        from .native import NativeStreamPartCodec

        built_in = NativeStreamPartCodec()
    else:
        raise TypeError(f"unsupported built-in codec profile: {profile!r}")
    codec_input = self._validate_profile_types(
        source=source,
        profile=profile,
        codec=built_in,
    )
    current = self._inferred_profile
    if current is not None and current != profile:
        raise CodecMismatch(expected=current, actual=profile)
    return (
        cast(MessageCodec[SourceT, ReplayT], built_in),
        cast(SseRenderer[ReplayT], built_in),
        profile,
        codec_input,
    )


def _validate_profile_types(
    *,
    source: object,
    profile: str,
    codec: object,
) -> _CodecInputTransform | None:
    """Prove live, optional codec-input, and replay types before preparation."""

    missing = object()
    declared_source = getattr(source, "messaging_source_type", missing)
    if declared_source is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="missing live source type metadata",
        )
    if not isinstance(declared_source, type):
        raise SourceProfileMismatch(
            profile=profile,
            reason="live source type metadata must be a type",
        )
    expected_source = getattr(codec, "messaging_source_type", missing)
    if expected_source is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="built-in codec is missing codec input type metadata",
        )

    raw_transform = getattr(source, "messaging_codec_input", missing)
    if raw_transform is missing:
        if declared_source is not expected_source:
            expected_name = getattr(
                expected_source,
                "__qualname__",
                type(expected_source).__name__,
            )
            raise SourceProfileMismatch(
                profile=profile,
                reason=f"live source type metadata must be {expected_name}",
            )
        codec_input: _CodecInputTransform | None = None
    else:
        if not callable(raw_transform):
            raise SourceProfileMismatch(
                profile=profile,
                reason="codec input transform must be callable",
            )
        declared_codec_input = getattr(
            source,
            "messaging_codec_input_type",
            missing,
        )
        if declared_codec_input is missing:
            raise SourceProfileMismatch(
                profile=profile,
                reason="missing codec input type metadata",
            )
        if declared_codec_input is not expected_source:
            expected_name = getattr(
                expected_source,
                "__qualname__",
                type(expected_source).__name__,
            )
            raise SourceProfileMismatch(
                profile=profile,
                reason=f"codec input type metadata must be {expected_name}",
            )
        codec_input = cast(_CodecInputTransform, raw_transform)

    declared_replay = getattr(source, "messaging_replay_type", missing)
    if declared_replay is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="missing replay type metadata",
        )
    expected_replay = getattr(codec, "messaging_replay_type", missing)
    if expected_replay is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="built-in codec is missing replay type metadata",
        )
    if declared_replay is not expected_replay:
        expected_name = getattr(
            expected_replay,
            "__qualname__",
            type(expected_replay).__name__,
        )
        raise SourceProfileMismatch(
            profile=profile,
            reason=f"replay type metadata must be {expected_name}",
        )
    return codec_input


def _commit_inferred_binding(
    self: MessageChannel[SourceT, ReplayT],
    *,
    codec: MessageCodec[SourceT, ReplayT],
    renderer: SseRenderer[ReplayT] | None,
    profile: str | None,
) -> None:
    if profile is None or self._codec is not None:
        return
    current = self._inferred_profile
    if current is not None and current != profile:
        raise CodecMismatch(expected=current, actual=profile)
    self._codec = codec
    self._codec_id = required_identifier("codec_id", codec.codec_id)
    self._renderer = renderer
    self._inferred_profile = profile


def _require_read_codec(
    self: MessageChannel[SourceT, ReplayT],
) -> MessageCodec[SourceT, ReplayT]:
    codec = self._codec
    if codec is None:
        raise TypeError(
            "channel codec is unknown; provide one explicitly or infer it from "
            "a profiled source first"
        )
    return codec


def _validate_page(*, after: int, limit: int | None = None) -> None:
    if isinstance(after, bool) or not isinstance(after, int):
        raise TypeError("after must be an integer")
    if after < 0:
        raise ValueError("after must be greater than or equal to zero")
    if limit is None:
        return
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


async def latest_seq(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> int:
    """Return the greatest committed sequence, or zero for an empty stream."""

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        self._messaging._require_open()
        latest = await _await_backend(
            "latest_seq",
            self._messaging._runtime_backend.latest_seq(
                channel=self.name,
                identity=identity,
            ),
        )
        self._messaging._require_open()
        return latest
    finally:
        self._messaging._finish_preflight(preflight)


async def get_run_status(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
) -> RunStatus:
    """Return one durable run's current authoritative status."""

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        self._messaging._require_open()
        status: object = await _await_backend(
            "get_run_status",
            self._messaging._runtime_backend.get_run_status(
                channel=self.name,
                identity=identity,
            ),
        )
        self._messaging._require_open()
        if not (is_active_run_status(status) or is_final_run_status(status)):
            raise MessagingBackendProtocolError(
                "Messaging backend returned an invalid run status",
                diagnostic_context={
                    "operation": "get_run_status",
                    "actual_type": type(status).__name__,
                },
            )
        return status
    finally:
        self._messaging._finish_preflight(preflight)


async def publish(
    self: MessageChannel[SourceT, ReplayT],
    message: SourceT,
    *,
    identity: RunIdentity,
    message_id: str | None = None,
) -> MessageEnvelope:
    """Commit an external value through the channel's bound codec and durable log."""
    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        codec = self._require_read_codec()
        if isinstance(codec, MessagePublicationPolicy):
            policy = cast(MessagePublicationPolicy[SourceT], codec)
            policy.validate_publication(message, identity=identity)
        payload = codec.encode(message)
        return await _await_backend(
            "publish",
            self._messaging._runtime_backend.publish(
                channel=self.name,
                identity=identity,
                message_id=uuid4().hex if message_id is None else message_id,
                codec=codec.codec_id,
                payload=payload,
            ),
        )
    finally:
        self._messaging._finish_preflight(preflight)


async def read(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
    after: int = 0,
    limit: int = 100,
) -> tuple[DecodedMessage[ReplayT], ...]:
    """Decode one ascending committed page after an exclusive cursor."""

    self._validate_page(after=after, limit=limit)
    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        codec = self._require_read_codec()
        expected_codec = required_identifier("codec_id", codec.codec_id)
        self._messaging._require_open()
        envelopes = await _await_backend(
            "read",
            self._messaging._runtime_backend.read(
                channel=self.name,
                identity=identity,
                after=after,
                limit=limit,
            ),
        )
        decoded: list[DecodedMessage[ReplayT]] = []
        for envelope in envelopes:
            if envelope.codec != expected_codec:
                raise CodecMismatch(
                    expected=expected_codec,
                    actual=envelope.codec,
                )
            decoded.append(
                DecodedMessage(
                    envelope=envelope,
                    data=codec.decode(envelope.payload),
                )
            )
        self._messaging._require_open()
        return tuple(decoded)
    finally:
        self._messaging._finish_preflight(preflight)


async def follow(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
    after: int = 0,
) -> MessageSubscription[ReplayT]:
    """Follow one run's committed events through its authoritative terminal."""

    from .messaging import MessageSubscription

    preflight = self._messaging._begin_preflight()
    try:
        self._validate_page(after=after)
        required_identity(identity)
        codec = self._require_read_codec()
        self._messaging._require_open()
        handle = await _await_backend(
            "bind_follow",
            self._messaging._runtime_backend.bind_follow(
                channel=self.name,
                identity=identity,
                after=after,
            ),
        )
        self._messaging._require_open()
        return MessageSubscription[ReplayT]._create(
            ledger=self._messaging._runtime_backend,
            prepared=PreparedRun(handle=handle, after=after, is_owner=False),
            codec=cast(MessageCodec[object, ReplayT], codec),
            renderer=self._renderer,
        )
    finally:
        self._messaging._finish_preflight(preflight)


async def validate_cursor(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
    after: int | None,
) -> None:
    """Validate one replay cursor without creating or attaching a run.

    This read-only preflight lets a host reject an out-of-range reconnect cursor
    before it opens an application source. `wrap()` still repeats cursor
    validation atomically with its start-or-attach decision.

    Args:
        identity: Run identity whose thread-level committed tail is checked.
        after: Exclusive replay cursor, or `None` to start at the current tail.

    Raises:
        InvalidCursor: `after` falls outside the current committed range.
        MessagingError: Messaging closes while the backend lookup is in flight.
        TypeError: `after` is neither an integer nor `None`.
        ValueError: `identity` contains a non-canonical identifier.
    """

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        if after is None:
            return
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer or None")
        latest = await _await_backend(
            "latest_seq",
            self._messaging._runtime_backend.latest_seq(
                channel=self.name,
                identity=identity,
            ),
        )
        self._messaging._require_open()
        if after < 0 or after > latest:
            raise InvalidCursor(after=after, latest=latest)
    finally:
        self._messaging._finish_preflight(preflight)


async def wrap(
    self: MessageChannel[SourceT, ReplayT],
    source: (
        MessageSource[SourceT] | ProfiledMessageSource[ProfileSourceT, ProfileReplayT]
    ),
    *,
    identity: RunIdentity | None = None,
    after: int | None = None,
    cancel: (CancelCallback[SourceT] | CancelCallback[ProfileSourceT] | None) = None,
    on_committed: CommittedCallback | None = None,
    on_source_ready: _DeliveryCallback | None = None,
    on_delivery_not_started: _DeliveryCallback | None = None,
) -> MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]:
    """Open a replayable subscription while preserving the source profile type."""

    return cast(
        "MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]",
        await self._wrap(
            cast(MessageSource[object], source),
            identity=identity,
            after=after,
            cancel=cast("CancelCallback[object] | None", cancel),
            on_committed=on_committed,
            on_source_ready=on_source_ready,
            on_delivery_not_started=on_delivery_not_started,
        ),
    )


async def _wrap(
    self: MessageChannel[SourceT, ReplayT],
    source: MessageSource[object],
    *,
    identity: RunIdentity | None = None,
    after: int | None = None,
    cancel: CancelCallback[object] | None = None,
    on_committed: CommittedCallback | None = None,
    on_source_ready: _DeliveryCallback | None = None,
    on_subscribed: _DeliveryCallback | None = None,
    on_delivery_not_started: _DeliveryCallback | None = None,
) -> MessageSubscription[object]:
    """Validate and start-or-attach before an HTTP response is constructed.

    Args:
        source: Single-use source owned and eventually closed by Messaging.
        identity: Explicit run identity for a custom source, or an optional
            equality check for a profiled TinkerFin source.
        after: Exclusive durable replay cursor, or the current tail when omitted.
        cancel: At-most-once synchronous or asynchronous callback. Omit it when the
            source declares `messaging_cancel_callback`. Supplying the same owner
            is accepted; a different callback is an ownership error. A callback
            may return `None` or a finite iterable of
            additional source values, which are encoded and appended after already
            accepted source values. It may accept no arguments or one required
            positional `CancelContext`, and is responsible for stopping the active
            source. A signature that also accepts no arguments is invoked without
            the context.
        on_committed: Asynchronous owner-only observer invoked with each durable
            envelope after append. Observer failures are logged and do not change
            the run outcome; replay attachments do not invoke it.
        on_subscribed: Async notification after binding this reader and before
            transferring it to the caller; failures detach only this reader.

    Returns:
        A detachable subscription over committed decoded messages.

    Raises:
        MessagingError: Backend preflight or producer startup is rejected.
        TypeError: `cancel` does not expose one of the supported signatures.
        ValueError: The explicit and source identities conflict.
    """

    try:
        preflight = self._messaging._begin_preflight()
    except BaseException as error:  # noqa: BLE001 - settle unclaimed delivery inputs
        await _settle_unregistered_delivery(
            error,
            source=source,
            on_delivery_not_started=on_delivery_not_started,
        )
    try:
        cancel_requested = asyncio.Event()
        task_started = asyncio.Event()

        async def operation() -> MessageSubscription[object]:
            return await _wrap_once(
                self,
                source,
                identity=identity,
                after=after,
                cancel=cancel,
                on_committed=on_committed,
                on_source_ready=on_source_ready,
                on_subscribed=on_subscribed,
                on_delivery_not_started=on_delivery_not_started,
                cancel_requested=cancel_requested,
                preflight=preflight,
            )

        task = asyncio.create_task(
            _retained_preflight(operation, started=task_started),
            name="tinkerfin-messaging-start-or-attach",
        )
        self._messaging._bind_preflight_owner(
            preflight,
            cast(asyncio.Task[object], task),
        )

        async def close_late(subscription: MessageSubscription[object]) -> None:
            await subscription.aclose()

        return await _await_retained_preflight(
            task,
            cancel_requested=cancel_requested,
            task_started=task_started,
            close_late_result=close_late,
        )
    finally:
        self._messaging._finish_preflight(preflight)


async def _wrap_once(
    self: MessageChannel[SourceT, ReplayT],
    source: MessageSource[object],
    *,
    identity: RunIdentity | None,
    after: int | None,
    cancel: CancelCallback[object] | None,
    on_committed: CommittedCallback | None,
    on_source_ready: _DeliveryCallback | None,
    on_subscribed: _DeliveryCallback | None,
    on_delivery_not_started: _DeliveryCallback | None,
    cancel_requested: asyncio.Event,
    preflight: _PreflightRegistration,
) -> MessageSubscription[object]:
    """Settle one complete ordinary start-or-attach decision."""

    from .messaging import MessageSubscription

    prepared: PreparedRun | None = None
    lease: _OwnerLease | None = None
    normalized_cancel: _ContextCancelCallback[object] | None = None
    producer_started = False
    delivery_started = False
    source_released = False
    producer_codec: MessageCodec[object, object] | None = None
    replay_renderer: SseRenderer[object] | None = None
    subscription: MessageSubscription[object] | None = None
    try:
        _validate_delivery_callback("on_source_ready", on_source_ready)
        _validate_delivery_callback(
            "on_delivery_not_started",
            on_delivery_not_started,
        )
        _validate_optional_cursor(after)
        codec, renderer, profile, codec_input = self._resolve_binding(source)
        producer_codec = cast(MessageCodec[object, object], codec)
        replay_renderer = cast(SseRenderer[object] | None, renderer)
        codec_id = required_identifier("codec_id", codec.codec_id)
        resolved_identity = self._resolve_identity(source, identity)
        source_cancel = getattr(source, "messaging_cancel_callback", None)
        if source_cancel is not None:
            if cancel is not None:
                matcher = getattr(
                    source,
                    "messaging_cancel_callback_matches",
                    None,
                )
                equivalent = cancel == source_cancel or (
                    callable(matcher) and bool(matcher(cancel))
                )
                if not equivalent:
                    raise TypeError(
                        "cancel must be omitted when the source owns cancellation"
                    )
            cancel = cast("CancelCallback[object]", source_cancel)
        if cancel is not None:
            normalized_cancel = _normalize_cancel_callback(cancel)
        prepared = await _await_backend(
            "prepare",
            self._messaging._runtime_backend.prepare(
                channel=self.name,
                identity=resolved_identity,
                codec=codec_id,
                after=after,
                cancellable=normalized_cancel is not None,
                recoverable=False,
            ),
        )
        if prepared.is_owner:
            lease = _OwnerLease(self._messaging, prepared)
            owner_task = asyncio.current_task()
            assert owner_task is not None

            def interrupt_preparation() -> None:
                owner_task.cancel()

            lease.protect(interrupt_preparation)
        if not prepared.is_owner:
            # A validated attachment is already a real delivery. Candidate-source or
            # response construction failures must never roll back host business state.
            delivery_started = True
        self._messaging._require_open()
        _raise_if_start_cancelled(cancel_requested)
        if prepared.is_owner:
            owner_preflight = getattr(source, "messaging_owner_preflight", None)
            if owner_preflight is not None:
                if not callable(owner_preflight):
                    raise TypeError("messaging_owner_preflight must be async callable")
                callback = cast(Callable[[], Awaitable[None]], owner_preflight)
                result = callback()
                if not inspect.isawaitable(result):
                    raise TypeError(
                        "messaging_owner_preflight must return an awaitable"
                    )
                await result
                self._messaging._require_open()
                _raise_if_start_cancelled(cancel_requested)
            # Once owner preflight succeeds, deferred sources have opened and managed
            # Runtime sources have committed their ready observations. Later failures
            # retain the host registration so it can reconcile against that Run.
            delivery_started = True
            await _invoke_delivery_callback(
                "on_source_ready", on_source_ready, preflight=preflight
            )
            self._messaging._require_open()
            _raise_if_start_cancelled(cancel_requested)
        self._commit_inferred_binding(
            codec=codec,
            renderer=renderer,
            profile=profile,
        )
        if prepared.is_owner:
            assert lease is not None
            lease.check()
            lease.protect(None)
            started = self._messaging._start_producer(
                prepared=prepared,
                lease=lease,
                source=source,
                codec=producer_codec,
                codec_input=codec_input,
                cancel=normalized_cancel,
                on_committed=on_committed,
            )
            producer_started = True
            delivery_started = True
            await started.wait()
            self._messaging._require_open()
        else:
            await source.aclose()
            source_released = True
            self._messaging._require_open()
        subscription = MessageSubscription[object]._create(
            ledger=self._messaging._runtime_backend,
            prepared=prepared,
            codec=producer_codec,
            renderer=replay_renderer,
        )
        # The framework owns this reader until its notification settles. Keeping
        # the existing preflight registered makes shutdown join this work, while
        # callback-triggered shutdown can still recognize its effective owner.
        await _invoke_delivery_callback(
            "on_subscribed", on_subscribed, preflight=preflight
        )
        self._messaging._require_open()
        _raise_if_start_cancelled(cancel_requested)
        return subscription
    # Preflight settlement must cover cancellation and process-control outcomes while
    # preserving the initiating failure after owned cleanup.
    except BaseException as error:  # noqa: BLE001 - deliver the combined failure after cleanup

        async def settle_failure(error: BaseException) -> NoReturn:
            if lease is not None and not producer_started:
                lease.protect(None)
            primary = (
                lease.error if lease is not None and lease.error is not None else error
            )
            if subscription is not None:
                try:
                    await subscription.aclose()
                except BaseException as close_error:  # noqa: BLE001 - cleanup continues
                    primary = _retain_settlement_failure(primary, close_error)
            if not producer_started and not source_released:
                try:
                    await source.aclose()
                except BaseException as close_error:  # noqa: BLE001 - cleanup continues
                    primary = _retain_settlement_failure(primary, close_error)
                if prepared is not None and prepared.is_owner:
                    try:
                        await _await_backend(
                            "finish",
                            self._messaging._runtime_backend.finish(
                                prepared.handle,
                                status="failed",
                                error=primary,
                            ),
                        )
                    except BackendOwnershipLost:
                        pass
                    except BaseException as finish_error:  # noqa: BLE001 - cleanup continues
                        primary = _retain_settlement_failure(primary, finish_error)
            if not delivery_started:
                try:
                    await _invoke_delivery_callback(
                        "on_delivery_not_started",
                        on_delivery_not_started,
                        preflight=preflight,
                    )
                except BaseException as callback_error:  # noqa: BLE001 - cleanup continues
                    primary = _retain_settlement_failure(primary, callback_error)
            if primary is not error:
                retain_failure(primary, error)
            raise primary

        return await _settle_delivery_step(settle_failure(error), preflight=preflight)

    finally:
        if lease is not None and not producer_started:
            await lease.aclose()


async def open_sse(
    self: MessageChannel[SourceT, ReplayT],
    source: MessageSource[object],
    *,
    identity: RunIdentity | None = None,
    after: int | Callable[[], int | None] | None = None,
    cancel: CancelCallback[object] | None = None,
    on_committed: CommittedCallback | None = None,
    on_source_ready: _DeliveryCallback | None = None,
    on_subscribed: _DeliveryCallback | None = None,
    on_delivery_not_started: _DeliveryCallback | None = None,
) -> AsyncGenerator[bytes, None]:
    """Prepare durable publication and return its SSE response body.

    Args:
        source: Single-use object source owned and eventually closed by Messaging.
        identity: Explicit run identity for a custom source, or an optional
            equality check for a profiled TinkerFin source.
        after: Exclusive replay cursor, a zero-argument synchronous resolver
            returning one, or `None` to start at the current tail. A resolver is
            invoked exactly once before durable preparation.
        cancel: Optional callback used for accepted remote cancellation. Omit it
            when the source declares its own callback.
        on_committed: Optional owner-only observer for newly committed envelopes.
        on_subscribed: Async notification for every successful owner or attachment
            subscription, settled before returning the body.

    Returns:
        A durable SSE body whose IDs are committed channel sequence numbers.
        The caller owns the body and must close it when it will not be consumed.

    Raises:
        MessagingError: Durable preparation or producer startup is rejected.
        TypeError: The cursor or delivery callback is invalid.
        ValueError: The explicit and source identities conflict.
    """

    try:
        _validate_delivery_callback("on_subscribed", on_subscribed)
        resolved_after = after() if callable(after) else after
    # Callback validation and cursor resolution precede delivery; every failure
    # still owns the unused candidate source and host not-started settlement.
    except BaseException as error:  # noqa: BLE001 - deliver the combined failure after cleanup
        await _settle_unregistered_delivery(
            error, source=source, on_delivery_not_started=on_delivery_not_started
        )

    subscription = await _wrap(
        self,
        source,
        identity=identity,
        after=resolved_after,
        cancel=cancel,
        on_committed=on_committed,
        on_source_ready=on_source_ready,
        on_subscribed=on_subscribed,
        on_delivery_not_started=on_delivery_not_started,
    )
    try:
        return subscription.to_sse()
    except BaseException as error:  # noqa: BLE001 - preserve rendering and cleanup failures

        async def settle_failure(error: BaseException) -> NoReturn:
            try:
                await subscription.aclose()
            except BaseException as close_error:  # noqa: BLE001 - preserve failure priority
                raise _retain_settlement_failure(error, close_error)
            raise error

        return await _settle_delivery_step(settle_failure(error))


async def wrap_recoverable(
    self: MessageChannel[SourceT, ReplayT],
    source: RecoverableSource[SourceT],
    *,
    identity: RunIdentity | None = None,
    after: int | None = None,
    cancel: CancelCallback[RecoverableMessage[SourceT]] | None = None,
    on_committed: CommittedCallback | None = None,
    on_source_ready: _DeliveryCallback | None = None,
    on_delivery_not_started: _DeliveryCallback | None = None,
) -> MessageSubscription[ReplayT]:
    """Start or rebuild an owner from its last committed checkpoint.

    Args:
        source: Factory that rebuilds one owned source from a checkpoint.
        identity: Explicit run identity for a custom source, or an optional
            equality check for a profiled source factory.
        after: Exclusive durable replay cursor, or the current tail when omitted.
        cancel: At-most-once synchronous or asynchronous callback. Returned tail
            values must be `RecoverableMessage` instances with stable IDs and
            matching checkpoints. The callback is responsible for causing the
            active source to stop. It may accept no arguments or one required
            positional `CancelContext`; a signature that also accepts no
            arguments is invoked without the context.
        on_committed: Asynchronous owner-only observer invoked after every
            successful append, including idempotent recovery commits.

    Returns:
        A detachable subscription over committed decoded messages.

    Raises:
        MessagingError: Backend preflight, recovery, or startup is rejected.
        TypeError: `cancel` does not expose one of the supported signatures.
        ValueError: The explicit and source identities conflict.
    """

    try:
        preflight = self._messaging._begin_preflight()
    except BaseException as error:  # noqa: BLE001 - recoverable source was not opened
        await _settle_unregistered_delivery(
            error,
            source=None,
            on_delivery_not_started=on_delivery_not_started,
        )
    try:
        cancel_requested = asyncio.Event()
        task_started = asyncio.Event()

        async def operation() -> MessageSubscription[ReplayT]:
            return await _wrap_recoverable_once(
                self,
                source,
                identity=identity,
                after=after,
                cancel=cancel,
                on_committed=on_committed,
                on_source_ready=on_source_ready,
                on_delivery_not_started=on_delivery_not_started,
                cancel_requested=cancel_requested,
                preflight=preflight,
            )

        task = asyncio.create_task(
            _retained_preflight(operation, started=task_started),
            name="tinkerfin-messaging-recoverable-start-or-attach",
        )
        self._messaging._bind_preflight_owner(
            preflight,
            cast(asyncio.Task[object], task),
        )

        async def close_late(subscription: MessageSubscription[ReplayT]) -> None:
            await subscription.aclose()

        return await _await_retained_preflight(
            task,
            cancel_requested=cancel_requested,
            task_started=task_started,
            close_late_result=close_late,
        )
    finally:
        self._messaging._finish_preflight(preflight)


async def _wrap_recoverable_once(
    self: MessageChannel[SourceT, ReplayT],
    source: RecoverableSource[SourceT],
    *,
    identity: RunIdentity | None,
    after: int | None,
    cancel: CancelCallback[RecoverableMessage[SourceT]] | None,
    on_committed: CommittedCallback | None,
    on_source_ready: _DeliveryCallback | None,
    on_delivery_not_started: _DeliveryCallback | None,
    cancel_requested: asyncio.Event,
    preflight: _PreflightRegistration,
) -> MessageSubscription[ReplayT]:
    """Settle one complete recoverable start-or-attach decision."""

    from .messaging import MessageSubscription

    prepared: PreparedRun | None = None
    lease: _OwnerLease | None = None
    opened: MessageSource[RecoverableMessage[SourceT]] | None = None
    normalized_cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None = None
    producer_started = False
    delivery_started = False
    codec: MessageCodec[SourceT, ReplayT] | None = None
    renderer: SseRenderer[ReplayT] | None = None
    try:
        _validate_delivery_callback("on_source_ready", on_source_ready)
        _validate_delivery_callback(
            "on_delivery_not_started",
            on_delivery_not_started,
        )
        _validate_optional_cursor(after)
        codec, renderer, profile, codec_input = self._resolve_binding(source)
        if codec_input is not None:
            raise SourceProfileMismatch(
                profile=profile or codec.codec_id,
                reason="recoverable source profiles must yield codec input directly",
            )
        codec_id = required_identifier("codec_id", codec.codec_id)
        resolved_identity = self._resolve_identity(source, identity)
        if cancel is not None:
            normalized_cancel = _normalize_cancel_callback(cancel)
        prepared = await _await_backend(
            "prepare",
            self._messaging._runtime_backend.prepare(
                channel=self.name,
                identity=resolved_identity,
                codec=codec_id,
                after=after,
                cancellable=normalized_cancel is not None,
                recoverable=True,
            ),
        )
        if prepared.is_owner:
            lease = _OwnerLease(self._messaging, prepared)
            owner_task = asyncio.current_task()
            assert owner_task is not None

            def interrupt_preparation() -> None:
                owner_task.cancel()

            lease.protect(interrupt_preparation)
        if not prepared.is_owner:
            delivery_started = True
        self._messaging._require_open()
        _raise_if_start_cancelled(cancel_requested)
        if prepared.is_owner:
            owner_preflight = getattr(source, "messaging_owner_preflight", None)
            if owner_preflight is not None:
                if not callable(owner_preflight):
                    raise TypeError("messaging_owner_preflight must be async callable")
                result = owner_preflight()
                if not inspect.isawaitable(result):
                    raise TypeError(
                        "messaging_owner_preflight must return an awaitable"
                    )
                await result
                self._messaging._require_open()
                _raise_if_start_cancelled(cancel_requested)
            assert lease is not None
            opened = await _open_recoverable_source(
                self._messaging, source, prepared, lease
            )
            self._messaging._require_open()
            _raise_if_start_cancelled(cancel_requested)
            delivery_started = True
            await _invoke_delivery_callback(
                "on_source_ready", on_source_ready, preflight=preflight
            )
            self._messaging._require_open()
            _raise_if_start_cancelled(cancel_requested)
        self._commit_inferred_binding(
            codec=codec,
            renderer=renderer,
            profile=profile,
        )
        if prepared.is_owner:
            assert opened is not None
            assert lease is not None
            lease.check()
            lease.protect(None)
            started = self._messaging._start_recoverable_producer(
                prepared=prepared,
                lease=lease,
                source=opened,
                codec=codec,
                cancel=normalized_cancel,
                on_committed=on_committed,
            )
            producer_started = True
            delivery_started = True
            await started.wait()
            self._messaging._require_open()
        return MessageSubscription[ReplayT]._create(
            ledger=self._messaging._runtime_backend,
            prepared=prepared,
            codec=cast(MessageCodec[object, ReplayT], codec),
            renderer=renderer,
        )
    # Recoverable preparation owns opened sources and backend settlement for every
    # failure category, including cancellation and process control.
    except BaseException as error:  # noqa: BLE001 - deliver the combined failure after cleanup

        async def settle_failure(error: BaseException) -> NoReturn:
            if lease is not None and not producer_started:
                lease.protect(None)
            primary = (
                lease.error if lease is not None and lease.error is not None else error
            )
            if prepared is not None and prepared.is_owner and not producer_started:
                if opened is not None:
                    try:
                        await opened.aclose()
                    except BaseException as close_error:  # noqa: BLE001 - cleanup continues
                        primary = _retain_settlement_failure(primary, close_error)
                try:
                    await _await_backend(
                        "finish",
                        self._messaging._runtime_backend.finish(
                            prepared.handle,
                            status="failed",
                            error=primary,
                        ),
                    )
                except BackendOwnershipLost:
                    pass
                except BaseException as finish_error:  # noqa: BLE001 - cleanup continues
                    primary = _retain_settlement_failure(primary, finish_error)
            if not delivery_started:
                try:
                    await _invoke_delivery_callback(
                        "on_delivery_not_started",
                        on_delivery_not_started,
                        preflight=preflight,
                    )
                except BaseException as callback_error:  # noqa: BLE001 - cleanup continues
                    primary = _retain_settlement_failure(primary, callback_error)
            if primary is not error:
                retain_failure(primary, error)
            raise primary

        return await _settle_delivery_step(settle_failure(error), preflight=preflight)

    finally:
        if lease is not None and not producer_started:
            await lease.aclose()


async def cancel(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> bool:
    """Request cancellation and wait until the durable run status is final.

    Args:
        identity: Thread and run identity whose callback should be invoked.

    Returns:
        `True` only when this call initiated cancellation and the run settled as
        cancelled. Concurrent or repeated requests return `False` after sharing
        the same final status.

    Raises:
        CancellationUnsupported: The run has no cancellation callback.
        RunNotFound: The target run does not exist.
        RunProducerFailed: The callback, encoding, append, or producer failed.
    """

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        handle = BackendRunHandle(
            channel=self.name,
            identity=identity,
            owner_token=None,
            fence=None,
        )
        initiated = await _await_backend(
            "request_cancel",
            self._messaging._runtime_backend.request_cancel(handle),
        )
        status = await _await_backend(
            "wait_finished",
            self._messaging._runtime_backend.wait_finished(handle),
        )
        if is_failed_run_status(status):
            cause = await _await_backend(
                "failure",
                self._messaging._runtime_backend.failure(handle),
            )
            raise RunProducerFailed(
                identity=identity,
                cause=cause
                or RuntimeError(f"Producer for run {identity.run_id!r} stopped"),
            )
        return initiated and status == "cancelled"
    finally:
        self._messaging._finish_preflight(preflight)


async def delete_stream(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> None:
    """Delete one inactive durable stream without changing the channel codec.

    Missing and previously deleted streams are successful no-ops. Deletion never
    requests producer cancellation; callers must settle an active run first.

    Args:
        identity: RunIdentity whose thread-level durable stream is deleted.

    Raises:
        MessagingError: Messaging closes or the backend rejects deletion.
        StreamDeleteConflict: The stream still has an active producer.
        ValueError: `identity` contains a non-canonical identifier.
    """

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        await _await_backend(
            "delete_stream",
            self._messaging._runtime_backend.delete_stream(
                channel=self.name,
                identity=identity,
            ),
        )
        self._messaging._require_open()
    finally:
        self._messaging._finish_preflight(preflight)
