"""Native object-stream coordination, observation, cleanup, and SSE mapping."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, TypeVar, cast

from tinkerfin_contracts import RunTerminalOutcome
from tinkerfin_native_stream import (
    NativeExtraStreamPart,
    NativeStreamContractError,
    NativeUpdatesStreamPart,
    NativeValuesStreamPart,
)
from tinkerfin_native_stream.public import public_extra_data

from ._failure_evidence import retain_failure, select_failure
from ._run_callbacks import callback_scope
from ._tasks import join_task
from .errors import (
    RunCoordinationError,
    RunCoordinationOwnershipLostError,
    RunCoordinationTimeoutError,
    RunCoordinationUnavailableError,
    RunObservationError,
    TinkerFinError,
    TinkerFinErrorCode,
    TinkerFinLifecycleError,
    TinkerFinStreamProtocolError,
)
from .native import NativeStreamPart
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    encode_sse_payload,
)

if TYPE_CHECKING:
    from ._lazy_run import NativeRunStream
    from .runtime import _GraphRunStream

PartT = TypeVar("PartT")

__all__ = ["_finish", "_finish_once", "_observe", "_start", "ready"]


class _StreamDeadlineExceeded(TimeoutError):
    """Identify expiration of the Runtime-owned total execution deadline."""


async def _before_deadline(
    operation: Callable[[], Awaitable[PartT]], deadline: float | None
) -> PartT:
    """Resolve deadline cancellation before the native owner settles its outcome.

    Running the budget inside the existing native Task preserves workspace context
    and lets terminal Observation distinguish timeout from caller cancellation.
    Cleanup starts after this context exits and retains its independent lifetime.
    """

    if deadline is None:
        return await operation()
    if asyncio.get_running_loop().time() >= deadline:
        raise _StreamDeadlineExceeded("AG-UI stream timed out")
    timeout_context = asyncio.timeout_at(deadline)
    try:
        async with timeout_context:
            return await operation()
    except TimeoutError as error:
        if timeout_context.expired():
            raise _StreamDeadlineExceeded("AG-UI stream timed out") from error
        raise


def _native_contract_error(
    error: NativeStreamContractError,
) -> TinkerFinStreamProtocolError:
    """Translate the shared Native contract failure at the Runtime boundary."""

    return TinkerFinStreamProtocolError(
        "Native stream part violates the current contract",
        context=error.context,
        diagnostic_context={"native_code": error.code.value},
        cause=error,
    )


def _coordination_error(operation: str, error: Exception) -> RunCoordinationError:
    """Translate only failures owned by a replaceable coordinator boundary."""

    if isinstance(error, RunCoordinationError):
        return error
    diagnostic_context = (
        dict(error.diagnostic_context) if isinstance(error, TinkerFinError) else {}
    )
    diagnostic_context["operation"] = operation
    if isinstance(error, TinkerFinError):
        if error.code is TinkerFinErrorCode.REDIS_LEASE_LOST:
            return RunCoordinationOwnershipLostError(
                "Run coordination ownership was lost",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
        if error.code is TinkerFinErrorCode.REDIS_LEASE_TIMEOUT:
            return RunCoordinationTimeoutError(
                "Run coordination timed out",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
        if error.code is TinkerFinErrorCode.REDIS_LEASE_UNAVAILABLE:
            return RunCoordinationUnavailableError(
                "Run coordination is unavailable",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
    if isinstance(error, TimeoutError):
        return RunCoordinationTimeoutError(
            "Run coordination timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return RunCoordinationError(
        "Run coordination failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


def _run_error(self: _GraphRunStream[PartT], error: BaseException) -> BaseException:
    resources = self._run_resources
    if resources is None:
        return error
    resolved = resources.owner.failure_for(error)
    if self.error is None:
        # Cancellation stays primary, while callers can inspect the controller
        # failure that stopped the Run before transport cleanup began.
        self.error = resources.owner.error
    return resolved


def _validate_timeout(
    timeout: float | None,
    *,
    name: str = "timeout",
) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError(f"{name} must be a number or None")
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


async def _map_sse_item(
    item: PartT,
    *,
    mapper: SseMapper[PartT] | None,
    default: SsePayload,
) -> SsePayload | None:
    if mapper is None:
        return default
    mapped = mapper(item)
    if not inspect.isawaitable(mapped):
        raise TypeError("SSE mapper must return an awaitable")
    payload = await mapped
    if payload is not None and not isinstance(payload, SsePayload):
        raise TypeError("SSE mapper must resolve to SsePayload or None")
    return payload


async def _resolve_sse_event_id(
    item: PartT,
    resolver: SseEventIdResolver[PartT] | None,
) -> str | int | None:
    if resolver is None:
        return None
    resolved = resolver(item)
    if not inspect.isawaitable(resolved):
        raise TypeError("event_id_resolver must return an awaitable")
    return await resolved


async def __anext__(
    self: _GraphRunStream[PartT], *, deadline: float | None = None
) -> PartT:
    """Own one pull, publish its canonical frame, and preserve terminal ordering.

    Lazy startup and Observer failure racing happen inside the same active-operation
    slot. Natural exhaustion alone selects success or interrupt; every exception and
    cancellation settles the shared Runtime lifecycle before it propagates.
    """

    if self._ready_error is not None and not self._ready_error_delivered:
        self._ready_error_delivered = True
        raise self._ready_error.with_traceback(self._ready_error.__traceback__)
    if self._closed:
        raise StopAsyncIteration
    current = cast(asyncio.Task[object] | None, asyncio.current_task())
    if current is None:  # pragma: no cover - async methods run in a Task
        raise TinkerFinLifecycleError("a Graph run stream requires an asyncio task")
    active = self._active_task
    if active is not None and not active.done():
        raise TinkerFinLifecycleError("a Graph run stream operation is already active")
    self._active_task = current
    try:
        if not self._started:
            await self._start()
        source = self._source
        assert source is not None

        async def pull() -> PartT:
            with self._owned_operation_failures.capture():
                while True:
                    part = await _next_or_observer_failure(self, source)
                    await self._observe(part)
                    if self._native_frame is None or not self._native_frame[1].internal:
                        return part

        try:
            part = await _before_deadline(pull, deadline)
        except StopAsyncIteration:
            outcome: RunTerminalOutcome
            if self._observation.context.input_kind == "abandon":
                outcome = "abandoned"
            elif self._root_interrupt_ids:
                outcome = "interrupted"
            else:
                outcome = "succeeded"
            await self._finish(None, outcome=outcome)
            raise
        except BaseException as error:
            error = _run_error(self, error)
            if isinstance(error, Exception):
                self.error = error
            await self._finish(error, outcome=_error_outcome(error))
            raise error
        return (
            cast(PartT, self._native_frame[0])
            if self._native_frame is not None
            else part
        )
    finally:
        if self._active_task is current:
            self._active_task = None


async def aclose(self: _GraphRunStream[PartT]) -> None:
    """Close the native source and release coordination idempotently.

    Closure requested from a part-observer call chain preserves delivery of the
    part currently being observed. An external closer cancels an active pull.
    """

    self._ready_error_delivered = True
    current = asyncio.current_task()
    active = self._active_task
    observer_lineage_active = (
        bool(self._active_observers) and self._observer_lineage.get()
    )
    if (
        active is not None
        and active is not current
        and not active.done()
        and not observer_lineage_active
    ):
        await join_task(active, cancel=True, suppress_task_cancellation=True)
    await self._finish(
        None,
        outcome="cancelled" if self._started else None,
    )


async def _next_or_observer_failure(
    self: _GraphRunStream[PartT],
    source: AsyncIterator[PartT],
) -> PartT:
    """Race one upstream pull with the managed Observer failure signal."""

    if self._run_resources is not None:
        from ._call_observation import bind_observation_hub, reset_observation_hub

        token = bind_observation_hub(self._observation)
        try:
            return await anext(source)
        finally:
            reset_observation_hub(token)
    if not self._observation.enabled:
        return await anext(source)

    async def pull_next() -> PartT:
        from ._call_observation import bind_observation_hub, reset_observation_hub

        token = bind_observation_hub(self._observation)
        try:
            return await anext(source)
        finally:
            reset_observation_hub(token)

    pull = asyncio.create_task(pull_next(), name="tinkerfin-graph-run-pull")
    failure = asyncio.create_task(
        self._observation.wait_failure(),
        name="tinkerfin-observer-failure-wait",
    )
    primary: BaseException | None = None
    try:
        done, _pending = await asyncio.wait(
            (pull, failure),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if failure in done:
            await failure
            raise AssertionError("Observer failure waiter returned without failing")
        return await pull
    except BaseException as error:
        primary = error
        raise
    finally:
        # Cancel each owned task once and let its cleanup settle. A second cancel
        # can interrupt LangGraph 1.2.10's AsyncPregelLoop.__aexit__ before its
        # provider tasks close. join_task also keeps repeated caller cancellation
        # from propagating into that cleanup while preserving the caller's signal.
        settlement_error = primary
        for owned in (failure, pull):
            try:
                await join_task(owned, cancel=True, suppress_task_cancellation=True)
            except BaseException as error:  # noqa: BLE001 - settle both owners before propagating every failure
                settlement_error = (
                    error
                    if settlement_error is None
                    else select_failure(settlement_error, error)
                )
        # Process control stays observable even when an Observer or caller failed
        # first; cancellation still outranks ordinary provider/cleanup failures.
        if settlement_error is not None and settlement_error is not primary:
            raise settlement_error


def _error_outcome(error: BaseException) -> RunTerminalOutcome:
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "failed"


def to_sse(
    self: _GraphRunStream[PartT] | NativeRunStream,
    *,
    timeout: float | None = None,
    mapper: SseMapper[NativeStreamPart] | None = None,
    event_id_resolver: SseEventIdResolver[NativeStreamPart] | None = None,
) -> SseBody[bytes]:
    """Consume this native object stream as UTF-8 SSE bytes."""

    total_timeout = _validate_timeout(timeout)

    async def frames() -> AsyncGenerator[bytes, None]:
        iterator = aiter(self)
        deadline: float | None = None
        if total_timeout is not None:
            loop = asyncio.get_running_loop()
            try:
                deadline = loop.time() + total_timeout
            except OverflowError:
                deadline = math.inf
        try:
            while True:
                try:
                    if deadline is None:
                        raw_part = await anext(iterator)
                        part = self._take_frame(raw_part).replay
                        payload = await _map_sse_item(
                            part,
                            mapper=mapper,
                            default=SsePayload(
                                data=part.model_dump_json(by_alias=True),
                                event="stream-part",
                            ),
                        )
                        if payload is None:
                            continue
                        event_id = await _resolve_sse_event_id(
                            part,
                            event_id_resolver,
                        )
                        frame = encode_sse_payload(payload, event_id=event_id)
                    else:
                        async with asyncio.timeout_at(deadline):
                            raw_part = await anext(iterator)
                            part = self._take_frame(raw_part).replay
                            payload = await _map_sse_item(
                                part,
                                mapper=mapper,
                                default=SsePayload(
                                    data=part.model_dump_json(by_alias=True),
                                    event="stream-part",
                                ),
                            )
                            if payload is None:
                                continue
                            event_id = await _resolve_sse_event_id(
                                part,
                                event_id_resolver,
                            )
                            frame = encode_sse_payload(
                                payload,
                                event_id=event_id,
                            )
                except StopAsyncIteration:
                    return
                except TimeoutError as error:
                    raise TimeoutError("native SSE stream timed out") from error
                yield frame
        finally:
            await self.aclose()

    return SseBody(source_factory=frames, close=self.aclose)


async def _observe(self: _GraphRunStream[PartT], part: PartT) -> None:
    """Normalize once, retain the frame sidecar, then notify public consumers.

    The order is Driver validation, Runtime Observation, and finally ``on_part``.
    Keeping the sidecar before callbacks makes the exact same canonical frame available
    to one downstream Adapter, SSE, or Messaging consumer without parsing the raw part.
    """

    try:
        frame = self._stream_driver.normalize(
            part,
            context=self._observation.context,
        )
        origin = self._observation.graph_scopes.accept(frame.canonical)
        declarations = self._observation.graph_scopes.declarations(frame.canonical)
        public = self._observation.graph_scopes.public_part(frame.canonical)
        if public is not None and public is not frame.canonical:
            if not isinstance(part, Mapping):
                raise TypeError(
                    "private Native records require a mapping stream envelope"
                )
            part = cast(PartT, {**part, "data": public.data})
            if not isinstance(public, (NativeExtraStreamPart, NativeUpdatesStreamPart)):
                raise TypeError("Native privacy filtering changed an unsupported mode")
            frame = replace(
                frame,
                canonical=public,
                replay=frame.replay.model_copy(
                    update={"data": public_extra_data(public)}
                ),
            )
        observations = tuple(
            value.model_copy(
                update={
                    "graph_origin": origin,
                    **(
                        {"subagent_requests": declarations}
                        if value.kind == "native.task"
                        else {}
                    ),
                }
            )
            for value in frame.observations
        )
        frame = replace(
            frame,
            origin=origin,
            subagent_requests=declarations,
            observations=observations,
            internal=public is None,
            replay=frame.replay.model_copy(
                update={
                    "graph_origin": origin,
                    "subagent_requests": declarations,
                }
            ),
        )
    except NativeStreamContractError as error:
        translated = _native_contract_error(error)
        raise translated from error
    except (TypeError, ValueError) as error:
        raise TinkerFinStreamProtocolError(
            "Native graph provenance violates the current contract", cause=error
        ) from error
    # The sidecar is the only downstream normalization authority for this raw part.
    # AG-UI, native SSE, and Messaging must consume it rather than parse v2 again.
    self._native_frame = (part, frame)
    if frame.internal:
        return
    # Each root values part reports the interrupts for one task, not all pending
    # tasks. Accumulate this invocation's IDs as LangGraph 1.2.10 Pregel.ainvoke
    # does, including when later task/value parts contain no interrupts.
    if isinstance(frame.canonical, NativeValuesStreamPart) and not frame.canonical.ns:
        self._root_interrupt_ids = tuple(
            dict.fromkeys((*self._root_interrupt_ids, *frame.root_interrupt_ids))
        )
    if self._observation.enabled:
        for observation in frame.observations:
            await self._observation.observe(observation)
    observer = self._on_part
    if observer is None:
        return
    self._active_observers += 1
    token = self._observer_lineage.set(True)
    try:
        with callback_scope():
            observed = observer(part)
            if not inspect.isawaitable(observed):
                raise TypeError("on_part must return an awaitable")
            await observed
    finally:
        self._observer_lineage.reset(token)
        self._active_observers -= 1


async def _start(self: _GraphRunStream[PartT]) -> None:
    if self._started:
        return
    coordination_factory = self._coordination_factory
    if coordination_factory is not None:
        try:
            coordination = coordination_factory()
            await coordination.__aenter__()
        except Exception as error:
            translated = _coordination_error("enter", error)
            raise translated from error
        self._coordination = coordination
    self._started = True
    try:
        await self._observation.start()
        preflight = self._source_preflight
        if preflight is not None:
            result = preflight()
            if not inspect.isawaitable(result):
                raise TypeError("source_preflight must return an awaitable")
            await result
        source = self._source_factory()
        if not isinstance(source, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        self._source = source
    except BaseException as error:
        error = _run_error(self, error)
        if isinstance(error, Exception):
            self.error = error
        await self._finish(error, outcome=_error_outcome(error))
        raise error


async def ready(self: _GraphRunStream[PartT]) -> None:
    """Establish the observable Run boundary while retaining ordinary setup errors."""

    if self._started or self._closed:
        return
    try:
        await self._start()
    except Exception as error:  # noqa: BLE001 - first consumer receives setup failure
        self._ready_error = error


async def _finish(
    self: _GraphRunStream[PartT],
    error: BaseException | None,
    *,
    outcome: RunTerminalOutcome | None = None,
) -> None:
    resources = self._run_resources
    if resources is not None:
        if not resources.owner.is_current():
            await resources.owner.aclose()
            return
        if not self._owned_finish_done:
            self._closed = True
            self._owned_finish_done = True
            await resources.owner.begin_settlement()
            try:
                await self._finish_once(error, outcome)
            except BaseException as cleanup_error:  # noqa: BLE001 - repeat close reports the same settled failure
                self._owned_finish_error = cleanup_error
                if self.error is None and isinstance(cleanup_error, Exception):
                    self.error = cleanup_error
        if self._owned_finish_error is not None:
            raise self._owned_finish_error
        return
    task = self._finish_task
    if task is None:
        self._closed = True
        with self._owned_operation_failures.capture():
            task = asyncio.create_task(
                self._finish_once(error, outcome),
                name="tinkerfin-graph-run-stream-close",
            )
        self._finish_task = task
    try:
        await join_task(task)
    except Exception as cleanup_error:
        if self.error is None:
            self.error = cleanup_error
        raise


async def _finish_once(
    self: _GraphRunStream[PartT],
    error: BaseException | None,
    outcome: RunTerminalOutcome | None,
) -> None:
    """Settle upstream ownership and publish one final Observation lifecycle.

    Upstream, workspace, and coordinator resources close before terminal Observation.
    Cleanup can downgrade success to failure. Process control takes priority over
    cancellation, and cancellation over ordinary errors; other failures retain their
    notes and original causes. Observer failures do not abandon other sessions.
    """

    source = self._source
    self._source = None
    coordination = self._coordination
    self._coordination = None
    cleanup_errors: list[BaseException] = []
    try:
        if source is not None:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
    except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
        cleanup_errors.append(cleanup_error)
    if coordination is not None:
        try:
            await coordination.__aexit__(
                None if error is None else type(error),
                error,
                None if error is None else error.__traceback__,
            )
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
            if isinstance(cleanup_error, Exception):
                cleanup_errors.append(_coordination_error("exit", cleanup_error))
            else:
                cleanup_errors.append(cleanup_error)

    # A request-specific settlement hook participates in the same retained close task.
    # It runs after upstream ownership is released and at most once, including when a
    # stream is closed before its first pull.
    on_settle = self._on_settle
    self._on_settle = None
    if on_settle is not None:
        try:
            await on_settle()
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
            cleanup_errors.append(cleanup_error)

    resources = self._run_resources
    if resources is not None:
        try:
            await resources.aclose(
                error or (cleanup_errors[0] if cleanup_errors else None)
            )
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
            cleanup_errors.append(cleanup_error)

    # The source has joined its children. Preserve protected operation failures
    # even when the upstream runner replaced their exception with cancellation.
    cleanup_errors.extend(
        failure
        for failure in self._owned_operation_failures.take()
        if failure is not error and all(failure is not item for item in cleanup_errors)
    )

    effective_outcome = outcome
    if self._started and effective_outcome is None:
        effective_outcome = "failed" if error is not None else "cancelled"
    if (
        cleanup_errors
        and effective_outcome in {"succeeded", "interrupted"}
        and error is None
    ):
        effective_outcome = "failed"

    observation_errors: list[BaseException] = []
    if self._started and effective_outcome is not None:
        terminal_error = error or (cleanup_errors[0] if cleanup_errors else None)
        code = _terminal_code(effective_outcome, terminal_error)
        try:
            await self._observation.terminal(
                effective_outcome,
                code=code,
                error=terminal_error,
                interrupt_ids=self._root_interrupt_ids,
            )
        except BaseException as observation_error:  # noqa: BLE001 - cleanup continues
            observation_errors.append(observation_error)
        try:
            await self._observation.close()
        except BaseException as observation_error:  # noqa: BLE001 - report below
            observation_errors.append(observation_error)

    all_secondary = [*cleanup_errors, *observation_errors]
    candidates = ([error] if error is not None else []) + all_secondary
    control = next(
        (
            item
            for item in candidates
            if not isinstance(item, Exception | asyncio.CancelledError)
        ),
        None,
    )
    if control is None:
        control = next(
            (item for item in candidates if isinstance(item, asyncio.CancelledError)),
            None,
        )
    if control is not None and control is not error:
        # A prior execution failure cannot hide cancellation or process control
        # raised while closing a coordinator, source, or Observer session.
        for secondary in candidates:
            if secondary is not control:
                retain_failure(
                    control, secondary, label="Graph run settlement also observed"
                )
        raise control
    if error is not None:
        for secondary in all_secondary:
            retain_failure(error, secondary, label="Graph run settlement also failed")
        return
    if len(all_secondary) == 1:
        raise all_secondary[0]
    if all_secondary:
        raise BaseExceptionGroup("Graph run settlement failed", all_secondary)


def _terminal_code(
    outcome: RunTerminalOutcome,
    error: BaseException | None,
) -> str | None:
    if outcome == "failed":
        return (
            "stream_timeout"
            if isinstance(error, _StreamDeadlineExceeded)
            else "observer_failed"
            if isinstance(error, RunObservationError)
            else "runtime_error"
        )
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "abandoned":
        return "abandoned"
    return None
