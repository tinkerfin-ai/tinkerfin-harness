"""长连接通过短数据库会话复核登录身份和项目归属"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tinkerfin_studio.api.errors import BusinessException, ProjectErrorCode
from tinkerfin_studio.auth.repository import RedisTokenRepository, UserRepository
from tinkerfin_studio.auth.service import AuthService
from tinkerfin_studio.auth.types import AuthenticatedSession
from tinkerfin_studio.infrastructure.redis_keys import AUTH_TOKEN_KEY_PREFIX
from tinkerfin_studio.projects.repository import ProjectRepository

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


async def session_has_access(
    resources: ApplicationResources,
    auth: AuthenticatedSession,
    project_id: str | None = None,
) -> bool:
    """复核完立即归还连接，不让通知监听占用数据库连接"""
    async with asyncio.timeout(5), resources.database.session() as session:
        service = AuthService(
            UserRepository(session),
            RedisTokenRepository(
                resources.redis_runtime, key_prefix=AUTH_TOKEN_KEY_PREFIX
            ),
            token_expire_seconds=resources.settings.auth_token_expire_seconds,
        )
        current = await service.resolve_token(auth.token)
        if (
            not current.is_authenticated
            or current.user is None
            or current.user.user_id != auth.user.user_id
        ):
            return False
        if project_id is not None:
            try:
                await ProjectRepository(session, auth.user.user_id).require(project_id)
            except BusinessException as error:
                if error.error_code is ProjectErrorCode.NOT_FOUND:
                    return False
                raise
        return True
