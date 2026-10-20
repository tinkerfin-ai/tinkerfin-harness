"""项目记忆的列表、搜索与条件编辑入口"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from tinkerfin.files import FileConflict, PersistentFiles
from tinkerfin_studio.api.dependencies import SessionDep, UserContextDep
from tinkerfin_studio.api.errors import BusinessException, ProjectErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.projects.memories import (
    MemoryDetail,
    MemoryEtag,
    MemoryItem,
    MemoryPage,
    MemoryPath,
    MemoryUpdate,
    MemoryWrite,
)
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/projects/{project_id}/memories", tags=["记忆"])
ProjectId = Annotated[str, Path(min_length=1, max_length=36)]


async def memory_files(
    project_id: ProjectId,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> AsyncIterator[PersistentFiles]:
    """授权项目后借用与 Agent 相同的文件集合，写入冲突交给页面处理"""
    await ProjectRepository(session, user.user_id).require(project_id)
    await session.commit()
    configured = get_resources(request.app).tinkerfin.with_namespace(
        f"ns_{user.user_id}"
    )
    try:
        yield configured.files(("projects", project_id, "memories"))
    except FileConflict as error:
        raise BusinessException(ProjectErrorCode.MEMORY_CONFLICT) from error
    except FileNotFoundError as error:
        raise BusinessException(ProjectErrorCode.MEMORY_NOT_FOUND) from error
    except ValueError as error:
        raise BusinessException(ProjectErrorCode.MEMORY_INVALID) from error


Files = Annotated[PersistentFiles, Depends(memory_files, scope="function")]


@router.get("")
async def list_memories(
    files: Files,
    query: Annotated[str, Query(max_length=256)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ApiResponse[MemoryPage]:
    """按文件名和文本搜索；每次最多扫描一千个文件，后续范围通过游标继续"""
    items: list[MemoryItem] = []
    normalized = query.strip().casefold()
    position = offset
    for _ in range(10):
        batch = await files.list(limit=100, offset=position)
        for item in batch:
            position += 1
            summary = MemoryItem.from_file(item)
            matches = normalized in item.path.casefold()
            if not matches and summary.editable:
                matches = normalized in item.content.decode("utf-8").casefold()
            if matches:
                items.append(summary)
            if len(items) == limit:
                return ApiResponse.success(
                    MemoryPage(items=items, next_offset=position)
                )
        if len(batch) < 100:
            return ApiResponse.success(MemoryPage(items=items, next_offset=None))
    return ApiResponse.success(MemoryPage(items=items, next_offset=position))


@router.get("/file")
async def read_memory(
    files: Files, path: Annotated[MemoryPath, Query()]
) -> ApiResponse[MemoryDetail]:
    return ApiResponse.success(MemoryDetail.read_file(await files.read(path)))


@router.post("")
async def create_memory(
    project_id: ProjectId,
    body: MemoryWrite,
    files: Files,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[MemoryDetail]:
    saved = await files.create(body.path, body.content.encode("utf-8"))
    await notify_change(
        get_resources(request.app).notifications,
        user_id=user.user_id,
        topic="studio.memories.changed",
        key=project_id,
    )
    return ApiResponse.success(MemoryDetail.read_file(saved))


@router.put("/file")
async def update_memory(
    project_id: ProjectId,
    body: MemoryUpdate,
    files: Files,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[MemoryDetail]:
    saved = await files.update(
        body.path, body.content.encode("utf-8"), expected_etag=body.etag
    )
    await notify_change(
        get_resources(request.app).notifications,
        user_id=user.user_id,
        topic="studio.memories.changed",
        key=project_id,
    )
    return ApiResponse.success(MemoryDetail.read_file(saved))


@router.delete("/file")
async def delete_memory(
    project_id: ProjectId,
    path: Annotated[MemoryPath, Query()],
    etag: Annotated[MemoryEtag, Query()],
    files: Files,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[None]:
    await files.delete(path, expected_etag=etag)
    await notify_change(
        get_resources(request.app).notifications,
        user_id=user.user_id,
        topic="studio.memories.changed",
        key=project_id,
    )
    return ApiResponse.success()
