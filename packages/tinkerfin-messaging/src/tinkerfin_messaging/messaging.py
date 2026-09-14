"""Protocol-neutral producer, replay subscription, and SSE facade."""

from __future__ import annotations

import asyncio
import math
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import dataclass
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Generic,
    Never,
    Self,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

from tinkerfin_contracts import RunIdentity

from . import _message_channel, _messaging_boundary, _producer_runtime
from ._identity import required_identifier, required_identity
from ._messaging_boundary import (
    _invoke_cancel as _invoke_cancel,
)
from ._messaging_boundary import (
    _iterate_backend,
    _join_owned_task,
    _read_backend,
)
from ._messaging_boundary import (
    _normalize_cancel_callback as _normalize_cancel_callback,
)
from ._messaging_ledger import PreparedRun as _PreparedRun
from ._messaging_ledger import _MessagingLedger
from ._producer_runtime import _OwnerLease, _ProducedMessage
from ._tasks import TaskOutcome, capture, join_owned_task, select_failure
from .backend import MemoryBackend, RunStatus
from .backend_contract import MessagingBackend
from .errors import (
    CodecMismatch,
    MessagingClosed,
    MessagingNotStarted,
    MessagingSettlementTimeout,
)
from .models import (
    DecodedMessage,
    MessageEnvelope,
    RecoverableMessage,
)
from .protocols import (
    MessageCodec,
    MessageSource,
    ProfiledMessageSource,
    RecoverableSource,
    SseRenderer,
)

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProducedT = TypeVar("ProducedT")
ProfileSourceT = TypeVar("ProfileSourceT")
ProfileReplayT = TypeVar("ProfileReplayT")
BackendResultT = TypeVar("BackendResultT")


@dataclass(eq=False, slots=True)
class _PreflightRegistration:
    """Track only one preflight lifecycle, never its caller's later work."""

    owner: asyncio.Task[object]
    settled: asyncio.Future[None]
    owner_close_requested: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class CancelContext:
    """Identify the run whose accepted cancellation invokes a callback."""

    channel: str
    identity: RunIdentity

    def __post_init__(self) -> None:
        """Validate the durable channel and run identity before callback use."""

        required_identifier("channel", self.channel)
        required_identity(self.identity)


_CancelResult: TypeAlias = (
    Iterable[ProducedT] | Awaitable[Iterable[ProducedT] | None] | None
)
_ContextCancelCallback: TypeAlias = Callable[
    [CancelContext],
    _CancelResult[ProducedT],
]
CancelCallback: TypeAlias = (
    Callable[[], _CancelResult[ProducedT]] | _ContextCancelCallback[ProducedT]
)
if TYPE_CHECKING:
    from ._agui_channel import AgUiChannel


CommittedCallback: TypeAlias = Callable[[MessageEnvelope], Awaitable[None]]


class MessageSubscription(Generic[ReplayT]):
    """Decode one channel-created run replay and close it exactly once.

    Instances are returned by ``MessageChannel.wrap()`` and
    ``MessageChannel.follow()``. Direct construction is unsupported because the channel
    must first bind the codec, exact generation, cursor, and parent Messaging lifecycle.
    A subscription is single-use; its consumer owns iteration and may call ``aclose()``
    to detach without cancelling the producer. Only one pull may be active at a time.
    Closing cancels and settles a pending pull before releasing its backend iterator;
    the waiting consumer observes ``CancelledError``. Repeated close calls share the
    same completed cleanup.
    """

    _ledger: _MessagingLedger
    _prepared: _PreparedRun
    _codec: MessageCodec[object, ReplayT]
    _renderer: SseRenderer[ReplayT] | None
    _claimed: bool
    _delivery: AsyncIterator[DecodedMessage[ReplayT]] | None
    _backend_iterator: AsyncIterator[object] | None
    _backend_close_task: asyncio.Task[None] | None
    _close_task: asyncio.Task[None] | None

    def __init__(self) -> None:
        """Reject construction that bypasses MessageChannel binding.

        Raises:
            TypeError: Always; obtain a subscription from ``wrap()`` or ``follow()``.
        """

        raise TypeError(
            "MessageSubscription is created by MessageChannel.wrap() or follow()"
        )

    @classmethod
    def _create(
        cls,
        *,
        ledger: _MessagingLedger,
        prepared: _PreparedRun,
        codec: MessageCodec[object, ReplayT],
        renderer: SseRenderer[ReplayT] | None,
    ) -> Self:
        """Create a lazy decoder after the channel has completed durable binding."""

        subscription = cls.__new__(cls)
        subscription._ledger = ledger
        subscription._prepared = prepared
        subscription._codec = codec
        subscription._renderer = renderer
        subscription._claimed = False
        subscription._delivery = None
        subscription._backend_iterator = None
        subscription._backend_close_task = None
        subscription._close_task = None
        return subscription

    def __aiter__(self) -> AsyncIterator[DecodedMessage[ReplayT]]:
        """Claim and return the subscription's one decoded iterator."""

        return _messaging_boundary.__aiter__(
            self,
        )

    async def _iterate(self) -> AsyncGenerator[DecodedMessage[ReplayT], None]:
        backend_iterator = _read_backend(
            "follow",
            lambda: self._ledger.follow(
                self._prepared.handle,
                after=self._prepared.after,
            ),
        )
        self._backend_iterator = cast(AsyncIterator[object], backend_iterator)
        primary: BaseException | None = None
        try:
            async for envelope in _iterate_backend("follow", backend_iterator):
                expected_codec = self._codec.codec_id
                if envelope.codec != expected_codec:
                    raise CodecMismatch(
                        expected=expected_codec,
                        actual=envelope.codec,
                    )
                yield DecodedMessage(
                    envelope=envelope,
                    data=self._codec.decode(envelope.payload),
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await _messaging_boundary._close_backend_iterator(
                    self,
                    cast(AsyncIterator[object], backend_iterator),
                )
            except BaseException as close_error:
                if primary is None:
                    raise
                primary.add_note(f"Messaging follow cleanup also failed: {close_error}")

    def to_sse(self) -> AsyncGenerator[bytes, None]:
        """Return committed messages as single-use UTF-8 SSE frames."""

        return _messaging_boundary.to_sse(
            self,
        )

    async def aclose(self) -> None:
        """Detach this subscriber without affecting the producer."""

        return await _messaging_boundary.aclose(
            self,
        )


class MessageChannel(Generic[SourceT, ReplayT]):
    """Bind one stable codec to shared framework run identities."""

    def __init__(
        self,
        *,
        messaging: Messaging,
        name: str,
        codec: MessageCodec[SourceT, ReplayT] | None,
        renderer: SseRenderer[ReplayT] | None,
    ) -> None:
        """Initialize a named facade that borrows its parent Messaging lifecycle."""

        self._messaging = messaging
        self.name = required_identifier("channel name", name)
        if codec is None and renderer is not None:
            raise TypeError("renderer requires an explicit codec")
        codec_id = (
            None
            if codec is None
            else required_identifier(
                "codec_id",
                codec.codec_id,
            )
        )
        self._codec_id = codec_id
        self._codec = codec
        self._renderer = renderer
        self._inferred_profile: str | None = None

    @staticmethod
    def _resolve_identity(source: object, identity: RunIdentity | None) -> RunIdentity:
        """Resolve one explicit or immutable source identity before side effects."""

        return _message_channel._resolve_identity(
            source,
            identity,
        )

    def _resolve_binding(
        self,
        source: object,
    ) -> tuple[
        MessageCodec[SourceT, ReplayT],
        SseRenderer[ReplayT] | None,
        str | None,
        Callable[[object], object] | None,
    ]:
        """Resolve an explicit codec or one supported structural source profile."""

        return _message_channel._resolve_binding(
            self,
            source,
        )

    @staticmethod
    def _validate_profile_types(
        *,
        source: object,
        profile: str,
        codec: object,
    ) -> Callable[[object], object] | None:
        """Prove declared live, codec-input, and replay types before preparation."""

        return _message_channel._validate_profile_types(
            source=source,
            profile=profile,
            codec=codec,
        )

    def _commit_inferred_binding(
        self,
        *,
        codec: MessageCodec[SourceT, ReplayT],
        renderer: SseRenderer[ReplayT] | None,
        profile: str | None,
    ) -> None:
        return _message_channel._commit_inferred_binding(
            self,
            codec=codec,
            renderer=renderer,
            profile=profile,
        )

    def _require_read_codec(self) -> MessageCodec[SourceT, ReplayT]:
        return _message_channel._require_read_codec(
            self,
        )

    @staticmethod
    def _validate_page(*, after: int, limit: int | None = None) -> None:
        return _message_channel._validate_page(
            after=after,
            limit=limit,
        )

    async def publish(
        self,
        message: SourceT,
        *,
        identity: RunIdentity,
        message_id: str | None = None,
    ) -> MessageEnvelope:
        """Publish one message to an existing run and its current subscribers.

        The run must have committed its first source message and remain active.
        Publication does not open a source, hold its lease, or change its checkpoint.
        AG-UI channels accept only CUSTOM events and close publication at the main
        terminal. The host remains responsible for authorizing the target identity.

        Args:
            message: A value accepted by the channel codec and publication policy.
            identity: Existing thread and run to notify.
            message_id: Optional idempotency key; omitted keys are generated per call.

        Returns:
            The persisted envelope. Same-key, same-content retries return the original
            envelope while it is retained, including after the run has finished.

        Raises:
            PublicationRejected: The run or protocol rejects a new publication.
            RunNotFound: The target run does not exist.
            MessageIdConflict: The key already identifies different content.
            MessagingError: Storage, capacity, codec binding, or facade lifecycle fails.
        """
        return await _message_channel.publish(
            self, message, identity=identity, message_id=message_id
        )

    async def latest_seq(self, *, identity: RunIdentity) -> int:
        """Return the greatest committed sequence, or zero for an empty stream."""

        return await _message_channel.latest_seq(
            self,
            identity=identity,
        )

    async def get_run_status(self, *, identity: RunIdentity) -> RunStatus:
        """Return one durable run's current authoritative status.

        The lookup has no producer ownership. A leased backend may atomically
        classify an expired producer as ``owner_lost`` before returning.

        Args:
            identity: Exact thread and run identity to inspect.

        Returns:
            The durable running, cancellation, success, or failure status.

        Raises:
            RunNotFound: No durable record exists for the requested run.
            MessagingError: Messaging is closed or the backend lookup fails.
            ValueError: The identity is not canonical.
        """

        return await _message_channel.get_run_status(
            self,
            identity=identity,
        )

    async def read(
        self,
        *,
        identity: RunIdentity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[DecodedMessage[ReplayT], ...]:
        """Decode one ascending committed page after an exclusive cursor."""

        return await _message_channel.read(
            self,
            identity=identity,
            after=after,
            limit=limit,
        )

    async def follow(
        self,
        *,
        identity: RunIdentity,
        after: int = 0,
    ) -> MessageSubscription[ReplayT]:
        """Follow one run's committed events through its authoritative terminal."""

        return await _message_channel.follow(
            self,
            identity=identity,
            after=after,
        )

    async def validate_cursor(
        self,
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

        return await _message_channel.validate_cursor(
            self,
            identity=identity,
            after=after,
        )

    @overload
    async def wrap(
        self,
        source: MessageSource[SourceT],
        *,
        identity: RunIdentity,
        after: int | None = None,
        cancel: CancelCallback[SourceT] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> MessageSubscription[ReplayT]: ...

    @overload
    async def wrap(
        self,
        source: ProfiledMessageSource[ProfileSourceT, ProfileReplayT],
        *,
        identity: RunIdentity | None = None,
        after: int | None = None,
        cancel: CancelCallback[ProfileSourceT] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> MessageSubscription[ProfileReplayT]: ...

    async def wrap(
        self,
        source: (
            MessageSource[SourceT]
            | ProfiledMessageSource[ProfileSourceT, ProfileReplayT]
        ),
        *,
        identity: RunIdentity | None = None,
        after: int | None = None,
        cancel: (
            CancelCallback[SourceT] | CancelCallback[ProfileSourceT] | None
        ) = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]:
        """Start or attach one source and return its run-bounded subscription.

        Once a delivery callback starts, caller cancellation waits for it to finish.
        Callbacks must bound database and network operations with resource timeouts.

        The producing request transfers its single-use source to Messaging, which
        closes it after terminal settlement. An attachment never opens its unused
        candidate source; Messaging closes that candidate once before returning the
        attached subscription. Replay remains bounded by committed backend sequence
        and preserves downstream backpressure.

        Args:
            source: Custom source with explicit identity, or profiled TinkerFin source.
            identity: Required custom-source identity or optional equality check for a
                profiled source.
            after: Exclusive replay cursor, or current committed tail when omitted.
            cancel: Optional at-most-once cancellation owner; omit when the source
                already declares its own matching callback.
            on_committed: Owner-only async observer invoked after each durable append;
                attachment never invokes it and observer failure does not change the
                producer outcome.
            on_source_ready: Owner-only async callback after the source is ready and
                before the producer task is created.
            on_delivery_not_started: Async cleanup callback used only when source readiness and a
                valid attachment were both absent.

        Returns:
            Detachable subscription over committed, decoded messages.

        Raises:
            MessagingError: Backend preflight, ownership, cursor, or startup fails.
            TypeError: Source, codec profile, or cancellation signature is invalid.
            ValueError: Explicit and source identities conflict.
        """

        return await _message_channel.wrap(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
            on_source_ready=on_source_ready,
            on_delivery_not_started=on_delivery_not_started,
        )

    async def _wrap(
        self,
        source: MessageSource[object],
        *,
        identity: RunIdentity | None = None,
        after: int | None = None,
        cancel: CancelCallback[object] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
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

        Returns:
            A detachable subscription over committed decoded messages.

        Raises:
            MessagingError: Backend preflight or producer startup is rejected.
            TypeError: `cancel` does not expose one of the supported signatures.
            ValueError: The explicit and source identities conflict.
        """

        return await _message_channel._wrap(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
            on_source_ready=on_source_ready,
            on_delivery_not_started=on_delivery_not_started,
        )

    @overload
    async def open_sse(
        self,
        source: MessageSource[SourceT],
        *,
        identity: RunIdentity,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[SourceT] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[bytes, None]: ...

    @overload
    async def open_sse(
        self,
        source: ProfiledMessageSource[ProfileSourceT, ProfileReplayT],
        *,
        identity: RunIdentity | None = None,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[ProfileSourceT] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[bytes, None]: ...

    async def open_sse(
        self,
        source: MessageSource[object],
        *,
        identity: RunIdentity | None = None,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[object] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Start or attach a run and return its committed messages as UTF-8 SSE bytes.

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
            on_source_ready: Owner-only async callback after source readiness and before
                producer creation.
            on_delivery_not_started: Async cleanup callback when no delivery starts.

        Returns:
            A durable SSE body whose IDs are committed channel sequence numbers.
            The caller owns the body and must close it when it will not be consumed.

        Raises:
            MessagingError: Durable preparation or producer startup is rejected.
            TypeError: The resolved cursor is neither an integer nor `None`.
            ValueError: The explicit and source identities conflict.
        """

        return await _message_channel.open_sse(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
            on_source_ready=on_source_ready,
            on_delivery_not_started=on_delivery_not_started,
        )

    async def wrap_recoverable(
        self,
        source: RecoverableSource[SourceT],
        *,
        identity: RunIdentity | None = None,
        after: int | None = None,
        cancel: CancelCallback[RecoverableMessage[SourceT]] | None = None,
        on_committed: CommittedCallback | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
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
            on_source_ready: Owner-only async callback after source recovery and before
                producer creation.
            on_delivery_not_started: Async cleanup callback when no producer or valid
                attachment was established.

        Returns:
            A detachable subscription over committed decoded messages.

        Raises:
            MessagingError: Backend preflight, recovery, or startup is rejected.
            TypeError: `cancel` does not expose one of the supported signatures.
            ValueError: The explicit and source identities conflict.
        """

        return await _message_channel.wrap_recoverable(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
            on_source_ready=on_source_ready,
            on_delivery_not_started=on_delivery_not_started,
        )

    async def cancel(self, *, identity: RunIdentity) -> bool:
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

        return await _message_channel.cancel(
            self,
            identity=identity,
        )

    async def delete_stream(self, *, identity: RunIdentity) -> None:
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

        return await _message_channel.delete_stream(
            self,
            identity=identity,
        )


class Messaging:
    """Manage producer tasks while one construction-time backend owns durable state."""

    def __init__(
        self,
        *,
        backend: MessagingBackend | None = None,
        settlement_timeout: float | None = None,
    ) -> None:
        """Configure one single-use asynchronous Messaging lifecycle.

        Args:
            backend: Durable backend owned according to its own resource contract.
            settlement_timeout: Optional per-caller close wait in seconds. ``None``
                waits until all owned producers settle. A finite timeout stops only
                the caller's wait and never cancels the shared close task.

        Raises:
            TypeError: The backend contract or ``settlement_timeout`` is invalid.
            ValueError: ``settlement_timeout`` is negative or non-finite.
        """

        if settlement_timeout is None:
            resolved_timeout = None
        else:
            if isinstance(settlement_timeout, bool) or not isinstance(
                settlement_timeout,
                int | float,
            ):
                raise TypeError("settlement_timeout must be a number or None")
            resolved_timeout = float(settlement_timeout)
            if not math.isfinite(resolved_timeout) or resolved_timeout < 0:
                raise ValueError("settlement_timeout must be finite and non-negative")
        if backend is not None and not isinstance(backend, MessagingBackend):
            raise TypeError("backend must implement MessagingBackend")
        self._backend: MessagingBackend = (
            MemoryBackend() if backend is None else backend
        )
        self._ledger = _MessagingLedger(self._backend)
        self._settlement_timeout = resolved_timeout
        self._state = "new"
        self._storage_setup_task: asyncio.Task[None] | None = None
        self._preflight_tasks: set[_PreflightRegistration] = set()
        self._producer_tasks: set[asyncio.Task[None]] = set()
        self._settling_producers: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[TaskOutcome[None]] | None = None
        self._producers_stopped: asyncio.Future[TaskOutcome[None]] | None = None

    @property
    def backend(self) -> MessagingBackend:
        """Return the borrowed storage backend fixed at construction time."""

        return self._backend

    @property
    def _runtime_backend(self) -> _MessagingLedger:
        """Return the private framework lifecycle coordinator."""

        return self._ledger

    async def __aenter__(self) -> Self:
        """Prepare storage and open this single-use Messaging lifecycle.

        Storage preparation is retained by the facade when this caller is cancelled.
        Another enter cannot overlap that preparation, and closing waits for the owned
        preparation task before the facade becomes permanently closed.

        Returns:
            This open Messaging facade after storage preparation succeeds.

        Raises:
            MessagingClosed: The facade is opening, open, closing, or already closed.
            BaseException: Storage preparation fails or this caller is cancelled.
        """

        if self._state == "closed":
            raise MessagingClosed("Messaging is closed")
        if self._state != "new":
            raise MessagingClosed("Messaging is already open")
        # Publishing ``opening`` before the first await makes the single-use check
        # authoritative for concurrent enter and close callers.
        self._state = "opening"
        setup_task = self._storage_setup_task
        if (
            setup_task is not None
            and setup_task.done()
            and (setup_task.cancelled() or setup_task.exception() is not None)
        ):
            self._storage_setup_task = None
            setup_task = None
        if setup_task is None:
            setup_task = asyncio.create_task(
                self._ledger.prepare_storage(),
                name="tinkerfin-messaging-prepare-storage",
            )
            setup_task.add_done_callback(self._storage_setup_finished)
            self._storage_setup_task = setup_task
        try:
            await asyncio.shield(setup_task)
        except BaseException:
            # Caller cancellation does not cancel the retained setup task. A later
            # enter may join it, unless close has already claimed the lifecycle.
            if self._state == "opening":
                self._state = "new"
            raise
        if self._state != "opening":
            raise MessagingClosed("Messaging closed during storage preparation")
        self._state = "open"
        return self

    @staticmethod
    def _storage_setup_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained storage preparation failure between callers."""

        if not task.cancelled():
            task.exception()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close Messaging while preserving the active scope's primary failure."""

        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            current = asyncio.current_task()
            if isinstance(cleanup_error, asyncio.CancelledError) and (
                current is not None and current.cancelling()
            ):
                cleanup_error.add_note(
                    f"Messaging context body also failed: {type(exc).__name__}: {exc}"
                )
                raise
            exc.add_note(
                "Messaging context cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def aclose(self) -> None:
        """Stop producers and finish accepted delivery cleanup.

        Calls from a preparation callback wait for producers to stop; the callback
        then returns to finish its own delivery cleanup. Other callers wait for both.
        A finite settlement timeout limits only this caller's wait.

        Raises:
            MessagingSettlementTimeout: The configured caller wait expires.
            BaseException: Owned work fails during shutdown.
        """

        caller = asyncio.current_task()
        owned = tuple(item for item in self._preflight_tasks if item.owner is caller)
        for registration in owned:
            if not registration.owner_close_requested.done():
                registration.owner_close_requested.set_result(None)
        task = self._close_task
        if task is None:
            self._state = "closing"
            self._producers_stopped = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(
                capture(self._close_once()), name="tinkerfin-messaging-close"
            )
            self._close_task = task
        stopped = self._producers_stopped
        assert stopped is not None
        completion = stopped if owned else task
        timeout = self._settlement_timeout
        if timeout is None or completion.done():
            await join_owned_task(completion)
            return
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await asyncio.shield(completion)
        except TimeoutError as error:
            if deadline.expired():
                raise MessagingSettlementTimeout(timeout=timeout) from error
            raise
        await join_owned_task(completion)

    async def _close_once(self) -> None:
        """Let preparation owners finish after production stops, then join them."""

        outcome = await capture(self._stop_producers())
        stopped = self._producers_stopped
        assert stopped is not None
        # This signal is not the facade's completion: a callback waiting for close
        # must resume before its real registration.settled can become ready.
        stopped.set_result(outcome)
        try:
            pending = tuple(item.settled for item in self._preflight_tasks)
            if pending:
                await asyncio.gather(*pending)
        finally:
            self._state = "closed"
        if isinstance(outcome, BaseException):
            raise outcome

    async def _stop_producers(self) -> None:
        primary: BaseException | None = None
        setup_task = self._storage_setup_task
        if setup_task is not None:
            try:
                await _join_owned_task(setup_task)
            except BaseException as setup_error:  # noqa: BLE001 - settle remaining work
                primary = setup_error
        for producer in tuple(self._producer_tasks):
            if not producer.done() and producer not in self._settling_producers:
                producer.cancel()
        for registration in tuple(self._preflight_tasks):
            await asyncio.wait(
                (registration.settled, registration.owner_close_requested),
                return_when=asyncio.FIRST_COMPLETED,
            )
        # A preflight that finished without requesting close may have started a
        # producer. No owner waiting on producers_stopped can start one while closing.
        producers = tuple(self._producer_tasks)
        for producer in producers:
            if not producer.done() and producer not in self._settling_producers:
                producer.cancel()
        if producers:
            results = await asyncio.gather(*producers, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    primary = (
                        result if primary is None else select_failure(primary, result)
                    )
        if primary is not None:
            raise primary

    def _begin_preflight(self) -> _PreflightRegistration:
        """Register one explicit lifecycle before shutdown can take its snapshot."""

        self._require_open()
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Messaging preflight requires an asyncio task")
        registration = _PreflightRegistration(
            owner=cast(asyncio.Task[object], current),
            settled=asyncio.get_running_loop().create_future(),
            owner_close_requested=asyncio.get_running_loop().create_future(),
        )
        self._preflight_tasks.add(registration)
        return registration

    def _bind_preflight_owner(
        self,
        registration: _PreflightRegistration,
        owner: asyncio.Task[object],
    ) -> None:
        """Bind retained startup to the child that owns its actual settlement."""

        if registration in self._preflight_tasks:
            registration.owner = owner

    def _finish_preflight(self, registration: _PreflightRegistration) -> None:
        if not registration.settled.done():
            registration.settled.set_result(None)
        self._preflight_tasks.discard(registration)

    def agui_channel(self, *, name: str) -> AgUiChannel:
        """Create a durable AG-UI channel with automatic protocol handling.

        Args:
            name: Canonical channel name shared by publishing and reading workers.

        Returns:
            Typed AG-UI delivery channel borrowing this Messaging lifecycle.

        Raises:
            MessagingClosed: The parent lifecycle is not open.
            ImportError: The AG-UI optional dependency is not installed.
            ValueError: The channel name is invalid.
        """
        self._require_open()
        from . import AgUiChannel

        return AgUiChannel(messaging=self, name=name)

    @overload
    def channel(
        self,
        *,
        name: str,
        codec: None = None,
        renderer: None = None,
    ) -> MessageChannel[Never, Never]: ...

    @overload
    def channel(
        self,
        *,
        name: str,
        codec: MessageCodec[SourceT, ReplayT],
        renderer: SseRenderer[ReplayT] | None = None,
    ) -> MessageChannel[SourceT, ReplayT]: ...

    def channel(
        self,
        *,
        name: str,
        codec: MessageCodec[SourceT, ReplayT] | None = None,
        renderer: SseRenderer[ReplayT] | None = None,
    ) -> MessageChannel[SourceT, ReplayT]:
        """Create a typed channel view that borrows this Messaging lifecycle.

        Args:
            name: Canonical logical channel name used in backend keys.
            codec: Optional explicit source/replay codec. A profiled source can infer it
                when the channel leaves this value unset.
            renderer: Optional replay-to-SSE renderer. A codec implementing the same
                Protocol is reused automatically.

        Returns:
            Reusable typed channel view sharing the parent backend and producer owner.

        Raises:
            MessagingClosed: The parent lifecycle is not open.
            ValueError: ``name`` is not canonical.
        """

        self._require_open()
        if renderer is None and isinstance(codec, SseRenderer):
            renderer = cast(SseRenderer[ReplayT], codec)
        return MessageChannel(
            messaging=self,
            name=name,
            codec=codec,
            renderer=renderer,
        )

    def _start_producer(
        self,
        *,
        prepared: _PreparedRun,
        lease: _OwnerLease,
        source: MessageSource[ProducedT],
        codec: MessageCodec[SourceT, ReplayT],
        codec_input: Callable[[ProducedT], SourceT] | None,
        cancel: _ContextCancelCallback[ProducedT] | None,
        on_committed: CommittedCallback | None,
    ) -> asyncio.Event:
        return _producer_runtime._start_producer(
            self,
            prepared=prepared,
            lease=lease,
            source=source,
            codec=codec,
            codec_input=codec_input,
            cancel=cancel,
            on_committed=on_committed,
        )

    def _start_recoverable_producer(
        self,
        *,
        prepared: _PreparedRun,
        lease: _OwnerLease,
        source: MessageSource[RecoverableMessage[SourceT]],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None,
        on_committed: CommittedCallback | None,
    ) -> asyncio.Event:
        return _producer_runtime._start_recoverable_producer(
            self,
            prepared=prepared,
            lease=lease,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
        )

    def _start_producer_task(
        self,
        *,
        prepared: _PreparedRun,
        lease: _OwnerLease,
        source: MessageSource[ProducedT],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[ProducedT] | None,
        on_committed: CommittedCallback | None,
        prepare_item: Callable[[ProducedT, int], _ProducedMessage[SourceT]],
    ) -> asyncio.Event:
        return _producer_runtime._start_producer_task(
            self,
            prepared=prepared,
            lease=lease,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
            prepare_item=prepare_item,
        )

    def _producer_finished(self, task: asyncio.Task[None]) -> None:
        """Remove and consume a settled owned task to prevent orphan warnings."""

        return _producer_runtime._producer_finished(
            self,
            task,
        )

    @staticmethod
    def _codec_id(codec: MessageCodec[SourceT, ReplayT]) -> str:
        return _producer_runtime._codec_id(
            codec,
        )

    def _require_open(self) -> None:
        if self._state == "new":
            raise MessagingNotStarted("Messaging is not started")
        if self._state != "open":
            raise MessagingClosed("Messaging is closed")
