"""模型发现的目录语义、用户归属及请求资源释放"""

import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError
from sqlalchemy import event

from tinkerfin_studio.api import model_router
from tinkerfin_studio.api.errors import BusinessException, application_exception_handler
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.models.schemas import ModelDiscoveryResult
from tinkerfin_studio.models.transport import (
    ModelEndpointNotAllowed,
    ModelResponseTooLarge,
)


@pytest.fixture
def discovery_app(database, monkeypatch, model_connections):
    app = FastAPI()
    app.include_router(model_router.router)
    app.dependency_overrides[model_router._discovery_user] = lambda: UserContext(
        user_id=1, username="tester", display_name="Tester", roles=(), disabled=False
    )
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )

    async def business_error(request, error):
        return await application_exception_handler(request, error)

    app.add_exception_handler(BusinessException, business_error)
    return app


@pytest.mark.parametrize(
    ("status", "body", "outcome", "code"),
    [
        (
            200,
            {"data": [{"id": "first"}, {"id": "second"}, {"id": "first"}]},
            "success",
            "models_received",
        ),
        (200, {"unexpected": "data"}, "inconclusive", "models_unavailable"),
        (401, {"error": "private-draft-key"}, "inconclusive", "authentication_failed"),
        (403, {}, "inconclusive", "authentication_failed"),
        (429, {}, "inconclusive", "rate_limited"),
        (404, {}, "inconclusive", "models_unavailable"),
        (302, {}, "inconclusive", "models_unavailable"),
    ],
)
async def test_discovery_returns_unique_names_without_claiming_input_capability(
    discovery_app, monkeypatch, status, body, outcome, code
):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        model_router, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        response = await client.post("/models/connections/configured/models")
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["outcome"] == outcome and result["code"] == code
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models")
    ]
    assert requests[0].headers["authorization"] == "Bearer private-draft-key"
    assert "private-draft-key" not in response.text
    if outcome == "success":
        assert result["items"] == [
            {"model_name": "first", "display_name": "first"},
            {"model_name": "second", "display_name": "second"},
        ]


async def test_discovery_does_not_borrow_another_users_connection(
    discovery_app, monkeypatch
):
    discovery_app.dependency_overrides[model_router._discovery_user] = lambda: (
        UserContext(
            user_id=3,
            username="missing",
            display_name="Missing",
            roles=(),
            disabled=False,
        )
    )

    def unexpected(**kwargs):
        raise AssertionError("无权读取的连接不得发起网络请求")

    monkeypatch.setattr(model_router, "ModelTransport", unexpected)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        response = await client.post("/models/connections/configured/models")
    assert response.status_code == 422
    assert "private-draft-key" not in response.text


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (socket.gaierror("private-draft-key"), "network_error"),
        (httpx.ConnectError("private-draft-key"), "network_error"),
        (httpx.ReadTimeout("private-draft-key"), "timeout"),
        (ModelEndpointNotAllowed("private-draft-key"), "endpoint_not_allowed"),
        (ModelResponseTooLarge("private-draft-key"), "response_too_large"),
        (ValueError("private-draft-key"), "invalid_response"),
        (RuntimeError("private-draft-key"), "service_error"),
    ],
)
async def test_discovery_returns_safe_failure_without_retry(
    discovery_app, monkeypatch, failure, code
):
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(
        model_router, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        response = await client.post("/models/connections/configured/models")
    assert response.json()["data"]["code"] == code and calls == 1
    assert "private-draft-key" not in response.text


async def test_browser_disconnect_cancels_discovery_and_closes_client(
    discovery_app, monkeypatch
):
    started, closed = asyncio.Event(), asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("不可达")

        async def aclose(self):
            closed.set()

    async def disconnected(self):
        await started.wait()
        return True

    monkeypatch.setattr(model_router, "ModelTransport", lambda **kwargs: Transport())
    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post("/models/connections/configured/models")
    assert closed.is_set()


@pytest.mark.parametrize("authenticated", [True, False])
async def test_discovery_authenticates_before_network_without_holding_database_session(
    database, monkeypatch, authenticated, model_connections
):
    from datetime import UTC, datetime

    from sqlalchemy import text as sql_text

    from tinkerfin_studio.api.errors import application_exception_handler
    from tinkerfin_studio.auth.types import RequestAuthState

    app = FastAPI()
    app.include_router(model_router.router)

    async def handle_business_error(request, error: Exception):
        assert isinstance(error, BusinessException)
        return await application_exception_handler(request, error)

    app.add_exception_handler(BusinessException, handle_business_error)
    connections = 0
    model_calls = 0

    def checkout(*args):
        nonlocal connections
        connections += 1

    def checkin(*args):
        nonlocal connections
        connections -= 1

    event.listen(database.engine.sync_engine, "checkout", checkout)
    event.listen(database.engine.sync_engine, "checkin", checkin)

    class Auth:
        async def resolve_token(self, token):
            assert token == "browser-token"
            user = UserContext(
                user_id=1,
                username="tester",
                display_name="Tester",
                roles=(),
                disabled=False,
            )
            return RequestAuthState(
                token=token,
                user=user if authenticated else None,
                is_authenticated=authenticated,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )

    async def auth_service(request, session):
        await session.execute(sql_text("SELECT 1"))
        return Auth()

    async def handle(request):
        nonlocal model_calls
        model_calls += 1
        assert connections == 0
        return httpx.Response(200, json={"data": [{"id": "draft-model"}]})

    monkeypatch.setattr(model_router, "get_auth_service", auth_service)
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )
    monkeypatch.setattr(
        model_router, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/connections/configured/models",
            headers={"Authorization": "Bearer browser-token"},
        )
    assert response.status_code == (200 if authenticated else 401)
    assert model_calls == (1 if authenticated else 0) and connections == 0


async def test_http_cancellation_waits_for_client_close_after_repeated_cancel(
    database, monkeypatch, model_connections
):
    """重复取消请求不能截断模型目录客户端已开始的关闭"""
    (started, cleaning, release, closed) = (asyncio.Event() for _ in range(4))

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("不可达")

        async def aclose(self) -> None:
            cleaning.set()
            await release.wait()
            closed.set()

    app = FastAPI()
    app.include_router(model_router.router)
    app.dependency_overrides[model_router._discovery_user] = lambda: UserContext(
        user_id=1, username="tester", display_name="Tester", roles=(), disabled=False
    )
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )
    monkeypatch.setattr(model_router, "ModelTransport", lambda **kwargs: Transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post("/models/connections/configured/models")
        )
        try:
            await started.wait()
            request.cancel("首次取消")
            await cleaning.wait()
            request.cancel("重复取消")
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await delivered.wait()
            assert not request.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert closed.is_set()
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("request_fails", [False, True])
async def test_discovery_client_close_failure_is_not_a_catalog_result(
    discovery_app, monkeypatch, request_fails
):
    """客户端关闭失败继续传播，不返回模型目录业务结果"""
    close_failure = RuntimeError("模型客户端关闭失败")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request_fails:
                raise httpx.ConnectError("供应商连接失败")
            return httpx.Response(200, json={"data": [{"id": "draft-model"}]})

        async def aclose(self) -> None:
            raise close_failure

    monkeypatch.setattr(model_router, "ModelTransport", lambda **kwargs: Transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError) as caught:
            await client.post("/models/connections/configured/models")
    assert caught.value is close_failure


@pytest.mark.parametrize("disconnect", [False, True])
async def test_discovery_cancellation_preserves_client_close_failure(
    discovery_app, monkeypatch, disconnect
):
    """请求取消或浏览器断连时等待关闭，并保留关闭失败的异常链"""
    started, cleaning, release, disconnected = (asyncio.Event() for _ in range(4))
    close_failure = RuntimeError("模型客户端关闭失败")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("取消后不得返回模型目录")

        async def aclose(self) -> None:
            cleaning.set()
            await release.wait()
            raise close_failure

    async def is_disconnected(self: Request) -> bool:
        await disconnected.wait()
        return True

    monkeypatch.setattr(model_router, "ModelTransport", lambda **kwargs: Transport())
    monkeypatch.setattr(Request, "is_disconnected", is_disconnected)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=discovery_app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post("/models/connections/configured/models")
        )
        try:
            await started.wait()
            if disconnect:
                disconnected.set()
            else:
                request.cancel("请求取消")
            await cleaning.wait()
            assert not request.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await request
            pending: list[BaseException] = [caught.value]
            seen: set[int] = set()
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
                pending.extend(
                    cause
                    for cause in (error.__cause__, error.__context__)
                    if cause is not None
                )
            assert id(close_failure) in seen
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


def test_discovery_result_rejects_unrecognized_code():
    """响应只接受已定义的目录结果码"""
    assert set(
        ModelDiscoveryResult.model_json_schema()["properties"]["code"]["enum"]
    ) == {
        "models_received",
        "models_unavailable",
        "endpoint_not_allowed",
        "response_too_large",
        "timeout",
        "authentication_failed",
        "rate_limited",
        "service_error",
        "network_error",
        "invalid_response",
    }
    with pytest.raises(ValidationError) as caught:
        ModelDiscoveryResult.model_validate({"outcome": "failed", "code": "unknown"})
    assert caught.value.errors()[0]["loc"] == ("code",)
