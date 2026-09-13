"""业务 JSON 包络、空结果以及文件响应的 HTTP 契约"""

import io
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.dependencies import (
    get_conversation_command_service,
    get_model_service,
    get_user_context,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.application import create_application
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import ModelConnectionSave
from tinkerfin_studio.models.service import AgentModelService


class ConversationCommands:
    """记录删除请求并按会话业务状态拒绝删除"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, *, thread_id: str) -> None:
        if thread_id == "running":
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT)
        self.deleted.append(thread_id)


async def test_empty_json_results_and_file_content_keep_their_http_contract(
    session: AsyncSession, attachments: AttachmentService
) -> None:
    """真实模型与附件操作返回空数据包络，文件下载保留原始内容"""

    application = create_application(lifespan=None)
    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        avatar_url=None,
        roles=(),
        disabled=False,
    )
    models = AgentModelService(AgentModelRepository(session, user_id=user.user_id))
    commands = ConversationCommands()
    application.dependency_overrides[get_model_service] = lambda: models
    application.dependency_overrides[get_conversation_command_service] = lambda: (
        commands
    )
    application.dependency_overrides[get_user_context] = lambda: user
    application.state.resources = SimpleNamespace(attachments=attachments)
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    content = image.getvalue()
    await models.save_connection(
        ModelConnectionSave(
            connection_id="shared",
            display_name="连接",
            provider_id="custom",
            api_type="openai_chat_completions",
            base_url="https://api.openai.com/v1",
            api_key=SecretStr("private-key"),
        )
    )
    payload = {
        "model_id": "main",
        "display_name": "Main",
        "connection_id": "shared",
        "model_name": "provider-model",
    }

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        saved = await client.put("/api/models/configurations/main", json=payload)
        assert len(await models.settings()) == 1
        overview = await client.get("/api/models/settings")
        assert overview.status_code == 200
        settings = overview.json()["data"]
        assert settings["connections"][0]["has_key"] is True
        assert "api_key" not in settings["connections"][0]
        assert "private-key" not in overview.text
        assert settings["models"][0]["connection_id"] == "shared"
        batch = await client.post("/api/models/configurations", json=[payload])
        assert batch.status_code == 200
        assert len(await models.settings()) == 1
        mismatched = await client.put("/api/models/configurations/other", json=payload)
        deleted_model = await client.delete("/api/models/configurations/main")
        assert await models.settings() == []
        deleted_thread = await client.delete("/api/conversation/idle")
        conflict = await client.delete("/api/conversation/running")
        uploaded = await client.post(
            "/api/attachments", params={"name": "chart.png"}, content=content
        )
        assert uploaded.status_code == 200
        attachment_id = uploaded.json()["data"]["id"]
        downloaded = await client.get(f"/api/attachments/{attachment_id}/content")
        deleted_attachment = await client.delete(f"/api/attachments/{attachment_id}")
        missing_attachment = await client.get(
            f"/api/attachments/{attachment_id}/content"
        )

    for response in (saved, deleted_model, deleted_thread, deleted_attachment):
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"code": 0, "message": "success", "data": None}
    assert commands.deleted == ["idle"]
    assert mismatched.status_code == 422
    assert mismatched.json()["code"] != 0
    assert mismatched.json()["data"] is None
    assert conflict.status_code == 409
    assert conflict.json() == {
        "code": int(ConversationErrorCode.DELETE_CONFLICT),
        "message": ConversationErrorCode.DELETE_CONFLICT.message,
        "data": None,
    }
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert missing_attachment.status_code == 404
    assert missing_attachment.json()["data"] is None


def test_business_json_openapi_responses_publish_the_envelope() -> None:
    """所有业务 JSON 成功响应声明统一包络；文件与 SSE 使用各自媒体类型"""

    schema = create_application(lifespan=None).openapi()
    native_paths = {
        "/api/attachments/{attachment_id}/content",
        "/api/conversation/chat",
        "/api/conversation/{thread_id}/trace",
        "/api/conversation/{thread_id}/trace/graph/follow",
    }
    null_results = {
        ("/api/auth/logout", "post"),
        ("/api/models/configurations/{model_id}", "put"),
        ("/api/models/configurations/{model_id}", "delete"),
        ("/api/models/configurations/{model_id}/default", "put"),
        ("/api/attachments/{attachment_id}", "delete"),
        ("/api/conversation/{thread_id}", "delete"),
    }
    seen_null_results = set()
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/") or path in native_paths:
            continue
        for method, operation in methods.items():
            responses = operation["responses"]
            assert "204" not in responses
            reference = responses["200"]["content"]["application/json"]["schema"][
                "$ref"
            ]
            model = schema["components"]["schemas"][reference.rsplit("/", 1)[1]]
            assert set(model["properties"]) == {"code", "message", "data"}
            if (path, method) in null_results:
                assert model["properties"]["data"]["type"] == "null"
                seen_null_results.add((path, method))
    assert seen_null_results == null_results
