"""Pause new work without interrupting accepted execution or its transport."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from test_remote import Harness, MemoryFiles

from tinkerfin_contracts import RunIdentity, bind_workspace_operation
from tinkerfin_remote import (
    RemoteError,
    RemoteErrorCode,
    ToolExecutor,
    WorkspaceRequest,
)
from tinkerfin_remote.models import (
    CoordinatorFrame,
    Dispatch,
    OpenWorkspace,
    UploadFiles,
)


async def test_maintenance_rejects_new_work_and_preserves_replay(tmp_path: Path):
    harness = Harness(tmp_path)
    await harness.open()
    try:
        with bind_workspace_operation(harness.scope("retained")):
            await harness.session.awrite("/workspace/retained", "value")
        async with harness.worker.maintenance():
            with bind_workspace_operation(harness.scope("retained")):
                await harness.session.awrite("/workspace/retained", "value")
            with bind_workspace_operation(harness.scope("refused")):
                with pytest.raises(RemoteError, match="maintenance"):
                    await harness.session.awrite("/workspace/refused", "value")
            identity = RunIdentity(
                namespace="account", thread_id="another-thread", run_id="another-run"
            )
            with pytest.raises(RemoteError, match="maintenance"):
                async with harness.endpoint.workspace(authorization="grant").prepare(
                    identity
                ):
                    pytest.fail("Maintenance admitted a new workspace")
            with pytest.raises(RemoteError) as overlap:
                async with harness.worker.maintenance():
                    pytest.fail("Maintenance accepted a second owner")
            assert overlap.value.code == RemoteErrorCode.CONFLICT
            assert harness.files.writes == 1
        with bind_workspace_operation(harness.scope("resumed")):
            await harness.session.awrite("/workspace/resumed", "value")
        assert harness.files.writes == 2
        with bind_workspace_operation(harness.scope("refused")):
            with pytest.raises(RemoteError, match="maintenance"):
                await harness.session.awrite("/workspace/refused", "value")
        assert harness.files.writes == 2
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "resolution", ["complete", "cancel-maintenance", "cancel-operation"]
)
async def test_maintenance_waits_for_owned_execution_and_cancel_resumes(
    tmp_path: Path, resolution: str
):
    harness = Harness(tmp_path)
    await harness.open()
    entering, entered, leave = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute():
        with bind_workspace_operation(
            harness.scope("running"), emit_output=harness.emit
        ):
            return await harness.session.aexecute("wait")

    async def maintain():
        entering.set()
        async with harness.worker.maintenance():
            entered.set()
            await leave.wait()

    execution = asyncio.create_task(execute())
    maintenance = None
    try:
        await harness.files.started.wait()
        maintenance = asyncio.create_task(maintain())
        await entering.wait()
        replay = tuple(
            item
            for frame in harness.wire.cloud_frames
            for item in frame.commands
            if isinstance(item.command, (OpenWorkspace, Dispatch))
        )
        # Replay accepted identities through delivery before the next fresh
        # request, rather than reading the coordinator's completed-result cache.
        await harness.wire.to_worker.put(
            CoordinatorFrame(
                namespace="account", target="device", commands=replay
            ).model_dump_json()
        )
        with bind_workspace_operation(harness.scope("while-waiting")):
            with pytest.raises(RemoteError, match="maintenance"):
                await harness.session.awrite("/workspace/refused", "value")
        assert not entered.is_set() and not harness.files.stopped.is_set()
        if resolution == "cancel-maintenance":
            maintenance.cancel()
            with pytest.raises(asyncio.CancelledError):
                await maintenance
            with bind_workspace_operation(harness.scope("resumed-before-idle")):
                await harness.session.awrite("/workspace/resumed", "value")
            assert not harness.files.stopped.is_set()
        if resolution == "cancel-operation":
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        else:
            harness.files.release.set()
            assert (await execution).exit_code == 0
        if resolution != "cancel-maintenance":
            await entered.wait()
            assert harness.files.stopped.is_set()
            leave.set()
            await maintenance
    finally:
        harness.files.release.set()
        execution.cancel()
        if maintenance is not None:
            maintenance.cancel()
        await asyncio.gather(
            execution,
            *(() if maintenance is None else (maintenance,)),
            return_exceptions=True,
        )
        await harness.close()


async def test_maintenance_keeps_delivery_for_accepted_upload(tmp_path: Path):
    harness = Harness(tmp_path)
    await harness.open()
    await harness.wire.close()
    chunks_ready, release_chunks = asyncio.Event(), asyncio.Event()
    entering, entered, leave = asyncio.Event(), asyncio.Event(), asyncio.Event()
    wire = harness.connect()
    original_send = wire.send_cloud

    async def hold_chunks(payload: str):
        frame = CoordinatorFrame.model_validate_json(payload)
        if frame.chunks:
            await original_send(
                frame.model_copy(update={"chunks": ()}).model_dump_json()
            )
            chunks_ready.set()
            await release_chunks.wait()
        await original_send(payload)

    # Attach the same public transport with a controlled upload boundary.
    await wire.close()
    wire.tasks = [
        asyncio.create_task(
            harness.endpoint.serve(send=hold_chunks, receive=wire.to_cloud.get)
        ),
        asyncio.create_task(
            harness.worker.serve(send=wire.send_worker, receive=wire.to_worker.get)
        ),
    ]

    async def upload():
        with bind_workspace_operation(harness.scope("upload")):
            return await harness.session.aupload_files(
                [("/workspace/upload", b"content")]
            )

    async def maintain():
        entering.set()
        async with harness.worker.maintenance():
            entered.set()
            await leave.wait()

    uploading = asyncio.create_task(upload())
    maintenance = None
    try:
        await chunks_ready.wait()
        invocation = await wire.wait_cloud(
            lambda frame: any(
                isinstance(item.command, Dispatch)
                and isinstance(item.command.request.operation, UploadFiles)
                for item in frame.commands
            )
        )
        operation = next(
            item.command.request.operation_id
            for item in invocation.commands
            if isinstance(item.command, Dispatch)
            and isinstance(item.command.request.operation, UploadFiles)
        )
        await wire.wait_worker(
            lambda frame: any(
                item.key == command.command_id
                for item in frame.commands_received
                for command in invocation.commands
                if isinstance(command.command, Dispatch)
                and command.command.request.operation_id == operation
            )
        )
        maintenance = asyncio.create_task(maintain())
        await entering.wait()
        assert not entered.is_set()
        release_chunks.set()
        assert (await uploading)[0].error is None
        await entered.wait()
        assert harness.files.files["/workspace/upload"] == b"content"
        leave.set()
        await maintenance
    finally:
        release_chunks.set()
        uploading.cancel()
        if maintenance is not None:
            maintenance.cancel()
        await asyncio.gather(
            uploading,
            *(() if maintenance is None else (maintenance,)),
            return_exceptions=True,
        )
        await harness.close()


async def test_closing_worker_releases_maintenance_without_reopening(tmp_path: Path):
    harness = Harness(tmp_path)
    await harness.open()
    try:
        async with harness.worker.maintenance():
            await harness.wire.close()
            await harness.worker.aclose()
        with pytest.raises(RemoteError) as closed:
            async with harness.worker.maintenance():
                pytest.fail("Closed worker resumed")
        assert closed.value.code == RemoteErrorCode.CLOSED
    finally:
        await harness.close()


@pytest.mark.parametrize("storage_failure", [False, True])
async def test_maintenance_joins_preparation_and_reports_supervision_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage_failure: bool
):
    harness = Harness(tmp_path)
    preparing, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered, leave = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def host(
        request: WorkspaceRequest,
    ) -> AsyncGenerator[ToolExecutor[MemoryFiles]]:
        if request.identity.thread_id == "preparing":
            preparing.set()
            await release.wait()
        yield ToolExecutor(workspace=harness.files)

    monkeypatch.setattr(harness, "host", host)
    await harness.open()

    async def prepare():
        async with harness.endpoint.workspace(authorization="grant").prepare(
            RunIdentity(namespace="account", thread_id="preparing", run_id="run")
        ):
            await leave.wait()

    async def maintain():
        async with harness.worker.maintenance():
            entered.set()
            await leave.wait()

    preparation = asyncio.create_task(prepare())
    maintenance: asyncio.Task[None] | None = None
    original_wait = asyncio.Event.wait

    async def observe_wait(signal: asyncio.Event) -> bool:
        # Observe suspension, not a worker field or a number of scheduler turns.
        observed = asyncio.current_task() is maintenance and not signal.is_set()
        if observed:
            waiting.set()
        try:
            return await original_wait(signal)
        finally:
            if observed:
                waiting.clear()

    def fail_storage(connection, statement, _multiparams, _params, _options):
        # Only the closed-session scan is faulted; maintenance's own reads must
        # remain available so this covers notification of a background failure.
        if "closed IS true" in str(statement) and "settled IS false" in str(statement):
            connection.connection.dbapi_connection.run_async(
                lambda _driver: original_wait(waiting)
            )
            raise SQLAlchemyError("controlled supervision storage failure")

    installed = False
    try:
        await preparing.wait()
        monkeypatch.setattr(asyncio.Event, "wait", observe_wait)
        maintenance = asyncio.create_task(maintain())
        await waiting.wait()
        assert not entered.is_set()
        if storage_failure:
            event.listen(
                harness.worker_engine.sync_engine, "before_execute", fail_storage
            )
            installed = True
            replay = next(
                item
                for frame in harness.wire.cloud_frames
                for item in frame.commands
                if isinstance(item.command, OpenWorkspace)
            )
            await harness.wire.to_worker.put(
                CoordinatorFrame(
                    namespace="account", target="device", commands=(replay,)
                ).model_dump_json()
            )
            with pytest.raises(RemoteError) as failed:
                await maintenance
            assert failed.value.code == RemoteErrorCode.STORAGE
            assert not entered.is_set() and not release.is_set()
        else:
            release.set()
            await entered.wait()
            leave.set()
            await maintenance
            await preparation
    finally:
        if installed:
            event.remove(
                harness.worker_engine.sync_engine, "before_execute", fail_storage
            )
        release.set()
        leave.set()
        if storage_failure:
            await asyncio.gather(harness.wire.close(), return_exceptions=True)
        preparation.cancel()
        if maintenance is not None:
            maintenance.cancel()
        await asyncio.gather(
            preparation,
            *(() if maintenance is None else (maintenance,)),
            return_exceptions=True,
        )
        cleanup = await asyncio.gather(harness.close(), return_exceptions=True)
        if not storage_failure:
            assert cleanup == [None]
