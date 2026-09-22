"""浏览器从任意来源直连 Studio 的请求契约"""

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from test_auth_service import TokenMemoryStore

from tinkerfin_studio.api.dependencies import get_auth_service
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.repository import UserRepository
from tinkerfin_studio.auth.service import AuthService


async def test_browser_origin_preflight_and_http_error(
    session: AsyncSession,
) -> None:
    """任意来源可发送认证和自定义请求头，普通响应与错误响应均开放跨源"""
    origin = "https://unconfigured.example"
    app = create_application(lifespan=None)
    auth = AuthService(
        UserRepository(session), TokenMemoryStore(), token_expire_seconds=60
    )
    app.dependency_overrides[get_auth_service] = lambda: auth
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://server"
    ) as client:
        preflight = await client.options(
            "/api/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization,last-event-id,x-client",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        assert "x-client" in preflight.headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in preflight.headers
        for route, status in (
            ("/health/live", 200),
            ("/missing", 404),
            ("/api/auth/me", 401),
        ):
            response = await client.get(route, headers={"Origin": origin})
            assert response.status_code == status
            assert response.headers["access-control-allow-origin"] == "*"
            assert "access-control-allow-credentials" not in response.headers
