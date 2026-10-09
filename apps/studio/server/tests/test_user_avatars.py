"""头像上传、内容限制和当前地址有效性的 HTTP 契约"""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect, Request
from starlette.types import Message
from test_auth_service import TokenMemoryStore

from tinkerfin_studio.api.dependencies import get_auth_service, get_user_context
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.api.user_router import upload_avatar
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.avatars import MAX_AVATAR_BYTES, prepare_avatar
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.repository import UserRepository
from tinkerfin_studio.auth.service import AuthService


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 128), "blue").save(output, "PNG")
    return output.getvalue()


async def test_avatar_upload_saves_only_the_oss_url(session: AsyncSession) -> None:
    owner = User(username="owner", password_hash="hash", roles=[], disabled=False)
    other = User(username="other", password_hash="hash", roles=[], disabled=False)
    session.add_all([owner, other])
    await session.commit()
    service = AuthService(
        UserRepository(session), TokenMemoryStore(), token_expire_seconds=1800
    )
    context = await service.get_user(owner.id)
    storage = SimpleNamespace(
        upload_avatar=AsyncMock(return_value="https://files.example/avatars/first.jpg")
    )
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(object_storage=storage)
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_user_context] = lambda: context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.put(
            "/api/user/me/avatar", content=_png(), headers={"Content-Type": "image/png"}
        )
        assert first.status_code == 200
        user = first.json()["data"]
        assert set(user) == {"user_id", "username", "avatar_url", "roles", "disabled"}
        assert user["username"] == "owner"
        assert (
            user["avatar_url"]
            == owner.avatar_url
            == "https://files.example/avatars/first.jpg"
        )
        image_id, image = storage.upload_avatar.call_args.args
        assert len(image_id) == 32
        with Image.open(io.BytesIO(image)) as content:
            assert content.size == (256, 64)
            assert content.format == "JPEG"
        assert (
            await client.put("/api/user/me/avatar", content=b"broken")
        ).status_code == 422
        assert (
            await client.put(
                "/api/user/me/avatar", content=b"a" * (MAX_AVATAR_BYTES + 1)
            )
        ).status_code == 422
        assert storage.upload_avatar.await_count == 1
        for field in ("username", "display_name"):
            assert (
                await client.put("/api/user/me/avatar", json={field: "changed"})
            ).status_code == 422
        storage.upload_avatar.side_effect = OSError("offline")
        assert (
            await client.put("/api/user/me/avatar", content=_png())
        ).status_code == 503
        assert owner.avatar_url == "https://files.example/avatars/first.jpg"
        storage.upload_avatar.side_effect = None
        storage.upload_avatar.return_value = "https://files.example/avatars/second.jpg"
        second = await client.put("/api/user/me/avatar", content=_png())
        assert (
            second.json()["data"]["avatar_url"]
            == owner.avatar_url
            == "https://files.example/avatars/second.jpg"
        )
        assert other.avatar_url is None
        assert {
            column.name
            for column in User.__table__.columns
            if column.name.startswith("avatar_")
        } == {"avatar_url"}
        assert {
            path for path in app.openapi()["paths"] if path.startswith("/api/user/")
        } == {"/api/user/{user_id}", "/api/user/me/avatar"}


@pytest.mark.parametrize(
    "data", [b"", b"invalid", b"<svg></svg>", b"a" * (MAX_AVATAR_BYTES + 1)]
)
async def test_avatar_rejects_invalid_or_unbounded_content(data: bytes) -> None:
    with pytest.raises(BusinessException):
        await prepare_avatar(data)


async def test_avatar_upload_requires_authentication(session: AsyncSession) -> None:
    app = create_application(lifespan=None)
    service = AuthService(
        UserRepository(session), TokenMemoryStore(), token_expire_seconds=1800
    )
    app.dependency_overrides[get_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.put("/api/user/me/avatar", content=_png())
        ).status_code == 401


@pytest.mark.parametrize(
    "failure", [TimeoutError, ClientDisconnect, asyncio.CancelledError]
)
async def test_interrupted_upload_never_replaces_the_saved_avatar(
    session: AsyncSession, failure: type[BaseException]
) -> None:
    user = User(
        username="owner",
        password_hash="hash",
        roles=[],
        disabled=False,
        avatar_url="https://example.test/avatar.jpg",
    )
    session.add(user)
    await session.commit()
    service = AuthService(
        UserRepository(session), TokenMemoryStore(), token_expire_seconds=1800
    )
    context = await service.get_user(user.id)
    assert context is not None

    async def receive() -> Message:
        raise failure()

    request = Request({"type": "http", "method": "PUT", "headers": []}, receive=receive)
    with pytest.raises(BusinessException if failure is TimeoutError else failure):
        await upload_avatar(request, context, service)
    assert (await service.get_user(user.id)) == context
