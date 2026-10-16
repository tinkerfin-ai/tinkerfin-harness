"""Single-use runs that prepare resources only after execution admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, Self, TypeVar

from tinkerfin_contracts import RunIdentity
from tinkerfin_native_stream import NativeStreamFrame

from ._failure_evidence import (
    has_non_cancellation_failure,
    retain_failure,
    select_failure,
)
from ._optional_dependencies import require_agui
from ._run_callbacks import bind_run, in_run_callback, reset_run
from ._run_owner import RunOwner
from ._tasks import OwnedOperationFailures, join_task
from .coordination import RunCoordinator
from .errors import AgUiSettlementTimeoutError, TinkerFinLifecycleError
from .native import NativeStreamPart
from .sse import SseBody, SseEventIdResolver, SseMapper

if TYPE_CHECKING:
    from ag_ui.core import BaseEvent

ItemT = TypeVar("ItemT")
ItemT_co = TypeVar("ItemT_co", covariant=True)


class _PreparedRun(Protocol[ItemT_co]):
    @property
    def error(self) -> Exception | None: ...

    def __aiter__(self) -> AsyncIterator[ItemT_co]: ...

    async def __anext__(self) -> ItemT_co: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _PreparationOutcome(Generic[ItemT]):
    stream: _PreparedRun[ItemT] | None = None
    error: BaseException | None = None


class _LazyRun(Generic[ItemT]):
    """Own one preparation task and its resulting managed stream.

    No task exists until preflight or consumption begins. Closing an unused candidate
    only marks it closed. Started preparation is cancelled and joined before its
    result is closed, including a result returned while cancellation was pending.
    """

    _identity: RunIdentity
    _prepare: Callable[[RunOwner], Awaitable[_PreparedRun[ItemT]]]
    _coordinator: RunCoordinator | None
    _owner: RunOwner | None
    _preparation: asyncio.Task[_PreparationOutcome[ItemT]] | None
    _prepared: _PreparedRun[ItemT] | None
    _preparation_error: BaseException | None
    _owned_operation_failures: OwnedOperationFailures
    _external_closers: int

    def __init__(self) -> None:
        """Require AgentRuntime to create and own the run's preparation boundary."""

        raise TypeError(
            "use AgentRuntime.open_run() or open_agui_run() to create a run"
        )

    @classmethod
    def _create(
        cls,
        identity: RunIdentity,
        prepare: Callable[[RunOwner], Awaitable[_PreparedRun[ItemT]]],
        coordinator: RunCoordinator | None,
    ) -> Self:
        self = cls.__new__(cls)
        self._identity = identity
        self._prepare = prepare
        self._coordinator = coordinator
        self._owner = None
        self._preparation = None
        self._prepared = None
        self._preparation_error = None
        self._preparation_error_delivered = False
        self._claimed = False
        self._closed = False
        self._pulling = False
        self._external_closers = 0
        self._owned_operation_failures = OwnedOperationFailures()
        return self

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the immutable identity available before resource preparation."""

        return self._identity

    @property
    def error(self) -> Exception | None:
        """Return a known preparation or execution failure, if any."""

        if isinstance(self._preparation_error, Exception):
            return self._preparation_error
        return None if self._prepared is None else self._prepared.error

    def __aiter__(self) -> AsyncIterator[ItemT]:
        """Claim this run for a single asynchronous consumer."""

        if self._claimed:
            raise TinkerFinLifecycleError("a run stream can only be consumed once")
        if self._closed:
            raise TinkerFinLifecycleError("a closed run stream cannot be consumed")
        self._claimed = True
        return self

    async def __anext__(self) -> ItemT:
        """Prepare once and return the next item while preserving backpressure."""

        if self._closed:
            raise StopAsyncIteration
        if self._pulling:
            raise TinkerFinLifecycleError("a run stream already has an active pull")
        self._pulling = True
        token = bind_run(self)
        try:
            with self._owned_operation_failures.capture():
                stream = await self._ready()
                part = await anext(stream)
            if self._closed and not self._external_closers:
                await self._close_managed()
            return part
        except BaseException as primary:
            # The external closer may be waiting for this consumer to stop.
            # Callback-initiated close is cooperative: the consumer closes only
            # after the callback and its current observation/event have returned.
            if not self._external_closers:
                await self._close_after_failure(primary)
            raise
        finally:
            reset_run(token)
            self._pulling = False

    async def _close_after_failure(self, primary: BaseException) -> None:
        try:
            await self._close_managed()
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, AgUiSettlementTimeoutError):
                if cleanup_error is not primary:
                    primary.add_note(str(cleanup_error))
                return
            if cleanup_error is primary:
                # Retained close can re-deliver the exact primary exception.
                # Do not create a self-cause or append notes while iterating them.
                self._owned_operation_failures.settle(primary)
                return
            if not isinstance(cleanup_error, Exception | asyncio.CancelledError) or (
                isinstance(primary, Exception)
                and isinstance(cleanup_error, asyncio.CancelledError)
            ):
                retain_failure(
                    cleanup_error, primary, label="Run processing also failed"
                )
                raise cleanup_error
            for note in getattr(cleanup_error, "__notes__", ()):
                primary.add_note(note)
            if cleanup_error is not primary:
                retain_failure(primary, cleanup_error, label="Run cleanup also failed")
        self._owned_operation_failures.settle(primary)

    async def _prepare_once(self) -> _PreparationOutcome[ItemT]:
        token = bind_run(self)
        try:
            owner = RunOwner(self._identity, self._coordinator)
            self._owner = owner

            async def prepare_stream(owner: RunOwner) -> _PreparedRun[ItemT]:
                stream = await self._prepare(owner)
                # Receive the stream before the native Task publishes its result.
                # Caller cancellation can interrupt the later await, but cannot
                # orphan a successful stream or hide a failed stream's known error.
                self._prepared = stream
                return stream

            stream = await owner.prepare(prepare_stream)
            if self._closed:
                # A preparation callback may request closure. It cannot join the
                # operation it is part of; preparation releases its result here.
                await stream.aclose()
            return _PreparationOutcome(stream=stream)
        except BaseException as error:  # noqa: BLE001 - return control failures to the owning caller
            if self._prepared is not None and self._prepared.error is not None:
                error = select_failure(error, self._prepared.error)
            self._preparation_error = error
            return _PreparationOutcome(error=error)
        finally:
            reset_run(token)

    async def _ready(self) -> _PreparedRun[ItemT]:
        if self._closed:
            raise TinkerFinLifecycleError("a closed run cannot prepare resources")
        task = self._preparation
        if task is None:
            task = asyncio.create_task(
                self._prepare_once(), name="tinkerfin-run-preparation"
            )
            self._preparation = task
        outcome = await asyncio.shield(task)
        if outcome.error is not None:
            self._preparation_error_delivered = True
            raise outcome.error
        if self._closed:
            raise asyncio.CancelledError("run closed during preparation")
        if outcome.stream is None:
            raise TinkerFinLifecycleError("run preparation produced no stream")
        return outcome.stream

    async def messaging_owner_preflight(self) -> None:
        """Prepare after Messaging has acquired producer ownership."""

        try:
            with self._owned_operation_failures.capture():
                await self._ready()
        except BaseException as primary:
            if not self._closed:
                await self._close_after_failure(primary)
            raise

    async def aclose(self) -> None:
        """Close the run, or request close from inside its own callback.

        A run callback cannot wait for itself. Its close request preserves the
        current delivered item and is settled by the enclosing run operation.
        External callers wait for preparation and managed cleanup. An AG-UI
        settlement timeout leaves cleanup owned by this stream; await aclose()
        again before disposing borrowed resources. Closing an unused run does
        not start preparation.

        Raises:
            AgUiSettlementTimeoutError: AG-UI cleanup has not finished within its
                configured waiting limit; its cleanup task remains owned.
            BaseException: Preparation or cleanup fails, or the caller is cancelled.
        """

        self._closed = True
        if in_run_callback(self):
            return
        self._external_closers += 1
        try:
            try:
                with self._owned_operation_failures.capture(closing=True):
                    await self._close_managed()
            except BaseException as error:
                if not isinstance(error, AgUiSettlementTimeoutError):
                    self._owned_operation_failures.settle(error)
                raise
            self._owned_operation_failures.settle(None)
        finally:
            self._external_closers -= 1

    async def _close_managed(self) -> None:
        self._closed = True
        token = bind_run(self)
        try:
            with self._owned_operation_failures.capture(closing=True):
                await self._close_resources()
        finally:
            reset_run(token)

    async def _close_resources(self) -> None:
        preparation = self._preparation
        if preparation is None:
            return
        primary: BaseException | None = None
        try:
            await join_task(preparation, cancel=True, suppress_task_cancellation=True)
        except BaseException as error:  # noqa: BLE001 - consume the settled result even when its join is cancelled
            primary = error
        if preparation.done() and not preparation.cancelled():
            # join_task waits for completion even when it then raises caller
            # cancellation. Consume that completed result on both paths, before
            # another close can mistake it for a newly discovered failure.
            outcome = preparation.result()
            if outcome.error is not None and not self._preparation_error_delivered:
                self._preparation_error_delivered = True
                if has_non_cancellation_failure(outcome.error):
                    primary = (
                        outcome.error
                        if primary is None
                        else select_failure(primary, outcome.error)
                    )
        if self._prepared is not None:
            try:
                await self._prepared.aclose()
            except AgUiSettlementTimeoutError:
                # The prepared stream still owns cleanup and its native Run owner.
                # Joining that owner here would defeat the caller's waiting budget.
                raise
            except BaseException as cleanup_error:  # noqa: BLE001 - retain both cleanup and preparation failures
                if primary is None:
                    primary = cleanup_error
                else:
                    primary = select_failure(primary, cleanup_error)
        if self._owner is not None:
            try:
                await self._owner.aclose()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve ownership cleanup with any earlier failure
                if primary is None:
                    primary = cleanup_error
                else:
                    primary = select_failure(primary, cleanup_error)
        if primary is not None:
            raise primary


class NativeRunStream(_LazyRun[Mapping[str, object]]):
    """A lazy Native run with canonical replay and deterministic close."""

    @property
    def messaging_codec_profile(self) -> str:
        """Return the Native codec profile before execution starts."""

        return "tinkerfin.native-stream"

    @property
    def messaging_source_type(self) -> type[Mapping[str, object]]:
        """Return the live Native object type."""

        return Mapping

    @property
    def messaging_codec_input_type(self) -> type[NativeStreamPart]:
        """Return the validated input type used for durable encoding."""

        return NativeStreamPart

    @property
    def messaging_replay_type(self) -> type[NativeStreamPart]:
        """Return the decoded Native replay type."""

        return NativeStreamPart

    def _take_frame(self, part: object) -> NativeStreamFrame:
        from .runtime import NativeGraphRunStream

        stream = self._prepared
        if not isinstance(stream, NativeGraphRunStream):
            raise TinkerFinLifecycleError("the Native run has no prepared frame")
        return stream._take_frame(part)

    def messaging_codec_input(self, part: Mapping[str, object]) -> NativeStreamPart:
        """Claim the canonical replay model paired with the last delivered item.

        Args:
            part: Raw object most recently yielded by this run.

        Returns:
            The immutable replay model, without serializing the raw object again.

        Raises:
            TinkerFinLifecycleError: The item is not the last delivered object or
                its replay model was already claimed.
        """

        return self._take_frame(part).replay

    def to_sse(
        self,
        *,
        timeout: float | None = None,
        mapper: SseMapper[NativeStreamPart] | None = None,
        event_id_resolver: SseEventIdResolver[NativeStreamPart] | None = None,
    ) -> SseBody[bytes]:
        """Return the lazy Native run as single-use UTF-8 SSE frames."""

        from ._runtime_streams import to_sse

        return to_sse(
            self, timeout=timeout, mapper=mapper, event_id_resolver=event_id_resolver
        )


class AgUiRunStream(_LazyRun["BaseEvent"]):
    """A lazy AG-UI run with producer-owned cancellation and SSE framing."""

    @property
    def messaging_codec_profile(self) -> str:
        """Return the AG-UI codec profile before execution starts."""

        return "agui.event"

    @property
    def messaging_source_type(self) -> type[BaseEvent]:
        """Return the live AG-UI event type without opening a run."""

        require_agui()
        from ag_ui.core import BaseEvent

        return BaseEvent

    @property
    def messaging_replay_type(self) -> type[BaseEvent]:
        """Return the durable AG-UI replay type."""

        return self.messaging_source_type

    @property
    def messaging_cancel_waits_for_first_item(self) -> bool:
        """Keep cancellation behind the main lifecycle's first event."""

        return True

    @property
    def messaging_cancel_callback(self) -> Callable[[], Awaitable[list[BaseEvent]]]:
        """Return the same cancellation owner before and after preparation."""

        return self.abort

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Check whether a supplied callback belongs to this exact run."""

        return callback == self.abort

    async def abort(self) -> list[BaseEvent]:
        """Cancel execution and return its remaining terminal events at most once.

        An unused run closes without preparation and returns an empty list. An
        admitted run closes its open event lifecycles before the cancellation
        terminal. Deliver the returned events in order to the same consumer.

        Returns:
            Remaining cancellation events, or an empty list when no tail is due.

        Raises:
            TinkerFinLifecycleError: Called from a callback belonging to this run.
            AgUiSettlementTimeoutError: Cleanup is still owned by the run; await
                aclose() again before disposing borrowed resources.
            BaseException: Cancellation or cleanup fails.
        """

        from .runtime import AgUiEventStream

        stream = self._prepared
        if isinstance(stream, AgUiEventStream) and (
            stream._completed or stream._abort_events_delivered
        ):
            return []
        if in_run_callback(self):
            raise TinkerFinLifecycleError(
                "abort() cannot be called from a run callback"
            )
        if stream is None:
            await self.aclose()
            return []
        if not isinstance(stream, AgUiEventStream):
            raise TinkerFinLifecycleError("the AG-UI run has no prepared event stream")
        self._closed = True
        self._external_closers += 1
        try:
            return await stream.abort()
        finally:
            self._external_closers -= 1

    def to_sse(
        self,
        *,
        mapper: SseMapper[BaseEvent] | None = None,
        event_id_resolver: SseEventIdResolver[BaseEvent] | None = None,
    ) -> SseBody[bytes]:
        """Return the lazy AG-UI run as single-use UTF-8 SSE frames."""

        from ._runtime_agui import to_sse

        return to_sse(self, mapper=mapper, event_id_resolver=event_id_resolver)


__all__ = ["AgUiRunStream", "NativeRunStream"]
