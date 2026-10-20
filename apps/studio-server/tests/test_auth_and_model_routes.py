from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from tinkerfin_studio.api.dependencies import (
    get_auth_service,
    get_auth_session,
    get_model_service,
    get_user_context,
)
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.service import LoginResult
from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelCatalogItem,
)


class RouteAuthService:
    """路由测试使用的可观察认证服务"""

    def __init__(self, user: UserContext) -> None:
        self.user = user
        self.logged_out: list[str] = []

    async def login(self, username: str, password: str) -> LoginResult:
        assert (username, password) == ("alice", "secret")
        return LoginResult(
            access_token="token-1",
            expires_at=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
            user=self.user,
        )

    async def logout(self, token: str) -> None:
        self.logged_out.append(token)


class RouteModelService:
    """路由测试使用的安全模型目录服务"""

    async def list_catalog(self) -> AgentModelCatalog:
        return AgentModelCatalog(
            items=[
                AgentModelCatalogItem(
                    modelId="main",
                    displayName="Main Model",
                    connectionId="main-provider",
                    connectionDisplayName="主提供方",
                    reasoningEnabled=True,
                    isDefault=True,
                )
            ],
            defaultModelId="main",
        )


async def test_auth_and_model_routes_keep_the_public_contract() -> None:
    """认证与模型目录应使用统一包络且不暴露模型密钥"""

    user = UserContext(
        user_id=7,
        username="alice",
        avatar_url="https://cdn.example.test/avatars/alice.webp",
        roles=("admin",),
        disabled=False,
    )
    auth = RouteAuthService(user)
    auth_session = AuthenticatedSession(
        token="token-1",
        expires_at=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
        user=user,
    )
    application = create_application(lifespan=None)
    application.dependency_overrides[get_auth_service] = lambda: auth
    application.dependency_overrides[get_auth_session] = lambda: auth_session
    application.dependency_overrides[get_user_context] = lambda: user
    application.dependency_overrides[get_model_service] = RouteModelService

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "secret"},
        )
        me = await client.get("/api/auth/me")
        models = await client.get("/api/models")
        logout = await client.post(
            "/api/auth/logout",
            headers={"Authorization": "Bearer token-1"},
        )

    assert login.json()["data"] == {
        "access_token": "token-1",
        "token_type": "Bearer",
        "expires_at": "2026-08-23T10:00:00Z",
        "user": {
            "user_id": 7,
            "username": "alice",
            "avatar_url": "https://cdn.example.test/avatars/alice.webp",
            "roles": ["admin"],
            "disabled": False,
        },
    }
    assert me.json()["data"] == {
        "expires_at": "2026-08-23T10:00:00Z",
        "user": login.json()["data"]["user"],
    }
    assert models.json()["data"] == {
        "items": [
            {
                "modelId": "main",
                "displayName": "Main Model",
                "connectionId": "main-provider",
                "connectionDisplayName": "主提供方",
                "reasoningEnabled": True,
                "isDefault": True,
            }
        ],
        "defaultModelId": "main",
    }
    assert "api_key" not in models.text
    assert logout.json() == {"code": 0, "message": "success", "data": None}
    assert auth.logged_out == ["token-1"]
