"""已认证用户的技能入口，业务编排和提交统一由技能库负责"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.dependencies import SessionDep, UserContextDep
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SkillErrorCode,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.resources import get_resources
from tinkerfin_studio.skills.packages import MAX_ARCHIVE_BYTES
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import (
    ConfirmImportRequest,
    GitHubImportRequest,
    ImportPreview,
    InstalledSkill,
    InstallSkillRequest,
    RemoteSkillDetail,
    RemoteSkillPage,
    SelectedSkill,
    SkillChangeResult,
    SkillCommandRequest,
    SkillDetail,
    SkillEnabledRequest,
    SkillSourceInfo,
    UpdateSkillRequest,
)

router = APIRouter(prefix="/skills", tags=["技能"])


@router.get("/selection")
async def run_selection(
    user: UserContextDep,
    session: SessionDep,
    thread_id: Annotated[str, Query(min_length=1, max_length=128)],
    run_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> ApiResponse[list[SelectedSkill]]:
    """重试提问时读取本人原运行的选择，不依赖当前安装状态"""
    conversations = ConversationRepository(session)
    thread = await conversations.get_thread(user_id=user.user_id, thread_id=thread_id)
    if thread is None:
        raise BusinessException(ConversationErrorCode.NOT_FOUND)
    if await conversations.get_run(thread_pk=thread.id, run_id=run_id) is None:
        raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
    snapshot = await SkillRepository(session, user.user_id).snapshot(
        RunIdentity(namespace=f"ns_{user.user_id}", thread_id=thread_id, run_id=run_id)
    )
    return ApiResponse.success(
        [
            SelectedSkill(id=skill.installation_id, name=skill.name)
            for skill in snapshot.skills
            if skill.selected
        ]
    )


@router.get("/sources")
async def sources(
    request: Request, user: UserContextDep
) -> ApiResponse[list[SkillSourceInfo]]:
    return ApiResponse.success(get_resources(request.app).skills.sources())


@router.get("/catalog")
async def catalog(
    request: Request,
    user: UserContextDep,
    source_id: Annotated[str, Query(min_length=1, max_length=64)],
    q: Annotated[str, Query(max_length=256)] = "",
    cursor: Annotated[str | None, Query(max_length=8192)] = None,
) -> ApiResponse[RemoteSkillPage]:
    return ApiResponse.success(
        await get_resources(request.app).skills.search(
            source_id, query=q, cursor=cursor
        )
    )


@router.get("/catalog/detail")
async def remote_detail(
    request: Request,
    user: UserContextDep,
    source_id: Annotated[str, Query(min_length=1, max_length=64)],
    skill_id: Annotated[str, Query(min_length=1, max_length=256)],
    revision: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> ApiResponse[RemoteSkillDetail]:
    return ApiResponse.success(
        await get_resources(request.app).skills.remote_detail(
            source_id, skill_id, revision
        )
    )


@router.get("/installations")
async def installations(
    request: Request, user: UserContextDep
) -> ApiResponse[list[InstalledSkill]]:
    return ApiResponse.success(
        await get_resources(request.app).skills.list(user.user_id)
    )


@router.post("/installations")
async def install(
    payload: InstallSkillRequest, request: Request, user: UserContextDep
) -> ApiResponse[InstalledSkill]:
    result = await get_resources(request.app).skills.install(user.user_id, payload)
    return ApiResponse.success(result.installation)


@router.get("/installations/{installation_id}")
async def installed_detail(
    installation_id: str, request: Request, user: UserContextDep
) -> ApiResponse[SkillDetail]:
    return ApiResponse.success(
        await get_resources(request.app).skills.detail(user.user_id, installation_id)
    )


@router.patch("/installations/{installation_id}")
async def set_enabled(
    installation_id: str,
    payload: SkillEnabledRequest,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[InstalledSkill]:
    result = await get_resources(request.app).skills.set_enabled(
        user.user_id, installation_id, payload
    )
    return ApiResponse.success(result.installation)


@router.delete("/installations/{installation_id}")
async def uninstall(
    installation_id: str,
    payload: SkillCommandRequest,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[None]:
    await get_resources(request.app).skills.uninstall(
        user.user_id, installation_id, payload
    )
    return ApiResponse.success()


@router.post("/installations/{installation_id}/update")
async def update_skill(
    installation_id: str,
    payload: UpdateSkillRequest,
    request: Request,
    user: UserContextDep,
) -> ApiResponse[SkillChangeResult]:
    return ApiResponse.success(
        await get_resources(request.app).skills.update(
            user.user_id, installation_id, payload
        )
    )


@router.post("/imports/github")
async def github_preview(
    payload: GitHubImportRequest, request: Request, user: UserContextDep
) -> ApiResponse[ImportPreview]:
    return ApiResponse.success(
        await get_resources(request.app).skills.preview_github(
            user.user_id, payload.url
        )
    )


@router.post("/imports/zip")
async def zip_preview(
    request: Request, user: UserContextDep
) -> ApiResponse[ImportPreview]:
    """传输层有界接收 ZIP，解析、校验及预览保存交给技能服务"""
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ARCHIVE_BYTES:
            raise BusinessException(SkillErrorCode.TOO_LARGE)
        content.extend(chunk)
    return ApiResponse.success(
        await get_resources(request.app).skills.preview_zip(
            user.user_id, bytes(content)
        )
    )


@router.post("/imports/{draft_id}/confirm")
async def confirm_import(
    draft_id: str, payload: ConfirmImportRequest, request: Request, user: UserContextDep
) -> ApiResponse[list[str]]:
    result = await get_resources(request.app).skills.confirm(
        user.user_id, draft_id, payload
    )
    return ApiResponse.success(result.installation_ids)
