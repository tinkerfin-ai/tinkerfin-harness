"""AG-UI delivery with automatic source validation and committed run notifications."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import cast

from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent

from tinkerfin_contracts import RunIdentity

from ._message_channel import _settle_unregistered_delivery
from .agui import AgUiCodec, _AgUiRunSource
from .backend import RunStatus
from .messaging import (
    CancelCallback,
    MessageChannel,
    MessageSubscription,
    Messaging,
)
from .models import MessageEnvelope
from .protocols import MessageCodec, ProfiledMessageSource
from .sse import parse_sse_event_id


class _ReplayRenderer:
    """Distinguish the bound committed prefix without altering stored AG-UI events."""

    def __init__(self, codec: AgUiCodec, *, through_seq: int) -> None:
        self._codec = codec
        self._through_seq = through_seq

    def render(self, *, seq: int, payload: BaseEvent) -> bytes:
        frame = self._codec.render(seq=seq, payload=payload)
        if seq > self._through_seq:
            return frame
        event_id, body = frame.split(b"\n", 1)
        return event_id + b"\nevent: replay\n" + body


class AgUiChannel:
    """Deliver AG-UI runs without exposing codec or source-adapter operations.

    The parent Messaging owns producers. Main-run notifications describe committed
    events and run only on the producing worker; replay never repeats them.
    """

    def __init__(self, *, messaging: Messaging, name: str) -> None:
        """Bind one channel to the parent's backend and lifecycle."""
        codec = AgUiCodec()
        self._messaging = messaging
        self._codec = codec
        self._channel = MessageChannel(
            messaging=messaging, name=name, codec=codec, renderer=codec
        )
        self.name = self._channel.name

    async def open_sse(
        self,
        source: ProfiledMessageSource[BaseEvent, BaseEvent],
        *,
        identity: RunIdentity | None = None,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[BaseEvent] | None = None,
        on_source_ready: Callable[[], Awaitable[None]] | None = None,
        on_subscribed: Callable[[], Awaitable[None]] | None = None,
        on_delivery_not_started: Callable[[], Awaitable[None]] | None = None,
        transform_event: Callable[[BaseEvent], BaseEvent | Awaitable[BaseEvent]]
        | None = None,
        on_run_started: Callable[[RunStartedEvent], Awaitable[None]] | None = None,
        on_run_finished: Callable[[RunFinishedEvent | RunErrorEvent], Awaitable[None]]
        | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Start or attach durable delivery and observe committed main-run events.

        Args:
            source: Unconsumed profiled AG-UI source with its cancellation contract.
            identity: Optional equality check against the source's bound identity.
            after: Exclusive replay cursor, resolver, or current tail when omitted.
            cancel: Optional explicit cancellation callback.
            on_source_ready: Producer-only notification before execution starts.
            on_subscribed: Async notification once per owner or attachment after
                subscription succeeds and before returning the body. Failure closes
                only this reader, without cancelling the durable producer or invoking
                `on_delivery_not_started`. Cancellation waits for an accepted callback
                and its cleanup; bound callback I/O with resource timeouts.
            on_delivery_not_started: Cleanup when no producer or attachment started.
            transform_event: Business transformation before validation and persistence.
            on_run_started: Producer-only notification after the main start is committed.
            on_run_finished: Producer-only notification after the main terminal is committed.

        Returns:
            Caller-owned SSE body; closing detaches without cancelling the producer.

        Raises:
            TypeError: The source does not supply the required AG-UI contract, or a
                delivery callback is not callable or does not return an awaitable.
            ValueError: Identity, cursor, or transformed protocol data is invalid.
            MessagingError: Preparation, persistence, or delivery fails.
            BaseException: A delivery callback fails; concurrent cleanup failures
                remain in the exception chain.
        """
        try:
            prepared = _AgUiRunSource.prepare(source, transform_event=transform_event)
        except BaseException as error:  # noqa: BLE001 - cleanup re-raises primary failure
            await _settle_unregistered_delivery(
                error,
                source=source,
                on_delivery_not_started=on_delivery_not_started,
            )
        codec = self._codec

        async def committed(envelope: MessageEnvelope) -> None:
            event = codec.decode(envelope.payload)
            if (
                on_run_started is not None
                and isinstance(event, RunStartedEvent)
                and codec.starts_publication(event, identity=envelope.identity)
            ):
                await on_run_started(event)
            if (
                on_run_finished is not None
                and isinstance(event, RunFinishedEvent | RunErrorEvent)
                and codec.ends_publication(event, identity=envelope.identity)
            ):
                await on_run_finished(event)

        return await self._channel.open_sse(
            prepared,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=committed,
            on_source_ready=on_source_ready,
            on_subscribed=on_subscribed,
            on_delivery_not_started=on_delivery_not_started,
        )

    async def follow(
        self, *, identity: RunIdentity, after: int | None = None
    ) -> MessageSubscription[BaseEvent]:
        """Replay an existing run from its beginning or a previously applied cursor.

        Args:
            identity: Authorized run to follow, without creating a candidate source.
            after: Exclusive cursor in this run; omission replays its complete prefix.

        Returns:
            Closeable subscription pinned to the stored generation and run boundary.

        Raises:
            InvalidCursor: The cursor falls outside this run's committed range.
            MessagingError: The run is missing, expired, deleted, or unavailable.
        """
        preflight = self._messaging._begin_preflight()
        try:
            prepared, through_seq = await self._messaging._runtime_backend.bind_replay(
                channel=self.name, identity=identity, after=after
            )
            self._messaging._require_open()
            return MessageSubscription[BaseEvent]._create(
                ledger=self._messaging._runtime_backend,
                prepared=prepared,
                codec=cast(MessageCodec[object, BaseEvent], self._codec),
                renderer=_ReplayRenderer(self._codec, through_seq=through_seq),
            )
        finally:
            self._messaging._finish_preflight(preflight)

    async def follow_sse(
        self, *, identity: RunIdentity, last_event_id: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """Attach HTTP delivery to an existing run without executing an agent.

        Args:
            identity: Application-authorized run to replay and follow.
            last_event_id: Last applied SSE ID; omission reconstructs the whole run.

        Returns:
            Caller-owned byte stream. Events committed when this subscription binds
            use the SSE event name ``replay``; later events use the default ``message``.
            IDs and AG-UI data are unchanged. Closing only detaches this reader.

        Raises:
            ValueError: The event ID is not a canonical non-negative decimal value.
            MessagingError: The run or cursor cannot be read safely.
        """
        subscription = await self.follow(
            identity=identity, after=parse_sse_event_id(last_event_id)
        )
        try:
            return subscription.to_sse()
        except BaseException:
            await subscription.aclose()
            raise

    async def publish(
        self,
        message: BaseEvent,
        *,
        identity: RunIdentity,
        message_id: str | None = None,
    ) -> MessageEnvelope:
        """Commit a business CUSTOM event while the main run accepts publication.

        Args:
            message: Business CUSTOM event; protocol lifecycle events are rejected.
            identity: Authorized running conversation.
            message_id: Optional idempotency key for a business notification.

        Returns:
            The committed envelope, including its stable sequence.

        Raises:
            MessagingError: Publication is rejected or storage fails.
        """
        return await self._channel.publish(
            message, identity=identity, message_id=message_id
        )

    async def get_run_status(self, *, identity: RunIdentity) -> RunStatus:
        """Read the existing producer's status without starting an agent."""
        return await self._channel.get_run_status(identity=identity)

    async def cancel(self, *, identity: RunIdentity) -> bool:
        """Cancel the existing producer and wait for its authoritative settlement."""
        return await self._channel.cancel(identity=identity)

    async def delete_stream(self, *, identity: RunIdentity) -> None:
        """Delete an inactive thread's retained delivery without touching its history."""
        await self._channel.delete_stream(identity=identity)
