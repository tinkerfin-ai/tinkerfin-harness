"""Runtime observation normalization, fan-out, and session ownership."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, cast

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.types import Command
from pydantic import JsonValue

from tinkerfin_contracts import (
    NativeExtraObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeMessageType,
    NativeObservation,
    NativeStateObservation,
    NativeTaskObservation,
    NativeToolCall,
    NativeToolCallChunk,
    ObservationBoundary,
    RunClosedObservation,
    RunInputKind,
    RunInputObservation,
    RunMode,
    RunObservationSession,
    RunObserverFailedObservation,
    RunResumeCheckpointedObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
    RuntimeObservation,
    RuntimeObserver,
)
from tinkerfin_native_stream import (
    NativeExtraStreamPart,
    NativeMessageStreamPart,
    NativeRuntimeInterrupt,
    NativeTaskResultPayload,
    NativeTasksStreamPart,
    NativeTaskStartPayload,
    NativeUpdatesStreamPart,
    NativeValidatedStreamPart,
    NativeValuesStreamPart,
    to_json_value,
)

from ._failure_evidence import retain_failure, select_failure
from ._run_callbacks import callback_scope
from ._tasks import join_task
from .errors import RunObservationError

if TYPE_CHECKING:
    from ._compaction_observation import CompactionOperation

_CALLBACK_TIMESTAMP: ContextVar[tuple[datetime, int] | None] = ContextVar(
    "tinkerfin_callback_timestamp", default=None
)


def _stamp() -> tuple[datetime, int]:
    return _CALLBACK_TIMESTAMP.get() or (datetime.now(UTC), time.monotonic_ns())


def _json_value(value: object) -> JsonValue:
    return to_json_value(value)


def _source_value(value: object) -> JsonValue:
    """Snapshot supported source values and omit opaque host resources safely."""

    try:
        return _json_value(value)
    except (TypeError, ValueError):
        return {
            "$type": "omitted",
            "class": _qualified_name(value),
        }


def _json_object(value: object) -> dict[str, JsonValue]:
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise TypeError("normalized observation value must be an object")
    return normalized


def _qualified_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _message_type(message: BaseMessage) -> NativeMessageType:
    if isinstance(message, AIMessageChunk):
        return "assistant_chunk"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, HumanMessage):
        return "human"
    if isinstance(message, ToolMessage):
        return "tool"
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, ChatMessage):
        return "chat"
    if isinstance(message, RemoveMessage):
        return "remove"
    return "other"


def _message_record(message: BaseMessage) -> NativeMessageRecord:
    """Detach one LangChain message into the stable protocol-neutral record.

    Full IDs and every Tool fragment are preserved for correlation. Provider-specific
    ``additional_kwargs`` are intentionally absent; verified reasoning uses a separate
    extractor and Observation so private metadata cannot leak through this core record.
    """

    tool_calls: tuple[NativeToolCall, ...] = ()
    tool_chunks: tuple[NativeToolCallChunk, ...] = ()
    # AIMessageChunk subclasses AIMessage in LangChain Core 1.6.1. Its derived
    # ``tool_calls`` view can contain an intentionally ID-less entry while a later
    # provider fragment contributes arguments to an existing index. Only complete
    # AIMessage snapshots may populate the complete-call contract; the locked stream
    # evidence and test_v2_driver_keeps_idless_followup_tool_data_as_a_chunk protect
    # this distinction.
    if isinstance(message, AIMessageChunk):
        tool_chunks = tuple(
            NativeToolCallChunk(
                index=chunk["index"],
                id=chunk.get("id"),
                name=chunk.get("name"),
                arguments=chunk.get("args") or "",
            )
            for chunk in message.tool_call_chunks
        )
    elif isinstance(message, AIMessage):
        normalized_calls: list[NativeToolCall] = []
        for call in message.tool_calls:
            tool_call_id = call.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("complete Tool calls require a stable ID")
            normalized_calls.append(
                NativeToolCall(
                    id=tool_call_id,
                    name=call["name"],
                    arguments=_json_object(call.get("args", {})),
                )
            )
        tool_calls = tuple(normalized_calls)
    response_metadata = _json_object(message.response_metadata)
    raw_usage = getattr(message, "usage_metadata", None)
    usage_metadata = None if raw_usage is None else _json_object(raw_usage)
    tool_call_id = message.tool_call_id if isinstance(message, ToolMessage) else None
    raw_tool_status = message.status if isinstance(message, ToolMessage) else None
    tool_status = raw_tool_status if raw_tool_status in {"success", "error"} else None
    return NativeMessageRecord(
        message_type=_message_type(message),
        id=message.id,
        name=message.name,
        content=_json_value(message.content),
        tool_calls=tool_calls,
        tool_call_chunks=tool_chunks,
        tool_call_id=tool_call_id,
        tool_status=tool_status,
        response_metadata=response_metadata,
        usage_metadata=usage_metadata,
    )


def _interrupt_record(value: object) -> NativeInterruptRecord:
    """Normalize one validated task interrupt into the shared Native contract."""

    interrupt = NativeRuntimeInterrupt.model_validate(value)
    return NativeInterruptRecord(
        id=interrupt.id,
        value=interrupt.value,
    )


def native_observation(
    part: NativeValidatedStreamPart,
    *,
    context: RunSourceContext,
) -> NativeObservation:
    """Project one validated live part into a protocol-neutral observation."""

    observed_at, monotonic_ns = _stamp()
    if isinstance(part, NativeMessageStreamPart):
        return NativeMessageObservation(
            identity=context.identity,
            graph_namespace=part.ns,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            message=_message_record(part.data.message),
            metadata=_json_object(part.data.metadata.model_dump(mode="python")),
        )
    if isinstance(part, NativeTasksStreamPart):
        payload = part.data
        if isinstance(payload, NativeTaskStartPayload):
            metadata = payload.metadata
            return NativeTaskObservation(
                identity=context.identity,
                graph_namespace=part.ns,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="start",
                task_id=payload.id,
                name=payload.name,
                triggers=payload.triggers,
                input=_source_value(payload.input),
                metadata=(
                    {}
                    if metadata is None
                    else _json_object(metadata.model_dump(mode="python"))
                ),
            )
        if not isinstance(payload, NativeTaskResultPayload):
            raise TypeError("validated task payload has an unsupported phase")
        error = payload.error
        return NativeTaskObservation(
            identity=context.identity,
            graph_namespace=part.ns,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="result",
            task_id=payload.id,
            name=payload.name,
            result=_source_value(payload.result),
            error_type=None if error is None else _qualified_name(error),
            interrupts=tuple(_interrupt_record(value) for value in payload.interrupts),
        )
    if isinstance(part, NativeValuesStreamPart):
        raw_messages = part.data.get("messages", ())
        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages, (str, bytes, bytearray)
        ):
            raise TypeError("values messages must be a sequence")
        messages: list[NativeMessageRecord] = []
        for raw_message in cast(Sequence[object], raw_messages):
            if not isinstance(raw_message, BaseMessage):
                raise TypeError("values messages must contain LangChain messages")
            messages.append(_message_record(raw_message))
        private_keys = frozenset(context.private_state_keys)
        state = {
            key: _source_value(value)
            for key, value in part.data.items()
            if key != "messages" and key not in private_keys
        }
        return NativeStateObservation(
            identity=context.identity,
            graph_namespace=part.ns,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            state=state,
            messages=tuple(messages),
            interrupts=tuple(
                NativeInterruptRecord(id=value.id, value=_json_value(value.value))
                for value in part.interrupts
            ),
        )
    if not isinstance(part, NativeExtraStreamPart | NativeUpdatesStreamPart):
        raise TypeError("validated Native part has an unsupported mode")
    data = _json_value(part.data)
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return NativeExtraObservation(
        identity=context.identity,
        graph_namespace=part.ns,
        observed_at=observed_at,
        monotonic_ns=monotonic_ns,
        mode=part.type,
        data_type=_qualified_name(part.data),
        safe_size_bytes=len(encoded),
        top_level_keys=tuple(sorted(data)) if isinstance(data, dict) else (),
    )


def native_input_kind(graph_input: object) -> RunInputKind:
    """Distinguish checkpoint continuation from an actual interrupt response.

    LangGraph resumes existing tasks for None or Command input. Only a Command
    carrying resume values answers dynamic interrupts; update and goto remain input
    evidence without authorizing interaction resolution.
    """

    if isinstance(graph_input, Command):
        return "resume" if graph_input.resume is not None else "continuation"
    return "continuation" if graph_input is None else "ordinary"


def source_context(
    *,
    identity: object,
    runtime_profile: str,
    input_kind: RunInputKind,
    parent_run_id: str | None,
    mode: RunMode,
    graph_input: object,
    config: object,
    private_state_keys: frozenset[str],
    resume: tuple[RunResumeSummary, ...] = (),
    call_tracking_enabled: bool = False,
) -> RunSourceContext:
    """Build one finite source snapshot before opening Observer resources."""

    from tinkerfin_contracts import RunIdentity

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be a RunIdentity")
    return RunSourceContext(
        identity=identity,
        runtime_profile=runtime_profile,
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        mode=mode,
        input=_source_value(graph_input),
        config=_source_value(config),
        resume=resume,
        private_state_keys=tuple(sorted(private_state_keys)),
        call_tracking_enabled=call_tracking_enabled,
    )


@dataclass(slots=True)
class _SessionSlot:
    index: int
    name: str
    session: RunObservationSession
    waiter: asyncio.Task[None] | None = None
    healthy: bool = True


@dataclass(slots=True)
class _ObserverDelivery:
    """Carry one accepted serial Observer operation to the Hub-owned worker."""

    operation: Callable[[], Awaitable[None]]
    settled: asyncio.Future[None]
    task: asyncio.Task[BaseException | None] | None = None
    cancel_requested: bool = False


class RuntimeObservationHub:
    """Own ordered Observer sessions and one active failure signal for a Run."""

    def __init__(
        self,
        *,
        context: RunSourceContext,
        observers: tuple[RuntimeObserver, ...],
        initialization_error: Exception | None = None,
    ) -> None:
        """Bind Run context to ordered borrowed Observer registrations."""

        from ._call_observation import RuntimeCallHandler
        from ._sync_call_observation import SyncRuntimeCallHandler

        self.compactions: dict[str, CompactionOperation] = {}
        self.context = context
        self._observers = observers
        self._initialization_error = initialization_error
        self._slots: list[_SessionSlot] = []
        self._failure: asyncio.Future[tuple[_SessionSlot, BaseException]] | None = None
        self._started = False
        self._terminal: RunTerminalOutcome | None = None
        self._closed = False
        self._claimed_errors: list[BaseException] = []
        self._delivery_queue: asyncio.Queue[_ObserverDelivery | None] | None = None
        self._delivery_worker: asyncio.Task[None] | None = None
        self._delivery_stop: asyncio.Task[None] | None = None
        self._call_loop: asyncio.AbstractEventLoop | None = None
        self._thread_callbacks: set[asyncio.Task[None]] = set()
        self._call_handler = RuntimeCallHandler(self)
        self._sync_call_handler = SyncRuntimeCallHandler(self, self._call_handler)

    @property
    def enabled(self) -> bool:
        """Return whether the Run has any Observer sessions to manage."""

        return bool(self._observers)

    @property
    def terminal_outcome(self) -> RunTerminalOutcome | None:
        """Return the first selected terminal outcome, if terminal was published."""

        return self._terminal

    @property
    def call_handler(self) -> AsyncCallbackHandler:
        """Return the request-owned LangChain callback for invocation configuration."""

        return self._call_handler

    @property
    def call_handlers(self) -> tuple[BaseCallbackHandler, ...]:
        """Return distinct native async and sync callbacks over the same Run state."""
        return self._call_handler, self._sync_call_handler

    @property
    def call_loop(self) -> asyncio.AbstractEventLoop:
        """Return the loop that owns callback state and Observer delivery."""
        if self._call_loop is None:
            raise RuntimeError("Runtime callbacks require an opened observation Run")
        return self._call_loop

    def in_call_loop(self) -> bool:
        """Return whether a callback arrived directly on its owning event loop."""
        try:
            return asyncio.get_running_loop() is self._call_loop
        except RuntimeError:
            return False

    async def deliver_thread_callback(
        self, deliver: Callable[[], Awaitable[None]]
    ) -> None:
        """Own one foreign-thread delivery and reject callbacks after termination."""
        if self._closed or self._terminal is not None:
            raise asyncio.CancelledError("Runtime callback outlived its Run")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Runtime callback delivery requires an owned task")
        self._thread_callbacks.add(task)
        try:
            await deliver()
        finally:
            self._thread_callbacks.discard(task)

    async def _settle_thread_callbacks(self) -> None:
        callbacks = tuple(self._thread_callbacks)
        for callback in callbacks:
            callback.cancel()
        if callbacks:
            await asyncio.gather(*callbacks, return_exceptions=True)

    def claim_error(self, error: BaseException) -> bool:
        """Return whether one propagated failure has no more specific recorded owner."""

        if any(claimed is error for claimed in self._claimed_errors):
            return False
        # Retaining the object prevents CPython from reusing its address for a distinct
        # later failure during the same Run. Trace limits bound accepted observations,
        # and the whole request-scoped Hub is released after close.
        self._claimed_errors.append(error)
        return True

    async def start(self) -> None:
        """Open every Observer, then publish ordered Run start and input facts.

        Cancellation is never converted. Ordinary opening failures are aggregated,
        reported to every healthy session, and fail the Runtime before any Native pull.
        """

        if self._started:
            return
        loop = asyncio.get_running_loop()
        self._call_loop = loop
        self._failure = loop.create_future()
        opening_failures: list[tuple[str, BaseException]] = []
        for index, observer in enumerate(self._observers):
            name = f"{type(observer).__module__}.{type(observer).__qualname__}"
            try:
                with callback_scope():
                    session = await observer.open_run(
                        self.context.model_copy(deep=True)
                    )
                if not isinstance(session, RunObservationSession):
                    raise TypeError(
                        "RuntimeObserver.open_run must return RunObservationSession"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - Observer extension boundary
                opening_failures.append((name, error))
                continue
            slot = _SessionSlot(index=index, name=name, session=session)
            slot.waiter = asyncio.create_task(
                self._watch(slot),
                name=f"tinkerfin-observer-failure:{index}",
            )
            self._slots.append(slot)
        self._started = True
        self._start_delivery_worker()
        if opening_failures:
            error = self._observation_error(opening_failures)
            await self._deliver_failure_notifications(opening_failures)
            raise error
        observed_at, monotonic_ns = _stamp()
        await self.observe(
            RunStartedObservation(
                identity=self.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        )
        observed_at, monotonic_ns = _stamp()
        await self.observe(
            RunInputObservation(
                identity=self.context.identity,
                source=self.context,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        )

    async def _watch(self, slot: _SessionSlot) -> None:
        try:
            error = await slot.session.failure_waiter()
            if not isinstance(error, BaseException):
                error = TypeError(
                    "RunObservationSession.failure_waiter must resolve to an exception"
                )
        except asyncio.CancelledError:
            return
        except BaseException as caught:  # noqa: BLE001 - relay process control
            error = caught
        failure = self._failure
        if failure is not None and not failure.done():
            failure.set_result((slot, error))

    async def wait_failure(self) -> None:
        """Wait for the first active session failure and propagate it fail-closed."""

        failure = self._failure
        if failure is None:
            failure = asyncio.get_running_loop().create_future()
            self._failure = failure
        slot, error = await asyncio.shield(failure)
        # The waiter transports a session-returned process-control exception through
        # the managed race; it must never enter the ordinary Observer error family.
        if not isinstance(error, Exception):
            raise error
        if not slot.healthy:
            raise self._observation_error(((slot.name, error),))
        slot.healthy = False
        failures = ((slot.name, error),)
        await self._deliver_failure_notifications(failures)
        raise self._observation_error(failures)

    async def observe(self, observation: RuntimeObservation) -> None:
        """Broadcast a defensive copy in registration order to healthy sessions.

        One Observer cannot mutate the value seen by another. All failures from this
        broadcast are collected before healthy sessions receive failure notifications.

        Args:
            observation: Immutable Runtime fact copied once per healthy session.

        Raises:
            asyncio.CancelledError: The caller or Observer cancels delivery.
            RunObservationError: One or more ordinary Observer deliveries fail.
        """

        if self._closed:
            raise RuntimeError("Runtime Observation Hub is closed")
        await self._deliver_observation(observation)

    def _start_delivery_worker(self) -> None:
        """Start one bounded worker only when at least one Observer was opened."""

        if not self._slots or self._delivery_worker is not None:
            return
        self._delivery_queue = asyncio.Queue(maxsize=1)
        self._delivery_worker = asyncio.create_task(
            self._deliver_observer_operations(),
            name="tinkerfin-observer-delivery",
        )

    async def _deliver_observer_operations(self) -> None:
        """Execute accepted Observer I/O in order without a coordination lock."""

        queue = self._delivery_queue
        if queue is None:  # pragma: no cover - worker construction invariant
            raise RuntimeError("Observer delivery queue is unavailable")
        while True:
            delivery = await queue.get()
            if delivery is None:
                return
            if delivery.cancel_requested:
                if not delivery.settled.done():
                    delivery.settled.set_exception(asyncio.CancelledError())
                continue
            operation = asyncio.create_task(
                self._run_delivery_operation(delivery.operation),
                name="tinkerfin-observer-operation",
            )
            delivery.task = operation
            owner_cancelled = False
            try:
                try:
                    # Request cancellation targets the operation, while host
                    # shutdown targets its worker. Separate ownership prevents
                    # Runner cancellation from keeping the worker alive for
                    # another delivery.
                    await asyncio.wait((operation,))
                except asyncio.CancelledError as cancellation:
                    owner_cancelled = True
                    if not operation.done() and not operation.cancelling():
                        operation.cancel()
                    error = await join_task(operation, suppress_task_cancellation=True)
                    if error is not None:
                        if not isinstance(error, Exception | asyncio.CancelledError):
                            retain_failure(
                                error,
                                cancellation,
                                label="Observer delivery was also cancelled",
                            )
                            raise error
                        retain_failure(
                            cancellation,
                            error,
                            label="Observer operation cleanup also failed",
                        )
                    raise cancellation
                error = operation.result()
                if error is not None:
                    raise error
            except BaseException as error:
                if not delivery.settled.done():
                    delivery.settled.set_exception(error)
                if owner_cancelled:
                    raise
            else:
                if not delivery.settled.done():
                    delivery.settled.set_result(None)
            finally:
                delivery.task = None

    @staticmethod
    async def _run_delivery_operation(
        operation: Callable[[], Awaitable[None]],
    ) -> BaseException | None:
        """Return the exact failure for rethrowing on the delivery caller's task.

        A private task must not make SystemExit or KeyboardInterrupt bypass the
        caller's exception boundary. Cancellation uses the same receipt path;
        the worker keeps request cancellation separate from its own shutdown.
        """

        try:
            with callback_scope():
                await operation()
        except BaseException as error:  # noqa: BLE001 - transport without conversion
            return error
        return None

    async def _enqueue_delivery(
        self,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Wait for bounded delivery while detecting loss of its owned worker.

        Host shutdown can cancel the worker before Runtime terminal cleanup runs.
        Queue admission and each receipt must both observe that exit so cleanup
        cannot wait indefinitely for an operation that no task can deliver.
        """

        queue = self._delivery_queue
        if queue is None:
            await operation()
            return
        worker = self._delivery_worker
        if worker is None:  # pragma: no cover - queue construction invariant
            raise RuntimeError("Observer delivery worker is unavailable")
        settled = asyncio.get_running_loop().create_future()
        delivery = _ObserverDelivery(operation=operation, settled=settled)
        try:
            await self._put_delivery(queue, worker, delivery)
            await asyncio.wait((settled, worker), return_when=asyncio.FIRST_COMPLETED)
            if not settled.done():
                self._raise_delivery_worker_stopped(worker)
            settled.result()
        except asyncio.CancelledError:
            delivery.cancel_requested = True
            operation_task = delivery.task
            if (
                operation_task is not None
                and not operation_task.done()
                and not operation_task.cancelling()
            ):
                operation_task.cancel()
            settled.add_done_callback(_consume_delivery_exception)
            raise
        finally:
            if worker.done() and not settled.done():
                settled.cancel()

    async def _put_delivery(
        self,
        queue: asyncio.Queue[_ObserverDelivery | None],
        worker: asyncio.Task[None],
        delivery: _ObserverDelivery | None,
    ) -> None:
        """Own bounded queue admission until it succeeds or the consumer stops."""

        if worker.done():
            self._raise_delivery_worker_stopped(worker)
        queued = asyncio.create_task(
            queue.put(delivery), name="tinkerfin-observer-enqueue"
        )
        try:
            await asyncio.wait((queued, worker), return_when=asyncio.FIRST_COMPLETED)
            if not queued.done():
                self._raise_delivery_worker_stopped(worker)
            queued.result()
        finally:
            await join_task(queued, cancel=True, suppress_task_cancellation=True)

    def _raise_delivery_worker_stopped(self, worker: asyncio.Task[None]) -> NoReturn:
        """Preserve cancellation or fail explicitly after an unexpected normal exit."""

        worker.result()
        raise self._observation_error(
            (
                (
                    "runtime.delivery_worker",
                    RuntimeError("Observer delivery worker stopped"),
                ),
            )
        )

    async def _deliver_observation(self, observation: RuntimeObservation) -> None:
        async def broadcast() -> None:
            failures: list[tuple[str, BaseException]] = []
            for slot in self._slots:
                if not slot.healthy:
                    continue
                try:
                    await slot.session.observe(observation.model_copy(deep=True))
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - Observer extension boundary
                    slot.healthy = False
                    failures.append((slot.name, error))
            if failures:
                await self._notify_failures_now(failures)
                raise self._observation_error(failures)

        await self._enqueue_delivery(broadcast)

    async def _deliver_failure_notifications(
        self,
        failures: Sequence[tuple[str, BaseException]],
    ) -> None:
        async def notify() -> None:
            await self._notify_failures_now(failures)

        await self._enqueue_delivery(notify)

    async def _notify_failures_now(
        self,
        failures: Sequence[tuple[str, BaseException]],
    ) -> None:
        secondary: list[tuple[str, BaseException]] = []
        for failed_name, failure in failures:
            observed_at, monotonic_ns = _stamp()
            notification = RunObserverFailedObservation(
                identity=self.context.identity,
                observer_name=failed_name,
                error_type=_qualified_name(failure),
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
            for slot in self._slots:
                if not slot.healthy:
                    continue
                try:
                    await slot.session.observe(notification.model_copy(deep=True))
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - Observer extension boundary
                    slot.healthy = False
                    secondary.append((slot.name, error))
        if secondary:
            for slot in self._slots:
                if slot.name in {name for name, _error in secondary}:
                    slot.healthy = False

    async def resume_checkpointed(
        self,
        *,
        marker_id: str,
        native_interrupt_ids: frozenset[str],
    ) -> None:
        """Publish durable resume intent and force it before continuation output."""

        observed_at, monotonic_ns = _stamp()
        await self.observe(
            RunResumeCheckpointedObservation(
                identity=self.context.identity,
                marker_id=marker_id,
                native_interrupt_ids=tuple(sorted(native_interrupt_ids)),
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        )
        await self.force(ObservationBoundary.RESUME_CHECKPOINTED)

    async def terminal(
        self,
        outcome: RunTerminalOutcome,
        *,
        code: str | None = None,
        error: BaseException | None = None,
        interrupt_ids: tuple[str, ...] = (),
    ) -> None:
        """Select and force one terminal outcome without rewriting it on retry.

        Terminal broadcast failures make the caller fail closed, but the actual Agent
        outcome remains the one selected before Observation delivery.

        Args:
            outcome: First authoritative Runtime terminal classification.
            code: Optional stable client-safe terminal code.
            error: Optional trusted failure used only for its qualified type.
            interrupt_ids: Stable root interrupt IDs for an interrupted outcome.

        Raises:
            asyncio.CancelledError: The caller or Observer cancels terminal delivery.
            RunObservationError: Terminal delivery or force fails for an Observer.
        """

        if self._terminal is not None:
            return
        if (
            outcome == "failed"
            and error is not None
            and error is self._initialization_error
        ):
            # Preserve a proven pre-Graph failure without relabeling later observer
            # or cleanup failures, or changing cancellation into initialization failure.
            code = "runtime_initialization_error"
        self._terminal = outcome
        await self._settle_thread_callbacks()
        await self._call_handler.settle(outcome, error=error)
        for operation in tuple(self.compactions.values()):
            if error is not None:
                await operation.fail(error)
            else:
                await operation.abandon()
        self.compactions.clear()
        observed_at, monotonic_ns = _stamp()
        await self.observe(
            RunTerminalObservation(
                identity=self.context.identity,
                outcome=outcome,
                code=code,
                error_type=None if error is None else _qualified_name(error),
                interrupt_ids=interrupt_ids,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        )
        await self.force(
            ObservationBoundary.INTERRUPT
            if outcome == "interrupted"
            else ObservationBoundary.TERMINAL
        )

    async def force(self, boundary: ObservationBoundary) -> None:
        """Force every healthy session at a hard replay or terminal boundary."""

        if self._closed:
            raise RuntimeError("Runtime Observation Hub is closed")
        await self._deliver_force(boundary)

    async def _deliver_force(self, boundary: ObservationBoundary) -> None:
        async def force_sessions() -> None:
            failures: list[tuple[str, BaseException]] = []
            for slot in self._slots:
                if not slot.healthy:
                    continue
                try:
                    await slot.session.force(boundary)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - Observer extension boundary
                    slot.healthy = False
                    failures.append((slot.name, error))
            if failures:
                await self._notify_failures_now(failures)
                raise self._observation_error(failures)

        await self._enqueue_delivery(force_sessions)

    async def close(self) -> None:
        """Settle every Observer session while preserving caller cancellation."""

        if self._closed:
            return
        await self._close_once()

    async def _close_once(self) -> None:
        """Own the cancellation-safe cleanup body shared by close callers."""

        self._closed = True
        failures: list[tuple[str, BaseException]] = []
        process_control: BaseException | None = None

        def retain_process_control(error: BaseException, *, source: str) -> None:
            nonlocal process_control
            if process_control is None:
                process_control = error
                return
            # Registration order cannot let cancellation mask process control.
            process_control = select_failure(
                process_control,
                error,
                label=f"Runtime Observer cleanup also failed in {source}",
            )

        try:
            await self._settle_thread_callbacks()
        except asyncio.CancelledError as error:
            retain_process_control(error, source="thread callback settlement")

        if self._started and self._terminal is not None:
            observed_at, monotonic_ns = _stamp()
            try:
                await self._deliver_observation(
                    RunClosedObservation(
                        identity=self.context.identity,
                        outcome=self._terminal,
                        observed_at=observed_at,
                        monotonic_ns=monotonic_ns,
                    )
                )
                await self._deliver_force(ObservationBoundary.CLOSE)
            except asyncio.CancelledError as error:
                retain_process_control(error, source="runtime.close_observation")
            except Exception as error:  # noqa: BLE001 - Observer extension boundary
                failures.append(("runtime.close_observation", error))
            except BaseException as error:  # noqa: BLE001 - preserve process control
                retain_process_control(error, source="runtime.close_observation")
        try:
            await self._stop_delivery_worker()
        except asyncio.CancelledError as error:
            retain_process_control(error, source="observer delivery settlement")
        except Exception as error:  # noqa: BLE001 - Observer extension boundary
            failures.append(("observer delivery settlement", error))
        except BaseException as error:  # noqa: BLE001 - preserve process control
            retain_process_control(error, source="observer delivery settlement")
        for slot in reversed(self._slots):
            waiter = slot.waiter
            if waiter is not None and not waiter.done():
                waiter.cancel()
            try:
                with callback_scope():
                    await slot.session.aclose()
            except asyncio.CancelledError as error:
                retain_process_control(error, source=slot.name)
            except Exception as error:  # noqa: BLE001 - Observer extension boundary
                failures.append((slot.name, error))
            except BaseException as error:  # noqa: BLE001 - preserve process control
                retain_process_control(error, source=slot.name)
        if self._slots:
            try:
                await asyncio.gather(
                    *(slot.waiter for slot in self._slots if slot.waiter is not None),
                    return_exceptions=True,
                )
            except asyncio.CancelledError as error:
                retain_process_control(error, source="failure_waiter settlement")
        failure = self._failure
        if failure is not None and not failure.done():
            failure.cancel()
        if process_control is not None:
            for name, error in failures:
                retain_failure(
                    process_control,
                    error,
                    label=f"Runtime Observer cleanup also failed in {name}",
                )
            raise process_control
        if failures:
            raise self._observation_error(failures)

    async def _stop_delivery_worker(self) -> None:
        """Drain and join the owned delivery worker exactly once."""

        worker = self._delivery_worker
        queue = self._delivery_queue
        if worker is None or queue is None:
            return
        stop = self._delivery_stop
        if stop is None:

            async def stop_worker() -> None:
                if not worker.done():
                    await self._put_delivery(queue, worker, None)
                await join_task(worker)

            stop = asyncio.create_task(
                stop_worker(),
                name="tinkerfin-observer-delivery-close",
            )
            self._delivery_stop = stop
        try:
            await join_task(stop)
        finally:
            if stop.done() and not stop.cancelled() and stop.exception() is None:
                self._delivery_worker = None
                self._delivery_queue = None

    @staticmethod
    def _observation_error(
        failures: Sequence[tuple[str, BaseException]],
    ) -> RunObservationError:
        causes = [error for _name, error in failures]
        cause: BaseException = (
            causes[0]
            if len(causes) == 1
            else BaseExceptionGroup("Runtime observers failed", causes)
        )
        return RunObservationError(
            observer_names=tuple(name for name, _error in failures),
            cause=cause,
        )


def observer_tuple(observers: Sequence[RuntimeObserver]) -> tuple[RuntimeObserver, ...]:
    """Validate and freeze ordered Observer registration without aliases."""

    frozen = tuple(observers)
    seen: set[int] = set()
    for observer in frozen:
        if not isinstance(observer, RuntimeObserver):
            raise TypeError("observer must implement RuntimeObserver")
        identity = id(observer)
        if identity in seen:
            raise ValueError(
                "the same RuntimeObserver instance cannot be registered twice"
            )
        seen.add(identity)
    return frozen


def _consume_delivery_exception(future: asyncio.Future[None]) -> None:
    """Retrieve a delivery failure after its original caller was cancelled."""

    if not future.cancelled():
        future.exception()


__all__ = [
    "RuntimeObservationHub",
    "native_observation",
    "observer_tuple",
    "source_context",
]
