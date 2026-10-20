"""认证和用户 HTTP 边界模型"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext


class UserRead(BaseModel):
    """可通过接口返回的用户信息"""

    model_config = ConfigDict(from_attributes=True)

    user_id: int = Field(ge=1, description="用户 ID")
    username: str = Field(description="登录用户名")
    avatar_url: str | None = Field(
        description="头像的长期对象存储地址；空值由前端显示默认头像"
    )
    roles: list[str] = Field(default_factory=list, description="用户角色列表")
    disabled: bool = Field(description="是否禁止登录")

    @classmethod
    def from_context(cls, context: UserContext) -> "UserRead":
        """从可信用户上下文构造响应"""

        return cls.model_validate(context)


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(min_length=1, max_length=64, description="登录用户名")
    password: str = Field(min_length=1, max_length=1024, description="明文登录密码")


class AuthSessionRead(BaseModel):
    """后端已确认且具有固定到期时间的登录会话"""

    expires_at: datetime = Field(description="访问令牌的 UTC 固定到期时间")
    user: UserRead = Field(description="当前登录用户")

    @classmethod
    def from_context(cls, context: AuthenticatedSession) -> "AuthSessionRead":
        """从可信认证会话构造响应"""

        return cls(
            expires_at=context.expires_at,
            user=UserRead.from_context(context.user),
        )


class LoginResponse(AuthSessionRead):
    """登录成功响应"""

    access_token: str = Field(description="访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
