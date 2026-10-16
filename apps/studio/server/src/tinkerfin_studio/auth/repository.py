"""用户与访问令牌仓储"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.auth.models import User


class UserRepository:
    """用户数据访问"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_username(self, username: str) -> User | None:
        """按登录用户名读取用户"""

        return await self._session.scalar(select(User).where(User.username == username))

    async def get_by_id(self, user_id: int) -> User | None:
        """按主键读取用户"""

        return cast(User | None, await self._session.get(User, user_id))

    async def commit(self) -> None:
        """提交用户写入或结束已物化用户事实的只读事务"""

        await self._session.commit()


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """一条有明确到期时间的访问令牌记录"""

    token: str
    user_id: int
    expires_at: datetime
    revoked: bool = False

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


class TokenRepository(Protocol):
    """认证服务与分布式令牌存储之间的稳定边界"""

    async def save(self, record: TokenRecord) -> None: ...

    async def get(self, token: str) -> TokenRecord | None: ...

    async def revoke(self, token: str) -> None: ...


class RedisTokenRepository:
    """使用应用独占前缀保存访问令牌"""

    def __init__(self, client: Redis, *, key_prefix: str) -> None:
        self._client = client
        self._key_prefix = key_prefix.rstrip(":")

    def _key(self, token: str) -> str:
        return f"{self._key_prefix}:token:{token}"

    async def save(self, record: TokenRecord) -> None:
        """按剩余有效期保存令牌"""

        ttl = math.ceil((record.expires_at - datetime.now(UTC)).total_seconds())
        if ttl <= 0:
            await self._client.delete(self._key(record.token))
            return
        await self._client.set(
            self._key(record.token),
            json.dumps(
                {
                    "user_id": record.user_id,
                    "expires_at": record.expires_at.isoformat(),
                    "revoked": record.revoked,
                },
                separators=(",", ":"),
            ).encode(),
            ex=ttl,
        )

    async def get(self, token: str) -> TokenRecord | None:
        """读取并严格解析令牌记录"""

        payload = await self._client.get(self._key(token))
        if payload is None:
            return None
        try:
            decoded = json.loads(bytes(payload))
            expires_at = datetime.fromisoformat(decoded["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            return TokenRecord(
                token=token,
                user_id=int(decoded["user_id"]),
                expires_at=expires_at,
                revoked=bool(decoded["revoked"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def revoke(self, token: str) -> None:
        """保留原 TTL 并幂等标记令牌已撤销"""

        record = await self.get(token)
        if record is None:
            return
        await self._client.set(
            self._key(token),
            json.dumps(
                {
                    "user_id": record.user_id,
                    "expires_at": record.expires_at.isoformat(),
                    "revoked": True,
                },
                separators=(",", ":"),
            ).encode(),
            xx=True,
            keepttl=True,
        )
