"""草稿模型检查的结果语义、凭据归属和请求资源边界"""

import asyncio
import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import event

from tinkerfin_studio.api import model_router
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.models import testing
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ModelTestRequest
from tinkerfin_studio.models.service import AgentModelService


def draft(**updates) -> AgentModelSave:
    return AgentModelSave.model_validate(
        {
            "model_id": "draft",
            "display_name": "草稿",
            "connection_id": "configured",
            "model_name": "draft-model",
            **updates,
        }
    )


def stream_reply(text: str) -> httpx.Response:
    chunk = {
        "id": "test-reply",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "draft-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None,
            }
        ],
    }
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
    )


@pytest.mark.parametrize(
    ("status", "body", "outcome", "code"),
    [
        (200, {"data": [{"id": "draft-model"}]}, "success", "model_listed"),
        (200, {"data": [{"id": "other"}]}, "inconclusive", "model_not_listed"),
        (200, {"unexpected": "data"}, "inconclusive", "models_unavailable"),
        (401, {"error": "private-draft-key"}, "inconclusive", "authentication_failed"),
        (403, {}, "inconclusive", "authentication_failed"),
        (404, {}, "inconclusive", "models_unavailable"),
        (405, {}, "inconclusive", "models_unavailable"),
        (500, {}, "inconclusive", "models_unavailable"),
        (302, {}, "inconclusive", "models_unavailable"),
    ],
)
async def test_basic_only_lists_models_without_claiming_generation_capability(
    database,
    monkeypatch,
    status,
    body,
    outcome,
    code,
):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        testing, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    result = await testing.run_model_test(
        database,
        user_id=1,
        payload=ModelTestRequest(kind="basic", configuration=draft()),
    )
    assert result.outcome == outcome and result.code == code
    assert result.elapsed_ms >= 0 and result.image is None and result.text is None
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models")
    ]
    assert "private-draft-key" not in result.model_dump_json()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("kind", ["text", "vision"])
async def test_capability_uses_real_sdk_current_draft_and_borrowed_client(
    database,
    monkeypatch,
    provider,
    kind,
):
    requests = []
    connections = 0

    def checkout(*args):
        nonlocal connections
        connections += 1

    def checkin(*args):
        nonlocal connections
        connections -= 1

    event.listen(database.engine.sync_engine, "checkout", checkout)
    event.listen(database.engine.sync_engine, "checkin", checkin)

    async def handle(request):
        assert connections == 0
        requests.append(request)
        return stream_reply("Red left, blue right")

    monkeypatch.setattr(
        testing, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    payload = ModelTestRequest.model_validate(
        {
            "kind": kind,
            "configuration": draft(
                connection_id="deepseek" if provider == "deepseek" else "configured",
                reasoning_enabled=provider == "deepseek",
            ),
        }
    )
    result = await testing.run_model_test(database, user_id=1, payload=payload)
    assert result.outcome == "success" and result.text == "Red left, blue right"
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["authorization"] == "Bearer private-draft-key"
    body = json.loads(request.content)
    assert body["model"] == "draft-model" and body["stream"] is True
    if provider == "deepseek":
        assert body["thinking"] == {"type": "enabled"}
    if kind == "vision":
        assert result.code == "vision_response_received" and result.image is not None
        content = body["messages"][0]["content"]
        assert (
            content[1]["image_url"]["url"]
            == "data:image/png;base64," + result.image.data_base64
        )
        with Image.open(
            io.BytesIO(base64.b64decode(result.image.data_base64))
        ) as image:
            assert image.size == (96, 48)
            assert image.getpixel((10, 10)) == (255, 0, 0)
            assert image.getpixel((60, 10)) == (0, 0, 255)
    else:
        assert result.code == "text_received" and result.image is None
    async with database.session() as session:
        assert await AgentModelRepository(session, user_id=1).list_settings() == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (500, "service_error"),
        (302, "service_error"),
    ],
)
async def test_sdk_failures_are_safe_and_never_retried(
    database, monkeypatch, status, code
):
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "private-draft-key https://signed.example?token=secret"
                }
            },
        )

    monkeypatch.setattr(
        testing, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    result = await testing.run_model_test(
        database,
        user_id=1,
        payload=ModelTestRequest(kind="text", configuration=draft()),
    )
    assert result.outcome == "failed" and result.code == code and calls == 1
    assert (
        "secret" not in result.model_dump_json()
        and "private-draft-key" not in result.model_dump_json()
    )


async def test_empty_or_large_text_is_bounded_and_secrets_are_redacted(
    database, monkeypatch
):
    for text, expected in [
        ("", "empty_response"),
        ("private-draft-key" + "x" * 3000, "text_received"),
    ]:
        monkeypatch.setattr(
            testing,
            "ModelTransport",
            lambda **kwargs: httpx.MockTransport(lambda request: stream_reply(text)),
        )
        result = await testing.run_model_test(
            database,
            user_id=1,
            payload=ModelTestRequest(kind="text", configuration=draft()),
        )
        assert result.code == expected
        assert (
            len(result.text or "") <= 2000
            and "private-draft-key" not in result.model_dump_json()
        )


async def test_model_test_uses_saved_owner_connection_without_saving(
    database, monkeypatch
):
    seen = []

    async def send(self, request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            200, json={"data": [{"id": "draft-model"}]}, request=request
        )

    monkeypatch.setattr(testing.ModelTransport, "handle_async_request", send)
    result = await testing.run_model_test(
        database,
        user_id=1,
        payload=ModelTestRequest(
            kind="basic", configuration=draft(display_name="未保存")
        ),
        allowed_origins=(),
    )
    assert result.outcome == "success"
    assert seen == ["Bearer private-draft-key"]
    async with database.session() as session:
        service = AgentModelService(AgentModelRepository(session, user_id=1))
        assert await service.settings() == []
        with pytest.raises(BusinessException):
            await AgentModelService(
                AgentModelRepository(session, user_id=3)
            ).resolve_draft(draft())


@pytest.mark.parametrize(
    "options",
    [
        [],
        "{}",
        {"model": "override"},
        {"Authorization": "secret"},
        {"size": 123},
        {"output_format": None},
        {"unknown": float("nan")},
        {"unknown": float("inf")},
        {"nested": [float("nan")]},
        {"unknown": "中" * 22000},
    ],
)
def test_save_and_test_reject_invalid_generation_options(options):
    with pytest.raises(ValidationError):
        draft(generation_options=options)


def test_generation_options_preserve_unknown_json_values():
    options = {
        "size": "custom-size",
        "output_format": "custom-format",
        "custom": {"nested": [True, 1, 1.5, None, "value"]},
    }
    assert draft(generation_options=options).generation_options == options


@pytest.mark.parametrize(
    ("purpose", "kind"), [("chat", "image"), ("image", "text"), ("image", "vision")]
)
def test_wrong_capability_is_rejected_before_network(purpose, kind):
    with pytest.raises(ValidationError):
        ModelTestRequest.model_validate(
            {"kind": kind, "configuration": draft(purpose=purpose)}
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_close_owned_transport(
    database, monkeypatch, cancel
):
    started = asyncio.Event()

    class SlowTransport(httpx.AsyncBaseTransport):
        calls = 0
        closed = False

        async def handle_async_request(self, request):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self):
            self.closed = True

    transport = SlowTransport()
    monkeypatch.setattr(testing, "ModelTransport", lambda **kwargs: transport)
    if not cancel:
        monkeypatch.setitem(testing.TEST_TIMEOUT_SECONDS, "text", 0.05)
    task = asyncio.create_task(
        testing.run_model_test(
            database,
            user_id=1,
            payload=ModelTestRequest(kind="text", configuration=draft()),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.code == "timeout" and result.outcome == "failed"
    assert transport.closed and transport.calls == 1


@pytest.mark.parametrize("valid", [True, False])
async def test_image_test_reuses_generation_validates_preview_and_does_not_persist(
    database, monkeypatch, valid
):
    from tinkerfin_studio.attachments import generation

    image = io.BytesIO()
    Image.new("RGB", (1024, 768), "blue").save(image, "PNG")
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            image.getvalue() if valid else b"not-an-image"
                        ).decode()
                    }
                ]
            },
        )

    monkeypatch.setattr(
        generation, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    result = await testing.run_model_test(
        database,
        user_id=1,
        payload=ModelTestRequest(
            kind="image",
            configuration=draft(
                purpose="image", generation_options={"size": "1024x1024"}
            ),
        ),
    )
    assert len(requests) == 1 and requests[0].url.path == "/v1/images/generations"
    assert json.loads(requests[0].content)["size"] == "1024x1024"
    if valid:
        assert result.code == "image_received" and result.image is not None
        preview = base64.b64decode(result.image.data_base64)
        assert len(preview) <= 256 * 1024
        with Image.open(io.BytesIO(preview)) as rendered:
            assert rendered.format == "JPEG" and max(rendered.size) == 512
    else:
        assert result.code == "invalid_response" and result.image is None
    async with database.session() as session:
        assert await AgentModelRepository(session, user_id=1).list_settings() == []


async def test_http_test_route_returns_current_response_envelope(database, monkeypatch):
    app = FastAPI()
    app.include_router(model_router.router)
    app.dependency_overrides[model_router._test_user] = lambda: UserContext(
        user_id=1, username="tester", display_name="Tester", roles=(), disabled=False
    )
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )
    monkeypatch.setattr(
        testing,
        "ModelTransport",
        lambda **kwargs: httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "draft-model"}]})
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/configurations/test",
            json={
                "kind": "basic",
                "configuration": {
                    **draft().model_dump(mode="json"),
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["code"] == "model_listed"
    assert "private-draft-key" not in response.text


async def test_browser_disconnect_cancels_model_request_and_joins_cleanup(
    database, monkeypatch
):
    app = FastAPI()
    app.include_router(model_router.router)
    app.dependency_overrides[model_router._test_user] = lambda: UserContext(
        user_id=1,
        username="tester",
        display_name="Tester",
        roles=(),
        disabled=False,
    )
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )
    started = asyncio.Event()

    class SlowTransport(httpx.AsyncBaseTransport):
        closed = False
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self):
            self.closed = True

    transport = SlowTransport()
    monkeypatch.setattr(testing, "ModelTransport", lambda **kwargs: transport)
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "kind": "text",
                        "configuration": {
                            **draft().model_dump(mode="json"),
                        },
                    }
                ).encode(),
            }
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError(
            f"Disconnected request unexpectedly sent {message['type']}"
        )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            app(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "path": "/models/configurations/test",
                    "query_string": b"",
                    "headers": [(b"content-type", b"application/json")],
                    "scheme": "http",
                    "server": ("test", 80),
                    "client": ("test", 123),
                },
                receive,
                send,
            ),
            timeout=2,
        )
    assert transport.closed and transport.calls == 1


@pytest.mark.parametrize("authenticated", [True, False])
async def test_test_endpoint_authenticates_in_a_short_session_before_model_io(
    database, monkeypatch, authenticated
):
    from datetime import UTC, datetime, timedelta

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
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
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
        testing, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/configurations/test",
            headers={"Authorization": "Bearer browser-token"},
            json={
                "kind": "basic",
                "configuration": {
                    **draft().model_dump(mode="json"),
                },
            },
        )
    assert response.status_code == (200 if authenticated else 401)
    assert model_calls == (1 if authenticated else 0) and connections == 0


@pytest.mark.parametrize("cancel", [False, True])
async def test_image_preview_finishes_its_worker_before_cancellation_returns(
    database, monkeypatch, cancel
):
    import threading

    started = threading.Event()
    released = threading.Event()
    completed = threading.Event()

    async def generate(*args, **kwargs):
        return b"test-image"

    def preview(data, max_side):
        started.set()
        assert released.wait(2)
        completed.set()
        return b"preview"

    monkeypatch.setattr(testing, "generate_image_bytes", generate)
    monkeypatch.setattr(testing, "image_variant", preview)
    if not cancel:
        monkeypatch.setitem(testing.TEST_TIMEOUT_SECONDS, "image", 0.05)
    task = asyncio.create_task(
        testing.run_model_test(
            database,
            user_id=1,
            payload=ModelTestRequest(
                kind="image", configuration=draft(purpose="image")
            ),
        )
    )
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        if cancel:
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
        await asyncio.sleep(0.08)
        assert not task.done() and not completed.is_set()
        released.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).code == "timeout"
        assert completed.is_set()
    finally:
        released.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("arrays", [False, True])
@pytest.mark.parametrize("depth", [64, 65])
def test_generation_options_limit_container_depth_with_root_at_one(arrays, depth):
    from pydantic import JsonValue

    value: JsonValue = {}
    for _ in range(depth - 2):
        value = [value] if arrays else {"nested": value}
    options = {"root": value}
    if depth == 64:
        assert draft(generation_options=options).generation_options == options
    else:
        with pytest.raises(ValidationError):
            draft(generation_options=options)


@pytest.mark.parametrize("dns", [False, True])
async def test_connection_and_dns_failures_have_safe_network_result(
    database, monkeypatch, dns
):
    import socket

    async def handle(request):
        if dns:
            raise socket.gaierror("private-draft-key")
        raise httpx.ConnectError("private-draft-key", request=request)

    monkeypatch.setattr(
        testing, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    result = await testing.run_model_test(
        database,
        user_id=1,
        payload=ModelTestRequest(kind="text", configuration=draft()),
    )
    assert (
        result.code == "network_error"
        and "private-draft-key" not in result.model_dump_json()
    )


async def test_http_cancellation_waits_for_client_close_after_repeated_cancel(
    database, monkeypatch
):
    """重复取消请求不能截断测试客户端已开始的关闭"""
    started, cleaning, release, closed = (asyncio.Event() for _ in range(4))

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
    app.dependency_overrides[model_router._test_user] = lambda: UserContext(
        user_id=1, username="tester", display_name="Tester", roles=(), disabled=False
    )
    monkeypatch.setattr(
        model_router,
        "get_resources",
        lambda app: SimpleNamespace(
            database=database, settings=SimpleNamespace(model_allowed_origins=())
        ),
    )
    monkeypatch.setattr(testing, "ModelTransport", lambda **kwargs: Transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post(
                "/models/configurations/test",
                json={
                    "kind": "basic",
                    "configuration": {
                        **draft().model_dump(mode="json"),
                    },
                },
            )
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


pytestmark = pytest.mark.usefixtures("model_connections")
