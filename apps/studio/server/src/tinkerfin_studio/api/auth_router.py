"""登录、当前用户和登出路由"""

from fastapi import APIRouter

from tinkerfin_studio.api.dependencies import (
    AuthServiceDep,
    AuthSessionDep,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.schemas import (
    AuthSessionRead,
    LoginRequest,
    LoginResponse,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", response_model=ApiResponse[LoginResponse], summary="登录")
async def login(
    form: LoginRequest,
    auth_service: AuthServiceDep,
) -> ApiResponse[LoginResponse]:
    """校验用户名密码并返回访问令牌"""

    result = await auth_service.login(form.username, form.password)
    return ApiResponse.success(
        LoginResponse(
            access_token=result.access_token,
            expires_at=result.expires_at,
            user=UserRead.from_context(result.user),
        )
    )


@router.get("/me", response_model=ApiResponse[AuthSessionRead], summary="当前会话")
async def me(auth_session: AuthSessionDep) -> ApiResponse[AuthSessionRead]:
    """返回当前登录用户及固定到期时间"""

    return ApiResponse.success(AuthSessionRead.from_context(auth_session))


@router.post("/logout", response_model=ApiResponse[None], summary="登出")
async def logout(
    auth_session: AuthSessionDep,
    auth_service: AuthServiceDep,
) -> ApiResponse[None]:
    """撤销当前访问令牌"""

    await auth_service.logout(auth_session.token)
    return ApiResponse.success()
