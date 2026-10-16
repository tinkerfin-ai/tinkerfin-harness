"""Publish declared project inputs before execution admission."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from ..directory_contents import WorkspaceDirectoryContents
from ..errors import (
    OpenSandboxBackendProtocolError,
    OpenSandboxWorkspaceNotInitializedError,
)
from ._sql_tasks import run_owned_operation

if TYPE_CHECKING:
    from ..backends.sdk import OpenSandboxBackend
    from ._workspace_access import _ProjectCoordinator

_KeyT = TypeVar("_KeyT")


class _PreparedDirectory(BaseModel):
    model_config = ConfigDict(strict=True)
    status: Literal["prepared", "unchanged"]
    operation_id: str | None = None
    incarnation: str | None = None
    stage: str | None = None
    root: str | None = None


def _manifest(directory: WorkspaceDirectoryContents) -> dict[str, JsonValue]:
    return {
        name: hashlib.sha256(content).hexdigest() for name, content in directory.files
    }


def _prepared(value: JsonValue, project: str, operation_id: str) -> _PreparedDirectory:
    try:
        result = _PreparedDirectory.model_validate(value)
    except ValidationError as error:
        raise OpenSandboxBackendProtocolError(
            "Workspace preparation returned invalid data", cause=error
        ) from error
    if result.status == "unchanged":
        return result
    prefix = "/var/lib/tinkerfin-workspaces"
    if (
        result.operation_id != operation_id
        or result.incarnation is None
        or result.stage != f"{prefix}/directory-staging/{operation_id}"
        or result.root != f"{prefix}/projects/{project}/{result.incarnation}/files"
        or ".." in PurePosixPath(result.root or "").parts
    ):
        raise OpenSandboxBackendProtocolError(
            "Workspace preparation returned unrelated ownership"
        )
    return result


async def _publish(
    coordinator: _ProjectCoordinator[_KeyT],
    parent: OpenSandboxBackend,
    directory: WorkspaceDirectoryContents,
    files: dict[str, JsonValue],
) -> bool:
    from ._workspace_access import _finish_cleanup

    operation_id = str(uuid4())
    unchanged = False
    primary: BaseException | None = None
    try:
        prepared = _prepared(
            await coordinator._registry(
                parent,
                "managed_begin",
                {"operation_id": operation_id, "path": directory.path, "files": files},
            ),
            coordinator._project,
            operation_id,
        )
        unchanged = prepared.status == "unchanged"
        if unchanged:
            return False
        assert prepared.stage is not None
        for name, content in directory.files:
            path = f"{prepared.stage}/upload/{name}"
            responses = await parent.aupload_files([(path, content)])
            if (
                len(responses) != 1
                or responses[0].error is not None
                or responses[0].path != path
            ):
                raise OpenSandboxBackendProtocolError(
                    "Workspace directory upload failed"
                )
        await coordinator._registry(
            parent, "managed_commit", {"operation_id": operation_id}
        )
        return True
    except BaseException as error:
        primary = error
        raise
    finally:
        if not unchanged:
            # End fences the known identity even if begin lost its response. The
            # native operation lock refuses to unseal an unfinished publication.
            async def finish() -> None:
                result = await coordinator._registry(
                    parent, "maintenance_end", {"operation_id": operation_id}
                )
                if result is not None:
                    raise OpenSandboxBackendProtocolError(
                        "Workspace cleanup returned an unexpected result"
                    )

            await _finish_cleanup(finish(), primary=primary)


async def synchronize(
    coordinator: _ProjectCoordinator[_KeyT],
    directory: WorkspaceDirectoryContents,
    *,
    create: bool,
) -> bool:
    """Own one bounded publication through completion, failure and cancellation.

    Uploads target private staging. Only the native commit touches project files;
    it requires empty execution admission and never follows symbolic links.
    The parent remains borrowed throughout cleanup. Caller cancellation waits for
    the accepted operation, then propagates without abandoning its remote writes.
    """
    async with coordinator._manager._workspace_read_slots:
        files = await run_owned_operation(
            asyncio.to_thread(_manifest, directory),
            task_name="tinkerfin-workspace-content-hash",
        )
        async with coordinator._parent(allow_create=create) as borrowed:
            if borrowed is None:
                raise OpenSandboxWorkspaceNotInitializedError(
                    "The project has no existing sandbox"
                )
            _, parent = borrowed
            if create:
                await coordinator._registry(parent, "prepare", {})
            return await run_owned_operation(
                _publish(coordinator, parent, directory, files),
                task_name="tinkerfin-workspace-directory-publication",
            )
