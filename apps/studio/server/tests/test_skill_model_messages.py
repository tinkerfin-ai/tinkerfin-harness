"""当前模型适配器将用户原文和技能正文作为两条独立用户消息发送"""

import json
from typing import Any, Literal

import httpx
import pytest
from ag_ui.core import UserMessage
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from tinkerfin_agui_adapter.media import user_message_to_langchain
from tinkerfin_studio.models import providers
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig


@pytest.mark.parametrize("provider", ["openai", "deepseek", "ollama"])
async def test_provider_request_retains_two_users_without_sending_provenance(
    provider: Literal["openai", "deepseek", "ollama"], monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if provider == "ollama":
            return httpx.Response(
                200,
                json={
                    "model": "fixture",
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                },
            )
        chunk = {
            "id": "answer",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        return httpx.Response(
            200,
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    def reject_sync(request: httpx.Request) -> httpx.Response:
        raise AssertionError("模型请求必须使用异步客户端")

    initializer = providers.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(reject_sync)) as sync_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            def initialize(name: str, **kwargs: Any):
                return initializer(name, http_client=sync_client, **kwargs)

            monkeypatch.setattr(providers, "init_chat_model", initialize)
            transport = httpx.MockTransport(respond)
            config = AgentModelConfig(
                model_id="fixture",
                display_name="测试模型",
                provider=provider,
                model_name="fixture",
                base_url="https://model.invalid",
                api_key=SecretStr("test-only"),
                reasoning_enabled=False,
            )
            model = create_chat_model(
                config,
                http_async_client=client,
                http_async_transport=transport,
                max_retries=0,
            )
            context = user_message_to_langchain(
                UserMessage.model_validate(
                    {
                        "id": "context",
                        "role": "user",
                        "content": "完整技能正文",
                        "source": {
                            "kind": "context",
                            "name": "skill-invocation",
                            "metadata": {
                                "skills": [{"id": "report", "digest": "fixed"}]
                            },
                        },
                    }
                )
            )
            try:
                await model.ainvoke(
                    [
                        HumanMessage(id="question", content="  用 /reports 技能帮我\n"),
                        context,
                    ]
                )
            finally:
                await transport.aclose()
    assert len(requests) == 1
    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "  用 /reports 技能帮我\n"),
        ("user", "完整技能正文"),
    ]
    assert "tinkerfin_source" not in json.dumps(requests)
    assert all("source" not in message for message in messages)
