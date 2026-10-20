"""认证服务内部值对象"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserContext:
    """请求链使用的可信用户上下文"""

    user_id: int
    username: str
    roles: tuple[str, ...]
    disabled: bool
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """已通过后端校验且保留固定到期时间的请求会话"""

    token: str
    expires_at: datetime
    user: UserContext


@dataclass(frozen=True, slots=True)
class RequestAuthState:
    """访问令牌解析后的请求鉴权状态"""

    token: str | None = None
    is_authenticated: bool = False
    user: UserContext | None = None
    expires_at: datetime | None = None
    failure_reason: str | None = None
