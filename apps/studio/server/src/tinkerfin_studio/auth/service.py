"""认证业务服务"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError

from tinkerfin_studio.api.errors import (
    AuthErrorCode,
    BusinessException,
    SystemException,
)
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.passwords import verify_password
from tinkerfin_studio.auth.repository import (
    TokenRecord,
    TokenRepository,
    UserRepository,
)
from tinkerfin_studio.auth.types import RequestAuthState, UserContext

_DUMMY_PASSWORD_HASH = (
    "$pbkdf2-sha256$600000$AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """成功登录后返回给 HTTP 边界的结果"""

    access_token: str
    expires_at: datetime
    user: UserContext


class AuthService:
    """校验用户并管理分布式访问令牌"""

    def __init__(
        self,
        user_repository: UserRepository,
        token_repository: TokenRepository,
        *,
        token_expire_seconds: int,
    ) -> None:
        self._users = user_repository
        self._tokens = token_repository
        self._token_expire_seconds = token_expire_seconds

    async def login(self, username: str, password: str) -> LoginResult:
        """校验用户名密码并签发访问令牌"""

        user = await self._users.get_by_username(username)
        user_context = None if user is None else self._context(user)
        password_hash = _DUMMY_PASSWORD_HASH if user is None else user.password_hash
        # 密码计算与 Redis 不占用只读用户查询的数据库事务
        await self._users.commit()
        password_is_valid = await verify_password(
            password,
            password_hash,
        )
        if user_context is None or not password_is_valid:
            raise BusinessException(AuthErrorCode.BAD_CREDENTIALS)
        if user_context.disabled:
            raise BusinessException(AuthErrorCode.USER_DISABLED)

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._token_expire_seconds)
        try:
            await self._tokens.save(
                TokenRecord(
                    token=token,
                    user_id=user_context.user_id,
                    expires_at=expires_at,
                )
            )
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error
        return LoginResult(
            access_token=token,
            expires_at=expires_at,
            user=user_context,
        )

    async def resolve_token(self, token: str | None) -> RequestAuthState:
        """解析令牌并返回完整鉴权状态"""

        if token is None:
            return RequestAuthState()
        try:
            record = await self._tokens.get(token)
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error
        if record is None:
            return RequestAuthState(token=token, failure_reason="invalid_token")
        if record.revoked:
            return RequestAuthState(token=token, failure_reason="revoked_token")
        if record.is_expired:
            return RequestAuthState(token=token, failure_reason="expired_token")
        user = await self._users.get_by_id(record.user_id)
        if user is None:
            await self._users.commit()
            return RequestAuthState(token=token, failure_reason="user_not_found")
        user_context = self._context(user)
        await self._users.commit()
        if user_context.disabled:
            return RequestAuthState(token=token, failure_reason="disabled_user")
        return RequestAuthState(
            token=token,
            is_authenticated=True,
            user=user_context,
            expires_at=record.expires_at,
        )

    async def logout(self, token: str) -> None:
        """幂等撤销访问令牌"""

        try:
            await self._tokens.revoke(token)
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error

    async def get_user(self, user_id: int) -> UserContext | None:
        """按用户 ID 返回安全上下文"""

        user = await self._users.get_by_id(user_id)
        user_context = None if user is None else self._context(user)
        await self._users.commit()
        return user_context

    async def save_avatar(self, user_id: int, avatar_url: str) -> UserContext | None:
        """保存已成功上传到对象存储的头像地址"""

        user = await self._users.get_by_id(user_id)
        if user is None:
            return None
        user.avatar_url = avatar_url
        await self._users.commit()
        return self._context(user)

    @staticmethod
    def _context(user: User) -> UserContext:
        return UserContext(
            user_id=user.id,
            username=user.username,
            roles=tuple(user.roles),
            disabled=user.disabled,
            avatar_url=user.avatar_url,
        )
