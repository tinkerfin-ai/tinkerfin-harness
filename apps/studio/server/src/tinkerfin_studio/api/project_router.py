"""当前用户项目的创建、选择与重命名入口"""

from fastapi import APIRouter, Request

from tinkerfin_studio.api.dependencies import SessionDep, UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.projects.schemas import ProjectName, ProjectView
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/projects", tags=["项目"])


@router.get("")
async def list_projects(
    session: SessionDep, user: UserContextDep
) -> ApiResponse[list[ProjectView]]:
    """返回当前用户可选项目，空列表由界面引导创建"""
    return ApiResponse.success(
        [
            ProjectView.model_validate(item)
            for item in await ProjectRepository(session, user.user_id).list()
        ]
    )


@router.post("")
async def create_project(
    body: ProjectName, request: Request, session: SessionDep, user: UserContextDep
) -> ApiResponse[ProjectView]:
    """创建业务项目，不提前创建空会话或沙箱"""
    item = await ProjectRepository(session, user.user_id).create(body.name)
    await notify_change(
        get_resources(request.app).notifications,
        user_id=user.user_id,
        topic="studio.projects.changed",
        key=item.id,
    )
    return ApiResponse.success(ProjectView.model_validate(item))


@router.patch("/{project_id}")
async def rename_project(
    project_id: str,
    body: ProjectName,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> ApiResponse[ProjectView]:
    """修改名称，不改变项目资源和运行归属"""
    item = await ProjectRepository(session, user.user_id).rename(project_id, body.name)
    await notify_change(
        get_resources(request.app).notifications,
        user_id=user.user_id,
        topic="studio.projects.changed",
        key=item.id,
    )
    return ApiResponse.success(ProjectView.model_validate(item))
