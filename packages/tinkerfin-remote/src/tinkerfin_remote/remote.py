"""Expose authenticated remote workspace declarations to cloud runtimes."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import uuid4

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from tinkerfin_contracts import (
    PreparedWorkspace,
    RunIdentity,
    ToolDescriptor,
    ToolResult,
    WorkspaceTool,
    emit_workspace_output,
    next_workspace_operation,
)

if TYPE_CHECKING:
    from .session import RemoteSession

from . import _transport
from ._store import Ledger, workspace_key
from ._tasks import raise_cleanup_errors, settle
from ._wire import (
    bounded,
    chunk_bytes,
    decode,
    digest,
    digest_bytes,
    encode,
    key,
    receipt,
)
from .errors import RemoteError, RemoteErrorCode
from .limits import RemoteLimits
from .models import (
    TERMINAL_EVENTS,
    BinaryChunk,
    CallTool,
    CancelOperation,
    CloseWorkspace,
    Command,
    CommandEnvelope,
    CoordinatorFrame,
    Dispatch,
    OpenWorkspace,
    Operation,
    OperationRequest,
    ResolvePath,
    WorkerFrame,
    WorkspaceDescription,
    WorkspaceRequest,
)
from .operations import OperationStatus


@dataclass(frozen=True, slots=True)
class _Result:
    result: ToolResult
    files: Mapping[int, bytes]


class RemoteEndpoint:
    """Bind one authenticated namespace and target to tools and socket delivery.

    Obtain this object from RemoteTools.endpoint. Authentication remains the host's
    responsibility; never choose the namespace or target from model arguments.
    """

    def __init__(self, owner: RemoteTools, namespace: str, target: str) -> None:
        """Create an endpoint declaration without database or transport I/O."""
        self._owner = owner
        self._ledger = Ledger(
            owner._engine,
            role="coordinator",
            namespace=namespace,
            target=target,
            limits=owner._limits,
        )
        self._ready = False
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._readers_changed = asyncio.Event()
        self._serving = False
        self._delivery: asyncio.Task[None] | None = None
        self._closed = False
        self._acknowledgments: asyncio.Queue[CoordinatorFrame] = asyncio.Queue(
            maxsize=1
        )

    async def _prepare(self) -> None:
        self._owner._require_open()
        if self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "Remote endpoint is closed")
        async with self._lock:
            if not self._ready:
                await self._ledger.prepare()
                self._ready = True

    def workspace(self, *, authorization: str) -> RemoteWorkspace:
        """Declare host-authorized workspace access without opening resources."""
        self._owner._require_open()
        if not authorization or len(authorization) > 16_384:
            raise ValueError("authorization must be nonempty bounded text")
        return RemoteWorkspace(self, authorization)

    async def get_operation(self, operation_id: str) -> OperationStatus | None:
        """Read retained execution evidence without sending any remote command.

        Args:
            operation_id: Original operation's 64-character hexadecimal identifier.

        Returns:
            Current evidence for this endpoint, or ``None`` when it has no retained
            record. Absence, pending delivery, and expired results do not prove
            nonexecution and must never trigger an automatic replacement call.

        Raises:
            ValueError: The identifier is malformed.
            RemoteError: The endpoint is closed or storage cannot be read.
            asyncio.CancelledError: The caller cancelled the query; accepted
                database I/O settles before its resources are released.
        """
        if re.fullmatch(r"[0-9a-f]{64}", operation_id) is None:
            raise ValueError(
                "operation_id must be a 64-character hexadecimal identifier"
            )
        await self._prepare()
        return await self._ledger.inspect_operation(operation_id)

    async def _command(self, command: Command) -> str:
        await self._prepare()
        self._owner._require_open()
        envelope = CommandEnvelope(command_id=digest(command), command=command)
        await self._ledger.command(envelope)
        self._changed.set()
        return envelope.command_id

    async def revoke(self) -> None:
        """Permanently revoke this target and durably cancel all accepted work.

        Existing delivery is awakened immediately. Offline intent remains durable;
        it is not confirmation that remote processes have stopped. A new pairing
        must use a new target. Already committed results remain available as evidence.
        """
        await self._prepare()
        self._owner._require_open()
        await self._ledger.revoke()
        self._changed.set()
        self._readers_changed.set()

    async def _close_sessions(self) -> None:
        if await self._ledger.is_revoked():
            return
        commands: list[Command] = []
        for dispatch in await self._ledger.dispatches():
            commands.append(CancelOperation(operation_id=dispatch.request.operation_id))
        for request in await self._ledger.open_sessions():
            commands.append(CloseWorkspace(session_id=request.session_id))
        failures: list[BaseException | None] = []
        for command in commands:
            try:
                await self._ledger.command(
                    CommandEnvelope(command_id=digest(command), command=command)
                )
            except BaseException as error:  # noqa: BLE001 -- Raised after all cleanup.
                failures.append(error)
        raise_cleanup_errors(failures)

    async def _close_resources(self) -> None:
        # Each endpoint must retain its own stop intent even if another endpoint
        # is unavailable. Storage ownership ends only after all accepted SQL.
        failures: list[BaseException | None] = []
        try:
            await self._ledger.wait_idle()
            if self._ready:
                await self._close_sessions()
        except BaseException as error:  # noqa: BLE001 -- Raised after all cleanup.
            failures.append(error)
        try:
            await self._ledger.aclose()
        except BaseException as error:  # noqa: BLE001 -- Raised after all cleanup.
            failures.append(error)
        raise_cleanup_errors(failures)

    async def _incoming(self, payload: str) -> None:
        frame = decode(WorkerFrame, payload, self._owner._limits.frame_bytes)
        if (
            frame.namespace != self._ledger.namespace
            or frame.target != self._ledger.target
        ):
            raise RemoteError(
                RemoteErrorCode.REJECTED, "Frame crossed its authenticated endpoint"
            )
        await self._ledger.acknowledge("command", frame.commands_received)
        await self._ledger.acknowledge("chunk", frame.chunks_received)
        for event in frame.events:
            await self._ledger.receive_event(event)
        for event in frame.workspaces:
            await self._ledger.workspace_event(event)
        await self._ledger.acknowledge(
            "event", tuple(receipt(event) for event in frame.events)
        )
        await self._ledger.acknowledge(
            "workspace", tuple(receipt(event) for event in frame.workspaces)
        )
        if (
            frame.events
            or frame.workspaces
            or frame.commands_received
            or frame.chunks_received
        ):
            await self._ledger.reclaim()
        if frame.events or frame.workspaces:
            self._readers_changed.set()
        if (
            frame.commands_received
            or frame.chunks_received
            or frame.events
            or frame.workspaces
        ):
            await self._acknowledgments.put(
                CoordinatorFrame(
                    namespace=self._ledger.namespace,
                    target=self._ledger.target,
                    commands_confirmed=frame.commands_received,
                    chunks_confirmed=frame.chunks_received,
                    events_received=tuple(receipt(event) for event in frame.events),
                    workspaces_received=tuple(
                        receipt(event) for event in frame.workspaces
                    ),
                )
            )
            self._changed.set()

    async def _outgoing(self) -> str:
        if self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "Remote endpoint is closed")
        if await self._ledger.is_revoked():
            return bounded(
                CoordinatorFrame(
                    namespace=self._ledger.namespace,
                    target=self._ledger.target,
                    revoked=True,
                ),
                self._owner._limits.frame_bytes,
            )
        try:
            frame = self._acknowledgments.get_nowait()
        except asyncio.QueueEmpty:
            frame = CoordinatorFrame(
                namespace=self._ledger.namespace, target=self._ledger.target
            )
        budget = (
            self._owner._limits.frame_bytes
            - len(encode(frame).encode())
            - min(4096, self._owner._limits.frame_bytes // 8)
        )
        commands = await self._ledger.pending("command", CommandEnvelope, budget=budget)
        budget -= sum(len(encode(command).encode()) + 1 for command in commands)
        chunks = await self._ledger.pending("chunk", BinaryChunk, budget=budget)
        return bounded(
            frame.model_copy(update={"commands": commands, "chunks": chunks}),
            self._owner._limits.frame_bytes,
        )

    async def serve(
        self,
        *,
        send: Callable[[str], Awaitable[None]],
        receive: Callable[[], Awaitable[str]],
    ) -> None:
        """Attach authenticated duplex text delivery with regular keepalive frames.

        The caller owns the socket and its authentication/fencing. Disconnecting
        only detaches delivery; accepted operations and cancellation stay durable.
        Only one attachment is admitted at a time; an overlapping call fails
        with ``RemoteErrorCode.CONFLICT``.
        """
        await self._prepare()
        if self._serving:
            raise RemoteError(
                RemoteErrorCode.CONFLICT, "Endpoint delivery is already attached"
            )
        # Storage admission can suspend; reserve delivery before it so another
        # authenticated connection cannot pass the same ownership check.
        self._serving = True
        revoked_sent = False

        async def outgoing() -> str:
            nonlocal revoked_sent
            if revoked_sent:
                raise RemoteError(
                    RemoteErrorCode.REJECTED, "Remote endpoint was revoked"
                )
            payload = await self._outgoing()
            revoked_sent = await self._ledger.is_revoked()
            if revoked_sent:
                payload = bounded(
                    CoordinatorFrame(
                        namespace=self._ledger.namespace,
                        target=self._ledger.target,
                        revoked=True,
                    ),
                    self._owner._limits.frame_bytes,
                )
            return payload

        try:
            if await self._ledger.is_revoked():
                raise RemoteError(
                    RemoteErrorCode.REJECTED, "Remote endpoint was revoked"
                )
            self._delivery = asyncio.create_task(
                _transport.serve(
                    send=send,
                    receive=receive,
                    outgoing=outgoing,
                    incoming=self._incoming,
                    changed=self._changed,
                )
            )
            await self._delivery
        finally:
            self._delivery = None
            self._serving = False
            self._readers_changed.set()

    async def _workspace_reply(self, identifier: str) -> WorkspaceDescription:
        while True:
            self._readers_changed.clear()
            self._owner._require_open()
            reply = await self._ledger.get_workspace_reply(identifier)
            if reply is not None:
                if reply.error is not None:
                    raise RemoteError(RemoteErrorCode.REJECTED, reply.error)
                if reply.description is None:
                    raise RemoteError(
                        RemoteErrorCode.PROTOCOL, "Workspace reply has no capabilities"
                    )
                return reply.description
            if await self._ledger.is_revoked():
                raise RemoteError(
                    RemoteErrorCode.REJECTED, "Remote endpoint was revoked"
                )
            await _transport.changed_or_poll(self._readers_changed)

    async def _wait_result(
        self, identifier: str, *, output: bool = True, require_attachment: bool = False
    ) -> _Result:
        cursor = 0
        contents: dict[int, bytearray] = {}
        result_bytes = bytearray()
        while True:
            self._readers_changed.clear()
            self._owner._require_open()
            if not await self._ledger.result_available(identifier):
                raise RemoteError(
                    RemoteErrorCode.UNCERTAIN,
                    "The retained large result has expired; its operation was not repeated",
                )
            events = await self._ledger.event_page(identifier, after=cursor)
            for event in events:
                cursor = event.sequence
                if event.kind == "output" and output:
                    await emit_workspace_output(event.output)
                elif event.chunk is not None:
                    chunk = event.chunk
                    target = (
                        result_bytes
                        if chunk.channel == "result"
                        else contents.setdefault(chunk.file_index, bytearray())
                    )
                    if chunk.offset != len(target):
                        raise RemoteError(
                            RemoteErrorCode.PROTOCOL,
                            "Binary replay has a noncontiguous offset",
                        )
                    target.extend(chunk_bytes(chunk, self._owner._limits.chunk_bytes))
                elif event.kind == "completed":
                    if not await self._ledger.result_available(identifier):
                        raise RemoteError(
                            RemoteErrorCode.UNCERTAIN,
                            "The retained large result has expired; its operation was not repeated",
                        )
                    result = event.result
                    if event.result_transfer is not None:
                        expected = event.result_transfer
                        if (
                            len(result_bytes) != expected.size
                            or digest_bytes(bytes(result_bytes)) != expected.digest
                        ):
                            raise RemoteError(
                                RemoteErrorCode.PROTOCOL,
                                "Tool result transfer failed its digest",
                            )
                        try:
                            result = ToolResult.model_validate_json(bytes(result_bytes))
                        except ValidationError as error:
                            raise RemoteError(
                                RemoteErrorCode.PROTOCOL,
                                "Remote result transfer is invalid",
                                cause=error,
                            ) from error
                    assert result is not None
                    return _Result(
                        result,
                        {index: bytes(content) for index, content in contents.items()},
                    )
                elif event.kind in TERMINAL_EVENTS:
                    code = {
                        "uncertain": RemoteErrorCode.UNCERTAIN,
                        "cancelled": RemoteErrorCode.CANCELLED,
                    }.get(event.kind, RemoteErrorCode.REJECTED)
                    raise RemoteError(
                        code, event.message or "Remote operation did not complete"
                    )
            if not events:
                if await self._ledger.is_revoked():
                    raise RemoteError(
                        RemoteErrorCode.UNCERTAIN,
                        "Endpoint authorization was revoked; the operation outcome is not confirmed",
                    )
                if require_attachment and not self._serving:
                    raise RemoteError(
                        RemoteErrorCode.UNCERTAIN,
                        "Cancellation is saved; the disconnected endpoint has not confirmed termination",
                    )
                await _transport.changed_or_poll(self._readers_changed)

    async def _invoke(
        self,
        session: WorkspaceRequest,
        operation: Operation,
        *,
        uploads: tuple[bytes, ...] = (),
    ) -> _Result:
        self._owner._require_open()
        scope = next_workspace_operation()
        if (
            scope.identity != session.identity
            or scope.workspace_scope_id != session.workspace_scope_id
        ):
            raise RemoteError(
                RemoteErrorCode.REJECTED,
                "Operation scope crossed its prepared workspace",
            )
        observation = session.session_id if isinstance(operation, ResolvePath) else None
        request = OperationRequest(
            operation_id=key(
                scope.workspace_scope_id,
                scope.origin_identity.model_dump(mode="json"),
                scope.invocation_id,
                scope.steps,
                observation,
            ),
            origin=scope.origin_identity,
            workspace_scope_id=scope.workspace_scope_id,
            invocation_id=scope.invocation_id,
            steps=scope.steps,
            graph_namespace=scope.graph_namespace,
            purpose=scope.purpose,
            tool_call_id=scope.tool_call_id,
            observation_session=observation,
            operation=operation,
        )
        try:
            if await self._ledger.completed(request):
                return await self._wait_result(request.operation_id)
            await self._command(
                Dispatch(session_id=session.session_id, request=request)
            )
            for index, content in enumerate(uploads):
                for offset in range(0, len(content), self._owner._limits.chunk_bytes):
                    self._owner._require_open()
                    part = content[offset : offset + self._owner._limits.chunk_bytes]
                    await self._ledger.upload_chunk(
                        BinaryChunk(
                            operation_id=request.operation_id,
                            file_index=index,
                            offset=offset,
                            data=base64.b64encode(part).decode("ascii"),
                            digest=digest_bytes(part),
                        )
                    )
                    self._changed.set()
            return await self._wait_result(request.operation_id)
        except asyncio.CancelledError as error:
            error.add_note(f"Remote operation ID: {request.operation_id}")
            try:
                confirmed = await settle(self._cancel(request))
            except Exception as cleanup_error:
                error.add_note("Remote cancellation could not be confirmed")
                raise error from cleanup_error
            if not confirmed:
                error.add_note(
                    "Remote cancellation is durable; endpoint termination is not yet confirmed"
                )
            raise
        except Exception as error:
            await settle(self._cancel(request))
            if isinstance(error, RemoteError):
                raise RemoteError(
                    error.code,
                    error.message,
                    context={**error.context, "operation_id": request.operation_id},
                    diagnostic_context=error.diagnostic_context,
                    cause=error.cause or error,
                ) from (error.cause or error)
            raise

    async def _cancel(self, request: OperationRequest) -> bool:
        if self._owner._closed:
            return False
        identifier = request.operation_id
        try:
            if not await self._ledger.has_operation(identifier):
                return True
            saved, state, _cancelled = await self._ledger.get_operation(identifier)
            if state in TERMINAL_EVENTS:
                return state != "uncertain"
            assert saved is not None
            if digest(saved) != digest(request):
                return True
            if await self._ledger.is_revoked():
                return False
            await self._command(CancelOperation(operation_id=identifier))
            await self._wait_result(identifier, output=False, require_attachment=True)
        except RemoteError as error:
            if error.code not in {
                RemoteErrorCode.CANCELLED,
                RemoteErrorCode.UNCERTAIN,
                RemoteErrorCode.REJECTED,
                RemoteErrorCode.CLOSED,
            }:
                raise
            return error.code not in {RemoteErrorCode.UNCERTAIN, RemoteErrorCode.CLOSED}
        return True

    async def _close_session(self, identifier: str) -> None:
        if (
            not self._ready
            or self._owner._closed
            or not await self._ledger.has_session(identifier)
        ):
            return
        if await self._ledger.is_revoked():
            return
        await self._command(CloseWorkspace(session_id=identifier))
        while self._serving and not self._owner._closed:
            self._readers_changed.clear()
            if await self._ledger.session_settled(identifier):
                return
            await _transport.changed_or_poll(self._readers_changed)


class RemoteWorkspace:
    """Prepare remote tools and a filesystem backend for one cloud Run."""

    def __init__(self, endpoint: RemoteEndpoint, authorization: str) -> None:
        """Retain the authenticated endpoint and host's opaque authorization."""
        self._endpoint = endpoint
        self._authorization = authorization

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[RemoteSession, BackendProtocol]]:
        """Borrow advertised tools until the cloud Run releases its resources."""
        from .session import RemoteSession

        endpoint = self._endpoint
        if identity.namespace != endpoint._ledger.namespace:
            raise RemoteError(
                RemoteErrorCode.REJECTED,
                "Runtime namespace differs from its remote endpoint",
            )
        temporary = WorkspaceRequest(
            identity=identity,
            target=endpoint._ledger.target,
            authorization=self._authorization,
            workspace_scope_id="0" * 64,
            session_id=key(uuid4().hex),
        )
        request = temporary.model_copy(
            update={"workspace_scope_id": workspace_key(temporary)}
        )
        session = RemoteSession(endpoint, request)
        interrupted: BaseException | None = None
        try:
            identifier = await endpoint._command(OpenWorkspace(request=request))
            try:
                async with asyncio.timeout(30):
                    description = await endpoint._workspace_reply(identifier)
            except TimeoutError as error:
                raise RemoteError(
                    RemoteErrorCode.TIMEOUT,
                    "Remote workspace did not become available",
                    cause=error,
                ) from error
            tools: list[WorkspaceTool] = []
            for descriptor in description.tools:

                async def invoke(
                    arguments: Mapping[str, JsonValue],
                    *,
                    descriptor: ToolDescriptor = descriptor,
                ) -> ToolResult:
                    session._require_open()
                    return (
                        await endpoint._invoke(
                            request,
                            CallTool(
                                name=descriptor.name,
                                descriptor_digest=digest(descriptor),
                                arguments=dict(arguments),
                            ),
                        )
                    ).result

                tools.append(WorkspaceTool(descriptor, invoke))
            yield PreparedWorkspace(
                workspace=session,
                backend=CompositeBackend(
                    default=session,
                    routes={},
                    artifacts_root=description.artifacts_root,
                ),
                filesystem_instructions=description.filesystem_instructions,
                tool_descriptions=description.tool_descriptions,
                file_path_resolver=session.resolve_file_path,
                tools=tuple(tools),
                skill_sources=description.skill_sources,
                workspace_scope_id=request.workspace_scope_id,
            )
        except BaseException as error:
            interrupted = error
            raise
        finally:
            session._closed = True
            try:
                await settle(endpoint._close_session(request.session_id))
            except Exception as cleanup_error:
                if interrupted is not None and not isinstance(interrupted, Exception):
                    raise interrupted from cleanup_error
                raise


class RemoteTools:
    """Own cloud-side durable remote workspaces and authenticated endpoints.

    Args:
        engine: Borrowed async SQLAlchemy engine with host-configured driver and
            database deadlines. Cancellation waits for accepted SQL to settle.
        limits: Fixed per-endpoint storage, message, output and concurrency limits.

    Open once for the application lifetime. Socket routes attach endpoint.serve;
    cloud runtimes receive endpoint.workspace. Closing saves cancellation and
    releases local waiters; an offline peer has not thereby confirmed termination.
    """

    def __init__(
        self, engine: AsyncEngine, *, limits: RemoteLimits = RemoteLimits()
    ) -> None:
        """Capture storage and capacity without starting background work."""
        self._engine = engine
        self._limits = limits
        self._endpoints: dict[tuple[str, str], RemoteEndpoint] = {}
        self._open = False
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        """Open this one application lifecycle without borrowing engine ownership."""
        if self._open or self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "RemoteTools cannot be reopened")
        self._open = True
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Save cancellation before the application releases its SQL engine."""
        await settle(self.aclose(), primary_error=error)

    def _require_open(self) -> None:
        if not self._open or self._closed:
            raise RemoteError(RemoteErrorCode.CLOSED, "RemoteTools is not open")

    def endpoint(self, *, namespace: str, target: str) -> RemoteEndpoint:
        """Bind both authenticated owner and target; neither comes from a model."""
        self._require_open()
        identity = namespace, target
        if identity not in self._endpoints:
            self._endpoints[identity] = RemoteEndpoint(self, namespace, target)
        return self._endpoints[identity]

    async def aclose(self) -> None:
        """Save cancellation and join accepted SQL before returning the engine."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await settle(self._closing)

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for endpoint in self._endpoints.values():
            endpoint._closed = True
            endpoint._changed.set()
            endpoint._readers_changed.set()
        deliveries = [
            endpoint._delivery
            for endpoint in self._endpoints.values()
            if endpoint._delivery is not None
        ]
        for delivery in deliveries:
            delivery.cancel()
        deliveries_result = await asyncio.gather(*deliveries, return_exceptions=True)
        failures: list[BaseException | None] = [
            result for result in deliveries_result if isinstance(result, Exception)
        ]
        failures.extend(
            await asyncio.gather(
                *(endpoint._close_resources() for endpoint in self._endpoints.values()),
                return_exceptions=True,
            )
        )
        raise_cleanup_errors(failures)


__all__ = ["RemoteEndpoint", "RemoteTools", "RemoteWorkspace"]
