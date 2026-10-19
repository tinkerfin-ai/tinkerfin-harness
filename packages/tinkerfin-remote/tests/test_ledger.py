"""Verify immutable admission, bounded transport and cancellation evidence."""

import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_contracts import RunIdentity, ToolResult
from tinkerfin_remote import RemoteError, RemoteErrorCode, RemoteLimits
from tinkerfin_remote._store import Ledger, operation_key, workspace_key
from tinkerfin_remote._wire import digest, digest_bytes, receipt
from tinkerfin_remote.models import (
    BinaryChunk,
    BinaryFile,
    CancelOperation,
    CloseWorkspace,
    Command,
    CommandEnvelope,
    Dispatch,
    ExecuteCommand,
    OpenWorkspace,
    OperationEvent,
    OperationRequest,
    UploadFiles,
    WorkspaceDescription,
    WorkspaceEvent,
    WorkspaceRequest,
)


@asynccontextmanager
async def ledger(path: Path, *, retained_bytes: int = 131072) -> AsyncGenerator[Ledger]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}/ledger.db")
    try:
        store = Ledger(
            engine,
            role="worker",
            namespace="account",
            target="device",
            limits=RemoteLimits(
                frame_bytes=4096,
                request_bytes=2048,
                chunk_bytes=512,
                output_bytes=16384,
                retained_bytes=retained_bytes,
            ),
        )
        await store.prepare()
        yield store
    finally:
        await engine.dispose()


def workspace(session: str = "a") -> WorkspaceRequest:
    request = WorkspaceRequest(
        identity=RunIdentity(namespace="account", thread_id="thread", run_id="run"),
        target="device",
        authorization="grant",
        session_id=session * 64,
        workspace_scope_id="0" * 64,
    )
    return request.model_copy(update={"workspace_scope_id": workspace_key(request)})


def operation(request: WorkspaceRequest) -> OperationRequest:
    value = OperationRequest(
        operation_id="0" * 64,
        origin=request.identity,
        workspace_scope_id=request.workspace_scope_id,
        invocation_id="command",
        steps=("operation:1",),
        graph_namespace=(),
        purpose="tool",
        tool_call_id="tool",
        operation=ExecuteCommand(command="effect"),
    )
    return value.model_copy(update={"operation_id": operation_key(value)})


async def command(store: Ledger, value: Command) -> None:
    await store.command(CommandEnvelope(command_id=digest(value), command=value))


async def admitted(store: Ledger) -> tuple[WorkspaceRequest, OperationRequest]:
    request = workspace()
    await command(store, OpenWorkspace(request=request))
    invocation = operation(request)
    await command(store, Dispatch(session_id=request.session_id, request=invocation))
    return request, invocation


@pytest.mark.asyncio
async def test_close_records_cancel_for_running_and_preserves_terminal(
    tmp_path: Path,
) -> None:
    async with ledger(tmp_path) as store:
        request, invocation = await admitted(store)
        await store.append(invocation.operation_id, kind="started")
        await command(store, CloseWorkspace(session_id=request.session_id))
        assert (await store.get_operation(invocation.operation_id))[1:] == (
            "started",
            True,
        )
        await store.finish(invocation.operation_id, "cancelled", "stopped")
        await store.finish(invocation.operation_id, "uncertain", "late observer")
        assert [
            event.kind for event in await store.event_page(invocation.operation_id)
        ] == ["started", "cancelled"]


@pytest.mark.asyncio
async def test_workspace_reply_cannot_cross_control_request(tmp_path: Path) -> None:
    async with ledger(tmp_path) as store:
        first, second = workspace(), workspace("b")
        await command(store, OpenWorkspace(request=first))
        await command(store, OpenWorkspace(request=second))
        with pytest.raises(RemoteError) as error:
            await store.workspace_event(
                WorkspaceEvent(
                    command_id=digest(OpenWorkspace(request=first)),
                    session_id=second.session_id,
                    description=WorkspaceDescription(artifacts_root="/other"),
                )
            )
        assert error.value.code == RemoteErrorCode.REJECTED


@pytest.mark.asyncio
async def test_oversized_progress_is_rejected_without_blocking_terminal(
    tmp_path: Path,
) -> None:
    async with ledger(tmp_path) as store:
        _request, invocation = await admitted(store)
        started = await store.append(invocation.operation_id, kind="started")
        with pytest.raises(RemoteError) as error:
            await store.append(
                invocation.operation_id, kind="output", output="x" * 8000
            )
        assert error.value.code == RemoteErrorCode.CAPACITY
        await store.finish(invocation.operation_id, "failed", "bounded output")
        await store.acknowledge("event", [receipt(started)])
        assert [
            event.kind for event in await store.pending("event", OperationEvent)
        ] == ["failed"]


@pytest.mark.asyncio
async def test_replayed_receipts_are_identical_and_gaps_are_rejected(
    tmp_path: Path,
) -> None:
    async with ledger(tmp_path) as store:
        _request, invocation = await admitted(store)
        started = await store.append(invocation.operation_id, kind="started")
        await store.receive_event(started)
        with pytest.raises(RemoteError):
            await store.receive_event(
                started.model_copy(update={"kind": "failed", "message": "different"})
            )
        with pytest.raises(RemoteError):
            await store.receive_event(
                OperationEvent(
                    operation_id=invocation.operation_id,
                    sequence=3,
                    kind="completed",
                    result=ToolResult(value="gap"),
                )
            )
        assert len(await store.event_page(invocation.operation_id)) == 1


@pytest.mark.asyncio
async def test_upload_waits_for_complete_contiguous_digest(tmp_path: Path) -> None:
    async with ledger(tmp_path) as store:
        request = workspace()
        await command(store, OpenWorkspace(request=request))
        invocation = operation(request).model_copy(
            update={
                "operation": UploadFiles(
                    files=(
                        BinaryFile(
                            path="/file", size=6, digest=digest_bytes(b"abcdef")
                        ),
                    )
                )
            }
        )
        await command(
            store, Dispatch(session_id=request.session_id, request=invocation)
        )
        part = BinaryChunk(
            operation_id=invocation.operation_id,
            file_index=0,
            offset=0,
            data=base64.b64encode(b"abc").decode(),
            digest=digest_bytes(b"abc"),
        )
        await store.upload_chunk(part)
        await store.upload_chunk(part)
        assert await store.upload_content(invocation.operation_id) is None
        tail = part.model_copy(
            update={
                "offset": 3,
                "data": base64.b64encode(b"def").decode(),
                "digest": digest_bytes(b"def"),
            }
        )
        await store.upload_chunk(tail)
        assert await store.upload_content(invocation.operation_id) == (b"abcdef",)
        with pytest.raises(RemoteError):
            await store.upload_chunk(
                tail.model_copy(
                    update={
                        "data": base64.b64encode(b"xyz").decode(),
                        "digest": digest_bytes(b"xyz"),
                    }
                )
            )


@pytest.mark.asyncio
async def test_binary_result_requires_contiguous_offsets(tmp_path: Path) -> None:
    async with ledger(tmp_path) as store:
        _request, invocation = await admitted(store)
        await store.append(invocation.operation_id, kind="started")
        chunk = BinaryChunk(
            operation_id=invocation.operation_id,
            file_index=0,
            offset=1,
            data="YQ==",
            digest=digest_bytes(b"a"),
        )
        with pytest.raises(RemoteError):
            await store.append(invocation.operation_id, kind="binary", chunk=chunk)
        await store.append(
            invocation.operation_id,
            kind="binary",
            chunk=chunk.model_copy(update={"offset": 0}),
        )


@pytest.mark.asyncio
async def test_capacity_reserves_cancel_close_and_terminal_evidence(
    tmp_path: Path,
) -> None:
    async with ledger(tmp_path, retained_bytes=20000) as store:
        request, invocation = await admitted(store)
        await store.append(invocation.operation_id, kind="started")
        while True:
            try:
                await store.append(
                    invocation.operation_id, kind="output", output="x" * 1000
                )
            except RemoteError as error:
                assert error.code == RemoteErrorCode.CAPACITY
                break
        await command(store, CancelOperation(operation_id=invocation.operation_id))
        await command(store, CloseWorkspace(session_id=request.session_id))
        await store.finish(invocation.operation_id, "cancelled", "stopped")
        await store.workspace_event(
            WorkspaceEvent(
                command_id=digest(CloseWorkspace(session_id=request.session_id)),
                session_id=request.session_id,
                closed=True,
            )
        )
        assert (await store.get_operation(invocation.operation_id))[1:] == (
            "cancelled",
            True,
        )


@pytest.mark.asyncio
async def test_revoked_endpoint_cannot_admit_new_workspace(tmp_path: Path) -> None:
    async with ledger(tmp_path) as store:
        await store.revoke()
        with pytest.raises(RemoteError) as error:
            await command(store, OpenWorkspace(request=workspace()))
        assert error.value.code == RemoteErrorCode.REJECTED
