"""当前用户头像上传接口"""

from uuid import uuid4

import anyio
from anyio.lowlevel import checkpoint
from fastapi import APIRouter, Request

from tinkerfin_studio.api.dependencies import AuthServiceDep, UserContextDep
from tinkerfin_studio.api.errors import BusinessException, GlobalErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.avatars import MAX_AVATAR_BYTES, prepare_avatar
from tinkerfin_studio.auth.schemas import UserRead
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/user", tags=["用户"])


@router.put(
    "/me/avatar", response_model=ApiResponse[UserRead], summary="上传当前用户头像"
)
async def upload_avatar(
    request: Request,
    current_user: UserContextDep,
    auth_service: AuthServiceDep,
) -> ApiResponse[UserRead]:
    """接收图片原始字节，完成校验后才替换当前用户头像"""

    content = bytearray()
    try:
        with anyio.fail_after(30):
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_AVATAR_BYTES:
                    raise BusinessException(
                        GlobalErrorCode.VALIDATION_FAILED,
                        message="请选择不超过 5 MiB 的头像图片",
                    )
                content.extend(chunk)
    except TimeoutError as error:
        raise BusinessException(
            GlobalErrorCode.BAD_REQUEST, message="头像上传超时，请重试"
        ) from error
    image = await prepare_avatar(bytes(content))
    await checkpoint()
    try:
        avatar_url = await get_resources(request.app).object_storage.upload_avatar(
            uuid4().hex, image
        )
    except (OSError, TimeoutError) as error:
        raise BusinessException(
            GlobalErrorCode.SERVICE_UNAVAILABLE, message="头像上传失败，请重试"
        ) from error
    await checkpoint()
    user = await auth_service.save_avatar(current_user.user_id, avatar_url)
    if user is None:
        raise BusinessException(GlobalErrorCode.UNAUTHORIZED)
    return ApiResponse.success(UserRead.from_context(user))
