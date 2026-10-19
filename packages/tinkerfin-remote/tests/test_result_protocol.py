"""Reject malformed remote results without exposing their payload as a public error."""

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_contracts import (
    RunIdentity,
    ToolExecutionScope,
    ToolResult,
    bind_workspace_operation,
)
from tinkerfin_remote import RemoteError, RemoteErrorCode, RemoteSession, RemoteTools
from tinkerfin_remote.models import (
    BinaryChunk,
    CloseWorkspace,
    CoordinatorFrame,
    Dispatch,
    OpenWorkspace,
    OperationEvent,
    Receipt,
    ResultTransfer,
    WorkerFrame,
    WorkspaceDescription,
    WorkspaceEvent,
)


def _digest(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@asynccontextmanager
async def _session(
    path: Path, result: JsonValue, *, transfer: bytes | None = None
) -> AsyncGenerator[RemoteSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path / 'coordinator.sqlite'}")
    identity = RunIdentity(namespace="results", thread_id="thread", run_id="run")
    incoming: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    received: set[str] = set()

    async def receive_from_coordinator(payload: str) -> None:
        frame = CoordinatorFrame.model_validate_json(payload)
        events: list[OperationEvent] = []
        workspaces: list[WorkspaceEvent] = []
        for envelope in frame.commands:
            if envelope.command_id in received:
                continue
            received.add(envelope.command_id)
            command = envelope.command
            if isinstance(command, OpenWorkspace):
                workspaces.append(
                    WorkspaceEvent(
                        command_id=envelope.command_id,
                        session_id=command.request.session_id,
                        description=WorkspaceDescription(
                            artifacts_root="/workspace/.artifacts"
                        ),
                    )
                )
            elif isinstance(command, CloseWorkspace):
                workspaces.append(
                    WorkspaceEvent(
                        command_id=envelope.command_id,
                        session_id=command.session_id,
                        closed=True,
                    )
                )
            elif isinstance(command, Dispatch):
                identifier = command.request.operation_id
                events.append(
                    OperationEvent(operation_id=identifier, sequence=1, kind="started")
                )
                if transfer is None:
                    events.append(
                        OperationEvent(
                            operation_id=identifier,
                            sequence=2,
                            kind="completed",
                            result=ToolResult(value=result),
                        )
                    )
                else:
                    digest = hashlib.sha256(transfer).hexdigest()
                    events.extend(
                        (
                            OperationEvent(
                                operation_id=identifier,
                                sequence=2,
                                kind="binary",
                                chunk=BinaryChunk(
                                    operation_id=identifier,
                                    channel="result",
                                    file_index=0,
                                    offset=0,
                                    data=base64.b64encode(transfer).decode(),
                                    digest=digest,
                                ),
                            ),
                            OperationEvent(
                                operation_id=identifier,
                                sequence=3,
                                kind="completed",
                                result_transfer=ResultTransfer(
                                    size=len(transfer), digest=digest
                                ),
                            ),
                        )
                    )
        if frame.commands:
            await incoming.put(
                WorkerFrame(
                    namespace=frame.namespace,
                    target=frame.target,
                    commands_received=tuple(
                        Receipt(key=item.command_id, digest=_digest(item))
                        for item in frame.commands
                    ),
                    events=tuple(events),
                    workspaces=tuple(workspaces),
                ).model_dump_json()
            )

    try:
        async with RemoteTools(engine) as remote:
            endpoint = remote.endpoint(namespace=identity.namespace, target="peer")
            delivery = asyncio.create_task(
                endpoint.serve(send=receive_from_coordinator, receive=incoming.get)
            )
            try:
                async with endpoint.workspace(authorization="test-grant").prepare(
                    identity
                ) as prepared:
                    assert prepared.workspace_scope_id is not None
                    with bind_workspace_operation(
                        ToolExecutionScope(
                            identity=identity,
                            origin_identity=identity,
                            workspace_scope_id=prepared.workspace_scope_id,
                            invocation_id="result",
                            graph_namespace=(),
                            purpose="tool",
                        )
                    ):
                        yield prepared.workspace
            finally:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("boundary", ["result", "upload", "download", "transfer"])
async def test_malformed_peer_results_keep_payload_in_private_cause(
    tmp_path: Path, boundary: str
) -> None:
    private: dict[str, JsonValue] = {"private": "synthetic-provider-content"}
    result: JsonValue
    if boundary == "result":
        result = {"output": private, "exit_code": 0, "truncated": False}
    elif boundary == "upload":
        result = [{"path": "/report", "error": private}]
    else:
        result = [{"path": "/report", "size": 0, "digest": None, "error": private}]
    transfer = (
        json.dumps({"value": None, "is_error": private}).encode()
        if boundary == "transfer"
        else None
    )
    async with _session(tmp_path, result, transfer=transfer) as session:
        with pytest.raises(RemoteError) as caught:
            if boundary == "upload":
                await session.aupload_files([("/report", b"")])
            elif boundary == "download":
                await session.adownload_files(["/report"])
            else:
                await session.aexecute("true")
        assert caught.value.code == RemoteErrorCode.PROTOCOL
        assert isinstance(caught.value.cause, ValidationError)
        assert caught.value.__cause__ is caught.value.cause
        assert "synthetic-provider-content" not in str(caught.value)
        assert caught.value.context == {}
