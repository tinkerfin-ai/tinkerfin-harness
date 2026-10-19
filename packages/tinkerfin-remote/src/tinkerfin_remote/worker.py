"""Own concrete remote operations independently from socket delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import ClassVar, Generic, Self, TypeVar

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import (
    PreparedWorkspace,
    RunIdentity,
    ToolExecutionScope,
    bind_workspace_operation,
)
from tinkerfin_messaging import (
    MessageChannel,
    Messaging,
    RunNotFound,
    RunProducerFailed,
)
from tinkerfin_messaging.sqlalchemy import SqlAlchemyBackend

from . import _transport
from ._operations import invoke, result_transfer
from ._store import Ledger
from ._tasks import raise_cleanup_errors, settle
from ._wire import bounded, decode, digest, encode, key, receipt
from .errors import RemoteError, RemoteErrorCode
from .executor import ToolExecutor
from .limits import RemoteLimits
from .models import (
    TERMINAL_EVENTS,
    BinaryChunk,
    CloseWorkspace,
    CommandEnvelope,
    CoordinatorFrame,
    Dispatch,
    OpenWorkspace,
    OperationEvent,
    WorkerFrame,
    WorkspaceDescription,
    WorkspaceEvent,
    WorkspaceRequest,
)

WorkspaceT = TypeVar("WorkspaceT")
_logger = logging.getLogger("tinkerfin.remote.worker")


class _OwnershipCodec:
    codec_id: ClassVar[str] = "tinkerfin-remote-execution"

    def encode(self, item: str) -> bytes:
        if item not in {"owned", "settled"}:
            raise ValueError("invalid operation ownership marker")
        return item.encode("ascii")

    def decode(self, payload: bytes) -> str:
        value = payload.decode("ascii")
        self.encode(value)
        return value


class _Borrow(Generic[WorkspaceT]):
    def __init__(
        self, worker: ToolWorker[WorkspaceT], request: WorkspaceRequest
    ) -> None:
        self.worker = worker
        self.request = request
        self.ready: asyncio.Future[PreparedWorkspace[WorkspaceT, BackendProtocol]] = (
            asyncio.get_running_loop().create_future()
        )
        self.ready.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        self.close_requested = asyncio.Event()
        self.task = asyncio.create_task(self._own())
        self.task.add_done_callback(lambda _done: worker._wake.set())

    async def _own(self) -> None:
        worker, request = self.worker, self.request
        command_id = digest(OpenWorkspace(request=request))
        try:
            async with worker._open_workspace(request) as executor:
                async with executor._prepare(request) as prepared:
                    root = (
                        prepared.backend.artifacts_root
                        if isinstance(prepared.backend, CompositeBackend)
                        else "/.tinkerfin/artifacts/"
                        + key(request.identity.thread.model_dump(mode="json"))
                    )
                    description = WorkspaceDescription(
                        filesystem_instructions=prepared.filesystem_instructions,
                        tool_descriptions=dict(prepared.tool_descriptions),
                        tools=tuple(tool.descriptor for tool in prepared.tools),
                        skill_sources=prepared.skill_sources,
                        artifacts_root=root,
                    )
                    if len(encode(description).encode()) > worker._limits.request_bytes:
                        raise RemoteError(
                            RemoteErrorCode.CAPACITY,
                            "Workspace catalog exceeds its byte limit",
                        )
                    await worker._ledger.workspace_event(
                        WorkspaceEvent(
                            command_id=command_id,
                            session_id=request.session_id,
                            description=description,
                        )
                    )
                    self.ready.set_result(prepared)
                    worker._changed.set()
                    await self.close_requested.wait()
        except asyncio.CancelledError:
            if not self.ready.done():
                self.ready.cancel()
            raise
        except Exception as error:
            if not self.ready.done():
                failure = (
                    error
                    if isinstance(error, RemoteError)
                    else RemoteError(
                        RemoteErrorCode.UNAVAILABLE,
                        "Remote workspace could not be authorized or prepared",
                        cause=error,
                    )
                )
                self.ready.set_exception(failure)
                _logger.error(
                    "Remote workspace preparation failed",
                    extra={"tinkerfin_error_code": RemoteErrorCode.UNAVAILABLE.value},
                )
                if await worker._ledger.get_workspace_reply(command_id) is None:
                    await worker._ledger.workspace_event(
                        WorkspaceEvent(
                            command_id=command_id,
                            session_id=request.session_id,
                            error="Remote workspace could not be authorized or prepared",
                        )
                    )
                worker._changed.set()
            elif isinstance(error, RemoteError):
                raise
            else:
                raise RemoteError(
                    RemoteErrorCode.CLEANUP,
                    "Remote workspace cleanup failed",
                    cause=error,
                ) from error


class _Execution(Generic[WorkspaceT]):
    def __init__(self, worker: ToolWorker[WorkspaceT], dispatch: Dispatch) -> None:
        self.worker = worker
        self.dispatch = dispatch
        self.task: asyncio.Task[None] | None = None
        self.cancelled = False

    async def cancel(self) -> None:
        self.cancelled = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def source(self) -> AsyncGenerator[str]:
        # Messaging owns this source exclusively across processes. Never rebuild
        # an effect after it was admitted: missing completion means uncertainty.
        interrupted: BaseException | None = None
        try:
            yield "owned"
            self.task = asyncio.create_task(self._run())
            if self.cancelled:
                self.task.cancel()
            await self.task
            yield "settled"
        except BaseException as error:
            interrupted = error
            raise
        finally:
            try:
                await settle(self._finish())
            except Exception as cleanup_error:
                if interrupted is not None and not isinstance(interrupted, Exception):
                    raise interrupted from cleanup_error
                raise

    async def _finish(self) -> None:
        await self.cancel()
        _request, state, _cancelled = await self.worker._ledger.get_operation(
            self.dispatch.request.operation_id
        )
        if state == "pending":
            await self.worker._terminal(
                self.dispatch.request.operation_id,
                "cancelled",
                "Operation stopped before execution",
            )

    async def _run(self) -> None:
        worker, dispatch = self.worker, self.dispatch
        request = dispatch.request
        identifier = request.operation_id
        try:
            _stored, state, cancelled = await worker._ledger.get_operation(identifier)
            if state in TERMINAL_EVENTS:
                return
            if state != "pending":
                await worker._terminal(
                    identifier,
                    "uncertain",
                    "Previous execution has no confirmed outcome; it was not repeated",
                )
                return
            if cancelled or self.cancelled:
                await worker._terminal(
                    identifier, "cancelled", "Operation was cancelled before execution"
                )
                return
            session, closed = await worker._ledger.session(dispatch.session_id)
            if closed:
                await worker._terminal(
                    identifier, "cancelled", "Workspace closed before execution"
                )
                return
            prepared = await worker._borrow(session)
            uploads = await worker._ledger.upload_content(identifier)
            if uploads is None:
                raise RemoteError(
                    RemoteErrorCode.PROTOCOL, "Operation upload is incomplete"
                )
            _stored, _state, cancelled = await worker._ledger.get_operation(identifier)
            _session, closed = await worker._ledger.session(dispatch.session_id)
            if cancelled or closed or self.cancelled:
                await worker._terminal(
                    identifier, "cancelled", "Operation was cancelled before execution"
                )
                return
            await worker._ledger.append(identifier, kind="started")
            worker._changed.set()

            async def output(value: JsonValue) -> None:
                await worker._ledger.append(identifier, kind="output", output=value)
                worker._changed.set()

            async def binary(chunk: BinaryChunk) -> None:
                await worker._ledger.append(identifier, kind="binary", chunk=chunk)
                worker._changed.set()

            scope = ToolExecutionScope(
                identity=session.identity,
                origin_identity=request.origin,
                workspace_scope_id=request.workspace_scope_id,
                invocation_id=request.invocation_id,
                graph_namespace=request.graph_namespace,
                purpose=request.purpose,
                tool_call_id=request.tool_call_id,
                steps=request.steps,
            )
            with bind_workspace_operation(scope, emit_output=output):
                result = await invoke(
                    prepared,
                    request,
                    uploads=uploads,
                    limits=worker._limits,
                    emit_output=output,
                    emit_binary=binary,
                )
            transfer = await result_transfer(
                identifier, result, limits=worker._limits, emit=binary
            )
            await worker._ledger.append(
                identifier,
                kind="completed",
                result=result if transfer is None else None,
                result_transfer=transfer,
            )
            worker._changed.set()
        except asyncio.CancelledError as error:
            try:
                await settle(
                    worker._terminal(
                        identifier,
                        "cancelled",
                        "Execution stopped; completed side effects are not undone",
                    )
                )
            except Exception as cleanup_error:
                raise error from cleanup_error
            raise
        except Exception as error:  # noqa: BLE001 -- Worker owns the terminal result.
            if not isinstance(error, RemoteError):
                _logger.error(
                    "Remote operation failed",
                    extra={
                        "tinkerfin_operation_id": identifier,
                        "tinkerfin_error_code": RemoteErrorCode.REJECTED.value,
                    },
                )
            await worker._terminal(
                identifier,
                "failed",
                "Remote operation failed; completed side effects are not undone",
            )


class ToolWorker(Generic[WorkspaceT]):
    """Own authorized tools and durable execution while transports reconnect.

    Args:
        engine: Borrowed SQL engine with host-configured driver and database
            deadlines. Cancellation waits for accepted SQL to settle.
        namespace: Authenticated owner bound by the host.
        target: Authenticated device or endpoint identity bound by the host.
        open_workspace: Interpret opaque authorization and yield a ToolExecutor.
            The host owns its workspace manager; this worker owns tool preparation.
        limits: Fixed endpoint storage and bounded execution capacities.

    serve owns only delivery. wait_idle joins accepted operations. Exiting this
    application context cancels and joins owned work before returning workspace
    managers; cancellation does not undo effects already performed by tools.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        namespace: str,
        target: str,
        open_workspace: Callable[
            [WorkspaceRequest], AbstractAsyncContextManager[ToolExecutor[WorkspaceT]]
        ],
        limits: RemoteLimits = RemoteLimits(),
    ) -> None:
        """Capture authenticated host resources without opening them."""
        self._limits = limits
        self._ledger = Ledger(
            engine, role="worker", namespace=namespace, target=target, limits=limits
        )
        self._messaging = Messaging(backend=SqlAlchemyBackend(engine))
        self._channel: MessageChannel[str, str] | None = None
        self._open_workspace = open_workspace
        self._borrows: dict[str, _Borrow[WorkspaceT]] = {}
        self._running: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._cancelling: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._idle_changed = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._opened = False
        self._closed = False
        self._serving = False
        self._delivery: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        """Open execution ownership and recover retained work without replaying effects."""
        if self._opened or self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "ToolWorker cannot be reopened")
        try:
            await self._ledger.prepare()
            if await self._ledger.is_revoked():
                raise RemoteError(
                    RemoteErrorCode.REJECTED,
                    "Worker endpoint authorization was revoked",
                )
            await self._messaging.__aenter__()
        except BaseException as error:
            self._closed = True
            try:
                await self.aclose()
            except Exception as cleanup_error:
                if not isinstance(error, Exception):
                    raise error from cleanup_error
                raise RemoteError(
                    RemoteErrorCode.UNAVAILABLE,
                    "Remote worker could not open or release its resources",
                    cause=ExceptionGroup(
                        "Remote worker startup and cleanup failed",
                        [error, cleanup_error],
                    ),
                )
            if not isinstance(error, Exception) or isinstance(error, RemoteError):
                raise
            raise RemoteError(
                RemoteErrorCode.UNAVAILABLE,
                "Remote worker could not open its execution resources",
                cause=error,
            ) from error
        self._channel = self._messaging.channel(
            name="tinkerfin-remote-" + self._ledger.scope, codec=_OwnershipCodec()
        )
        self._opened = True
        self._supervisor = asyncio.create_task(self._supervise())
        self._supervisor.add_done_callback(lambda _done: self._changed.set())
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Join accepted work before the host releases workspace managers and storage."""
        await settle(self.aclose(), primary_error=error)

    def _require_open(self) -> None:
        if not self._opened or self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "ToolWorker is not open")
        if self._supervisor is not None and self._supervisor.done():
            try:
                self._supervisor.result()
            except RemoteError:
                raise
            except Exception as error:
                raise RemoteError(
                    RemoteErrorCode.UNAVAILABLE,
                    "Remote worker execution supervision failed",
                    cause=error,
                ) from error

    def _identity(self, identifier: str) -> RunIdentity:
        return RunIdentity(
            namespace=self._ledger.namespace,
            thread_id=key(self._ledger.target, identifier),
            run_id=identifier,
        )

    async def _borrow(
        self, request: WorkspaceRequest
    ) -> PreparedWorkspace[WorkspaceT, BackendProtocol]:
        if request.session_id not in self._borrows:
            self._borrows[request.session_id] = _Borrow(self, request)
        ready = self._borrows[request.session_id].ready
        await asyncio.wait((ready,))
        return ready.result()

    async def _terminal(self, identifier: str, kind: str, message: str) -> None:
        await self._ledger.finish(identifier, kind, message)
        self._changed.set()

    async def _execute(self, dispatch: Dispatch) -> None:
        assert self._channel is not None
        identifier = dispatch.request.operation_id
        execution = _Execution(self, dispatch)
        try:
            subscription = await self._channel.wrap(
                execution.source(),
                identity=self._identity(identifier),
                request_digest=digest(dispatch.request),
                after=0,
                cancel=execution.cancel,
            )
            async with subscription:
                async for _message in subscription:
                    pass
        except (RunProducerFailed, RunNotFound):
            await self._terminal(
                identifier,
                "uncertain",
                "Execution ownership ended without a confirmed result; the operation was not repeated",
            )
        else:
            await self._terminal(
                identifier,
                "uncertain",
                "Execution ended without a confirmed result; the operation was not repeated",
            )
        finally:
            self._idle_changed.set()

    async def _cancel(self, identifier: str) -> None:
        assert self._channel is not None
        try:
            await self._channel.cancel(identity=self._identity(identifier))
        except RunNotFound:
            return
        except RunProducerFailed:
            await self._terminal(
                identifier,
                "uncertain",
                "Cancellation could not confirm the operation outcome",
            )

    async def _schedule(self) -> None:
        for request in await self._ledger.open_sessions():
            if (
                request.session_id not in self._borrows
                and await self._ledger.get_workspace_reply(
                    digest(OpenWorkspace(request=request))
                )
                is None
            ):
                self._borrows[request.session_id] = _Borrow(self, request)
        for identifier, (_session, task) in tuple(self._running.items()):
            if task.done():
                del self._running[identifier]
                task.result()
        for identifier, task in tuple(self._cancelling.items()):
            if task.done():
                del self._cancelling[identifier]
                task.result()
        for dispatch in await self._ledger.dispatches():
            identifier = dispatch.request.operation_id
            _request, _state, cancelled = await self._ledger.get_operation(identifier)
            if identifier in self._running:
                if cancelled and identifier not in self._cancelling:
                    task = asyncio.create_task(self._cancel(identifier))
                    task.add_done_callback(lambda _done: self._wake.set())
                    self._cancelling[identifier] = task
                continue
            if len(self._running) >= self._limits.concurrent_operations:
                break
            if not cancelled and not await self._ledger.upload_ready(identifier):
                continue
            task = asyncio.create_task(self._execute(dispatch))
            task.add_done_callback(lambda _done: self._wake.set())
            self._running[identifier] = dispatch.session_id, task
        for identifier, borrow in tuple(self._borrows.items()):
            _request, closed = await self._ledger.session(identifier)
            if closed and not any(
                session == identifier for session, _task in self._running.values()
            ):
                borrow.close_requested.set()
                await borrow.task
                del self._borrows[identifier]
            elif borrow.task.done():
                borrow.task.result()
        for identifier in await self._ledger.closing_sessions():
            if (
                not await self._ledger.session_active(identifier)
                and identifier not in self._borrows
            ):
                await self._ledger.workspace_event(
                    WorkspaceEvent(
                        command_id=digest(CloseWorkspace(session_id=identifier)),
                        session_id=identifier,
                        closed=True,
                    )
                )
                self._changed.set()
        self._idle_changed.set()

    async def _supervise(self) -> None:
        while True:
            self._wake.clear()
            await self._schedule()
            await _transport.changed_or_poll(self._wake)

    async def _incoming(self, payload: str) -> None:
        frame = decode(CoordinatorFrame, payload, self._limits.frame_bytes)
        if (
            frame.namespace != self._ledger.namespace
            or frame.target != self._ledger.target
        ):
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Frame crossed its authenticated endpoint"
            )
        if frame.revoked:
            await self._ledger.revoke()
            self._wake.set()
            self._changed.set()
            return
        await self._ledger.acknowledge("event", frame.events_received)
        await self._ledger.acknowledge("workspace", frame.workspaces_received)
        await self._ledger.acknowledge("command", frame.commands_confirmed)
        await self._ledger.acknowledge("chunk", frame.chunks_confirmed)
        if (
            frame.events_received
            or frame.workspaces_received
            or frame.commands_confirmed
            or frame.chunks_confirmed
        ):
            await self._ledger.reclaim()
        for command in frame.commands:
            await self._ledger.command(command)
        for chunk in frame.chunks:
            await self._ledger.upload_chunk(chunk)
        if frame.commands or frame.chunks:
            self._wake.set()
        if any(
            (
                frame.commands,
                frame.chunks,
                frame.events_received,
                frame.workspaces_received,
                frame.commands_confirmed,
                frame.chunks_confirmed,
            )
        ):
            self._changed.set()

    async def _outgoing(self) -> str:
        self._require_open()
        commands = await self._ledger.pending("command", CommandEnvelope)
        chunks = await self._ledger.pending("chunk", BinaryChunk)
        frame = WorkerFrame(
            namespace=self._ledger.namespace,
            target=self._ledger.target,
            commands_received=tuple(receipt(item) for item in commands),
            chunks_received=tuple(receipt(item) for item in chunks),
        )
        budget = (
            self._limits.frame_bytes
            - len(encode(frame).encode())
            - min(4096, self._limits.frame_bytes // 8)
        )
        workspaces = await self._ledger.pending(
            "workspace", WorkspaceEvent, budget=budget
        )
        budget -= sum(len(encode(item).encode()) + 1 for item in workspaces)
        events = await self._ledger.pending("event", OperationEvent, budget=budget)
        return bounded(
            frame.model_copy(update={"workspaces": workspaces, "events": events}),
            self._limits.frame_bytes,
        )

    async def serve(
        self,
        *,
        send: Callable[[str], Awaitable[None]],
        receive: Callable[[], Awaitable[str]],
    ) -> None:
        """Attach delivery; leaving this method never cancels accepted execution."""
        self._require_open()
        if self._serving:
            raise RemoteError(
                RemoteErrorCode.CONFLICT, "Worker delivery is already attached"
            )
        self._serving = True
        try:
            self._delivery = asyncio.create_task(
                _transport.serve(
                    send=send,
                    receive=receive,
                    outgoing=self._outgoing,
                    incoming=self._incoming,
                    changed=self._changed,
                )
            )
            await self._delivery
        finally:
            self._delivery = None
            self._serving = False

    async def wait_idle(self) -> None:
        """Join all accepted operations, including durable work awaiting uploaded bytes."""
        while True:
            self._idle_changed.clear()
            self._require_open()
            if not self._running and not await self._ledger.dispatches():
                return
            await _transport.changed_or_poll(self._idle_changed)

    async def aclose(self) -> None:
        """Cancel owned work and release all borrows while retaining durable receipts."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await settle(self._closing)

    async def _close(self) -> None:
        self._closed = True
        self._changed.set()
        self._idle_changed.set()
        failures: list[BaseException | None] = []
        delivery_tasks = [
            task for task in (self._delivery, self._supervisor) if task is not None
        ]
        for task in delivery_tasks:
            task.cancel()
        failures.extend(
            result
            for result in await asyncio.gather(*delivery_tasks, return_exceptions=True)
            if isinstance(result, Exception)
        )
        tasks = [task for _session, task in self._running.values()] + list(
            self._cancelling.values()
        )
        for task in tasks:
            task.cancel()
        failures.extend(
            result
            for result in await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(result, Exception)
        )
        try:
            await self._messaging.aclose()
        except BaseException as error:  # noqa: BLE001 -- Raised after all cleanup.
            failures.append(error)
        # Stop execution before returning workspace borrows. A failed borrow
        # cannot prevent other borrows or the storage owner from being released.
        for borrow in self._borrows.values():
            if not borrow.ready.done():
                borrow.task.cancel()
            borrow.close_requested.set()
        failures.extend(
            result
            for result in await asyncio.gather(
                *(borrow.task for borrow in self._borrows.values()),
                return_exceptions=True,
            )
            if isinstance(result, Exception)
        )
        try:
            await self._ledger.aclose()
        except BaseException as error:  # noqa: BLE001 -- Raised after all cleanup.
            failures.append(error)
        raise_cleanup_errors(failures)


__all__ = ["ToolWorker"]
