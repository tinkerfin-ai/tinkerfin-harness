"""本人连接的能力预览、设置派生字段与写入隔离"""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tinkerfin_studio.api.dependencies import get_session, get_user_context
from tinkerfin_studio.api.errors import (
    BusinessException,
    GlobalErrorCode,
    application_exception_handler,
)
from tinkerfin_studio.api.model_router import router
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ModelConnectionSave
from tinkerfin_studio.models.service import AgentModelService


@pytest.fixture
async def capability_client(database):
    async with database.session() as session:
        service = AgentModelService(AgentModelRepository(session, user_id=1))
        await service.save_connection(
            ModelConnectionSave(
                connection_id="owned",
                display_name="OpenAI",
                provider_id="openai",
                api_type="openai_chat_completions",
                base_url="https://api.openai.com/v1",
                api_key=SecretStr("test-secret"),
            )
        )
        await service.save_settings(
            AgentModelSave(
                model_id="chat",
                display_name="Chat",
                model_name="gpt-4o",
                connection_id="owned",
            )
        )
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def session_dependency():
        async with database.session() as session:
            yield session

    async def business_error(request, error):
        return await application_exception_handler(request, error)

    app.add_exception_handler(BusinessException, business_error)
    app.dependency_overrides[get_session] = session_dependency
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="test", display_name="Test", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app


async def test_preview_and_settings_share_resolution_without_saving(capability_client):
    client, _ = capability_client
    response = await client.post(
        "/api/models/input-capabilities",
        json={"connection_id": "owned", "model_name": "gpt-4o"},
    )
    expected = {"automatic": "supported", "effective": "supported", "source": "catalog"}
    assert response.status_code == 200
    assert response.json()["data"]["image_input_capability"] == expected
    manual = await client.post(
        "/api/models/input-capabilities",
        json={
            "connection_id": "owned",
            "model_name": "gpt-4o",
            "image_support": "unsupported",
        },
    )
    assert manual.json()["data"]["image_input_capability"] == {
        **expected,
        "effective": "unsupported",
        "source": "manual",
    }
    settings = (await client.get("/api/models/configurations")).json()["data"]
    assert len(settings) == 1
    chat = settings[0]
    assert chat["image_input_capability"] == expected
    assert chat["image_support"] == "unknown"
    assert "test-secret" not in str(settings)
    assert (
        await client.put("/api/models/configurations/chat", json=chat)
    ).status_code == 422


async def test_capability_is_owned_and_authenticated(capability_client):
    client, app = capability_client
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=2, username="other", display_name="Other", roles=(), disabled=False
    )
    missing = await client.post(
        "/api/models/input-capabilities",
        json={"connection_id": "owned", "model_name": "gpt-4o"},
    )
    assert missing.status_code == 422

    def unauthenticated():
        raise BusinessException(GlobalErrorCode.UNAUTHORIZED)

    app.dependency_overrides[get_user_context] = unauthenticated
    assert (
        await client.post(
            "/api/models/input-capabilities",
            json={"connection_id": "owned", "model_name": "gpt-4o"},
        )
    ).status_code == 401


@pytest.mark.parametrize(
    "patch",
    [
        {"model_name": ""},
        {"model_name": "x" * 129},
        {"image_support": "yes"},
        {"owner": 2},
        {"base_url": "https://evil.test"},
    ],
)
async def test_capability_rejects_invalid_inputs(capability_client, patch):
    client, _ = capability_client
    response = await client.post(
        "/api/models/input-capabilities",
        json={"connection_id": "owned", "model_name": "gpt-4o", **patch},
    )
    assert response.status_code == 422
