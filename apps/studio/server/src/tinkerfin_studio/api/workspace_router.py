"""当前用户项目工作区的只读目录、源码预览和文件变化入口"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from tinkerfin_gateway import NotificationStream
from tinkerfin_gateway.starlette import SseResponse, sse_response
from tinkerfin_sandbox import (
    OpenSandboxError,
    OpenSandboxFileChangedError,
    OpenSandboxPausedError,
    OpenSandboxWorkspaceNotInitializedError,
)
from tinkerfin_studio.api.dependencies import AuthSessionDep, SessionDep, UserContextDep
from tinkerfin_studio.api.errors import (
    BusinessException,
    GlobalErrorCode,
    WorkspaceErrorCode,
)
from tinkerfin_studio.api.network_operation import connected_operation
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.api.session_access import session_has_access
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import get_resources
from tinkerfin_studio.workspace.schemas import (
    WorkspaceDirectoryView,
    WorkspaceFileView,
    WorkspacePreviewView,
)
from tinkerfin_studio.workspace.service import WorkspaceFileService


class _WorkspaceRoute(APIRoute):
    """工作区查询的成功与错误结果均不可缓存"""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 全局异常处理器在路由外创建响应，通过请求状态保留相同缓存策略
        scope.setdefault("state", {})["private_no_store"] = True

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        await super().handle(scope, receive, no_store_send)


router = APIRouter(
    prefix="/projects/{project_id}/workspace",
    tags=["工作区"],
    route_class=_WorkspaceRoute,
    responses={
        status: {
            "description": description,
            "content": {
                "application/json": {"schema": ApiResponse[None].model_json_schema()}
            },
        }
        for status, description in {
            401: "登录已失效",
            403: "文件或目录不可读取",
            404: "项目或条目不存在",
            409: "工作区未初始化、已暂停或文件已变化",
            422: "请求参数或路径无效",
            503: "工作区暂不可用",
        }.items()
    },
)
ProjectId = Annotated[str, Path(min_length=1, max_length=36)]
FilePath = Annotated[str, Query(min_length=1, max_length=4096)]


def workspace_error(error: Exception) -> BusinessException:
    """只投影可公开的业务状态，不传递底层路径或诊断信息"""
    if isinstance(error, OpenSandboxWorkspaceNotInitializedError):
        code = WorkspaceErrorCode.NOT_INITIALIZED
    elif isinstance(error, OpenSandboxPausedError):
        code = WorkspaceErrorCode.PAUSED
    elif isinstance(error, OpenSandboxFileChangedError):
        code = WorkspaceErrorCode.CHANGED
    elif isinstance(error, FileNotFoundError):
        code = WorkspaceErrorCode.NOT_FOUND
    elif isinstance(error, PermissionError):
        code = WorkspaceErrorCode.FORBIDDEN
    elif isinstance(error, (ValueError, NotADirectoryError)):
        code = WorkspaceErrorCode.INVALID_PATH
    else:
        code = WorkspaceErrorCode.UNAVAILABLE
    return BusinessException(code)


async def project_files(
    project_id: ProjectId,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> AsyncIterator[WorkspaceFileService]:
    """复用 Agent 的用户与项目归属，仅借用不创建资源的读取能力"""
    await ProjectRepository(session, user.user_id).require(project_id)
    await session.commit()
    workspace = get_resources(request.app).sandbox_manager.workspace(
        f"users/{user.user_id}", workspace_key=project_id
    )
    try:
        yield WorkspaceFileService(workspace)
    except (
        OpenSandboxError,
        FileNotFoundError,
        PermissionError,
        NotADirectoryError,
        ValueError,
    ) as error:
        raise workspace_error(error) from error


Files = Annotated[WorkspaceFileService, Depends(project_files, scope="function")]


@router.get("/entries")
async def list_entries(
    request: Request,
    files: Files,
    path: FilePath = "/",
    cursor: Annotated[str | None, Query(max_length=8192)] = None,
) -> ApiResponse[WorkspaceDirectoryView]:
    """展开目录时读取一页，尚无工作区时不创建沙箱"""
    return ApiResponse.success(
        await connected_operation(request, lambda: files.directory(path, cursor))
    )


@router.get("/file")
async def file_info(
    request: Request, files: Files, path: FilePath
) -> ApiResponse[WorkspaceFileView]:
    """检查选中文件是否变化，不读取正文"""
    info = await connected_operation(
        request, lambda: files.workspace.get_file_info(path)
    )
    return ApiResponse.success(WorkspaceFileView.model_validate(info))


@router.get("/preview")
async def preview_file(
    request: Request, files: Files, path: FilePath
) -> ApiResponse[WorkspacePreviewView]:
    """只返回文本片段或文件信息，不提供附件或下载地址"""
    return ApiResponse.success(
        await connected_operation(request, lambda: files.preview(path))
    )


@router.get("/events", response_class=SseResponse)
async def follow_files(
    project_id: ProjectId, request: Request, session: SessionDep, auth: AuthSessionDep
) -> SseResponse:
    """鉴权后跟随当前项目文件变化，连接关闭不影响任务执行"""
    resources = get_resources(request.app)
    await ProjectRepository(session, auth.user.user_id).require(project_id)
    await session.commit()
    workspace = resources.sandbox_manager.workspace(
        f"users/{auth.user.user_id}", workspace_key=project_id
    )

    async def authorized() -> bool:
        return await session_has_access(resources, auth, project_id)

    async def prepare() -> NotificationStream:
        try:
            await workspace.get_file_info("/")
        except (OpenSandboxError, OSError, ValueError) as error:
            raise workspace_error(error) from error
        try:
            return await resources.gateway.resource_changes(
                watch_changes=workspace.watch,
                expires_at=auth.expires_at,
                authorize=authorized,
            )
        except PermissionError as error:
            raise BusinessException(GlobalErrorCode.UNAUTHORIZED) from error
        except (OpenSandboxError, TimeoutError) as error:
            raise workspace_error(error) from error

    return await sse_response(
        prepare(), request=request, headers={"Cache-Control": "private, no-store"}
    )
