from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.passwords import hash_password, verify_password
from tinkerfin_studio.auth.repository import TokenRecord, UserRepository
from tinkerfin_studio.auth.service import AuthService


class TokenMemoryStore:
    """仅保存测试令牌的隔离仓储"""

    def __init__(self) -> None:
        self.records: dict[str, TokenRecord] = {}

    async def save(self, record: TokenRecord) -> None:
        self.records[record.token] = record

    async def get(self, token: str) -> TokenRecord | None:
        return self.records.get(token)

    async def revoke(self, token: str) -> None:
        record = self.records.get(token)
        if record is not None:
            self.records[token] = record.with_revoked(True)


async def test_password_hash_uses_independent_salts() -> None:
    """相同密码不得生成可关联的固定哈希"""

    first = await hash_password("correct horse battery staple")
    second = await hash_password("correct horse battery staple")

    assert first != second
    assert await verify_password("correct horse battery staple", first) is True
    assert await verify_password("wrong", first) is False


async def test_login_resolve_and_logout_use_one_token_record(
    session: AsyncSession,
    fixed_utc_time: datetime,
) -> None:
    """登录签发的 token 应可解析，并在登出后立即失效"""

    user = User(
        username="alice",
        password_hash=await hash_password("secret-pass"),
        roles=["admin"],
        disabled=False,
    )
    session.add(user)
    await session.commit()
    tokens = TokenMemoryStore()
    service = AuthService(
        UserRepository(session),
        tokens,
        token_expire_seconds=1800,
    )

    issued_after = fixed_utc_time
    login = await service.login("alice", "secret-pass")
    resolved = await service.resolve_token(login.access_token)
    assert session.in_transaction() is False
    await service.logout(login.access_token)
    revoked = await service.resolve_token(login.access_token)

    assert login.expires_at == issued_after + timedelta(seconds=1800)
    assert login.user.username == "alice"
    assert resolved.is_authenticated is True
    assert resolved.user == login.user
    assert resolved.expires_at == login.expires_at
    assert revoked.is_authenticated is False
    assert revoked.failure_reason == "revoked_token"
    assert tokens.records[login.access_token].expires_at == login.expires_at


async def test_login_releases_the_user_transaction_before_password_and_redis(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PBKDF2 与 Redis token 写入不得占用业务数据库连接"""

    user = User(
        username="transaction-user",
        password_hash="stored-hash",
        roles=[],
        disabled=False,
    )
    session.add(user)
    await session.commit()

    async def verify_without_transaction(password: str, password_hash: str) -> bool:
        assert (password, password_hash) == ("secret-pass", "stored-hash")
        assert session.in_transaction() is False
        return True

    class TransactionCheckingTokens(TokenMemoryStore):
        async def save(self, record: TokenRecord) -> None:
            assert session.in_transaction() is False
            await super().save(record)

    monkeypatch.setattr(
        "tinkerfin_studio.auth.service.verify_password",
        verify_without_transaction,
    )
    tokens = TransactionCheckingTokens()
    service = AuthService(
        UserRepository(session),
        tokens,
        token_expire_seconds=1800,
    )

    login = await service.login("transaction-user", "secret-pass")

    assert login.user.username == "transaction-user"
    assert session.in_transaction() is False


async def test_expired_token_is_rejected_without_changing_its_deadline(
    session: AsyncSession,
    fixed_utc_time: datetime,
) -> None:
    """固定到期的 token 在重复校验时不得续期"""

    expires_at = fixed_utc_time - timedelta(seconds=1)
    tokens = TokenMemoryStore()
    tokens.records["expired-token"] = TokenRecord(
        token="expired-token",
        user_id=1,
        expires_at=expires_at,
    )
    service = AuthService(
        UserRepository(session),
        tokens,
        token_expire_seconds=86400,
    )

    first = await service.resolve_token("expired-token")
    second = await service.resolve_token("expired-token")

    assert first.is_authenticated is False
    assert first.failure_reason == "expired_token"
    assert second.failure_reason == "expired_token"
    assert tokens.records["expired-token"].expires_at == expires_at


async def test_login_hides_bad_username_and_bad_password_difference(
    session: AsyncSession,
) -> None:
    """未知用户和错误密码应返回同一个安全业务错误"""

    user = User(
        username="alice",
        password_hash=await hash_password("secret-pass"),
        roles=[],
        disabled=False,
    )
    session.add(user)
    await session.commit()
    service = AuthService(
        UserRepository(session),
        TokenMemoryStore(),
        token_expire_seconds=1800,
    )

    failures: list[tuple[int, str]] = []
    for username, password in (("missing", "secret-pass"), ("alice", "wrong")):
        with pytest.raises(BusinessException) as caught:
            await service.login(username, password)
        failures.append((int(caught.value.error_code), caught.value.message))

    assert failures == [
        (1_001_001_000, "用户名或密码错误"),
        (1_001_001_000, "用户名或密码错误"),
    ]
