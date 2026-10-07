"""当前登录用户的资源变化通知"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from tinkerfin_gateway.starlette import SseResponse, sse_response
from tinkerfin_notifications import NotificationError, NotificationScope
from tinkerfin_studio.api.dependencies import AuthSessionDep
from tinkerfin_studio.api.errors import (
    BusinessException,
    GlobalErrorCode,
    SystemException,
)
from tinkerfin_studio.api.session_access import session_has_access
from tinkerfin_studio.automation.ownership import automation_owner
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import get_resources

router = APIRouter(tags=["通知"])


@router.get("/notifications", response_class=SseResponse)
async def follow_notifications(
    request: Request,
    auth: AuthSessionDep,
    project_id: Annotated[
        str | None, Query(alias="projectId", min_length=1, max_length=36)
    ] = None,
) -> SseResponse:
    """通知浏览器重新读取当前用户的会话、附件、轨迹和自动化资源

    作用域来自登录身份；固定到期或权限撤销后结束通知流。
    权限复核各自借用短会话，连接空闲期间不占用数据库连接。
    """
    resources = get_resources(request.app)
    user_id = auth.user.user_id
    scopes = [NotificationScope(f"ns_{user_id}")]
    if project_id is not None:
        async with resources.database.session() as session:
            await ProjectRepository(session, user_id).require(project_id)
        scopes.append(
            NotificationScope(
                "studio_automation", automation_owner(user_id, project_id)
            )
        )

    async def authorized() -> bool:
        return await session_has_access(resources, auth, project_id)

    try:
        return await sse_response(
            resources.gateway.notifications(
                scopes=tuple(scopes),
                expires_at=auth.expires_at,
                authorize=authorized,
            )
        )
    except PermissionError as error:
        raise BusinessException(GlobalErrorCode.UNAUTHORIZED) from error
    except (NotificationError, TimeoutError) as error:
        raise SystemException(GlobalErrorCode.SERVICE_UNAVAILABLE) from error
