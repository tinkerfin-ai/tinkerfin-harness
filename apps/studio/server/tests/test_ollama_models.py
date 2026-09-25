"""Ollama 原生请求、认证和流内容经过当前 LangChain 集成的契约"""

import json

import httpx
import pytest
from pydantic import SecretStr

from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.discovery import discover_models
from tinkerfin_studio.models.schemas import AgentModelConfig, ChatOptions, ModelProvider


def configuration(provider: ModelProvider = "ollama", key=""):
    return AgentModelConfig(
        model_id="local",
        display_name="本地模型",
        provider=provider,
        model_name="qwen3:14b",
        base_url="http://ollama.local:11434",
        api_key=SecretStr(key),
        reasoning_enabled=True,
        chat_options=ChatOptions(
            max_tokens=128, temperature=0.3, context_window=8192, keep_alive=300
        ),
    )


async def test_ollama_uses_native_api_with_explicit_options_and_no_environment_key(
    monkeypatch,
):
    monkeypatch.setenv("OLLAMA_API_KEY", "unrelated-process-key")
    seen = []

    async def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": "qwen3:14b",
                    "message": {
                        "role": "assistant",
                        "thinking": "summary",
                        "content": "OK",
                    },
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 8,
                    "eval_count": 2,
                }
            )
            + "\n",
            headers={"content-type": "application/x-ndjson"},
            request=request,
        )

    async with httpx.MockTransport(respond) as transport:
        model = create_chat_model(configuration(), http_async_transport=transport)
        message = await model.ainvoke("hello")
    assert len(seen) == 1 and seen[0].url.path == "/api/chat"
    assert "authorization" not in seen[0].headers
    payload = json.loads(seen[0].content)
    assert payload["options"]["num_predict"] == 128
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["temperature"] == 0.3
    assert payload["keep_alive"] == 300 and payload["think"] is True
    assert (
        message.text == "OK"
        and message.additional_kwargs["reasoning_content"] == "summary"
    )


async def test_ollama_preserves_tool_calls_and_uses_only_configured_key():
    headers = []

    async def respond(request):
        headers.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": "qwen3:14b",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "lookup",
                                    "arguments": {"query": "example"},
                                }
                            }
                        ],
                    },
                    "done": True,
                }
            )
            + "\n",
            request=request,
        )

    async with httpx.MockTransport(respond) as transport:
        model = create_chat_model(
            configuration(key="owner-key"), http_async_transport=transport
        )
        reply = await model.ainvoke("lookup")
    assert headers == ["Bearer owner-key"]
    assert reply.tool_calls[0]["name"] == "lookup"
    assert reply.tool_calls[0]["args"] == {"query": "example"}
    assert reply.tool_calls[0]["id"]


@pytest.mark.parametrize("image_support", ["supported", "unsupported"])
async def test_workspace_image_read_respects_declared_model_capability(
    tmp_path, image_support, large_image, work_file_runtime
):
    """工作图片按配置进入模型请求，读取过程不产生前端附件"""
    from deepagents.backends import FilesystemBackend

    from tinkerfin import TinkerFin
    from tinkerfin_studio.attachments.documents import DocumentProcessor
    from tinkerfin_studio.attachments.workspace_tools import save_work_file

    _, workspace, files = work_file_runtime
    saved = await save_work_file(
        workspace, DocumentProcessor(), name="image.png", data=large_image
    )
    assert saved.preview_file_path is not None
    for path, data in files.items():
        (tmp_path / path.lstrip("/")).write_bytes(data)
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "done"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"file_path": saved.preview_file_path},
                        }
                    }
                ],
            }
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": "qwen3:14b",
                    "message": message,
                    "done": True,
                    "done_reason": "stop",
                }
            )
            + "\n",
            request=request,
        )

    async with httpx.MockTransport(respond) as transport:
        config = configuration().model_copy(update={"image_support": image_support})
        model = create_chat_model(config, http_async_transport=transport)
        runtime = (
            TinkerFin()
            .with_namespace("workspace-read")
            .build(
                model=model,
                backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            )
        )
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            messages=[{"id": "input", "role": "user", "content": "Read /image.png"}],
        )
        try:
            events = [event async for event in stream]
            assert stream.error is None, repr(stream.error)
        finally:
            await stream.aclose()
    assert len(requests) == 2, [event.model_dump(mode="json") for event in events]
    images = [
        image
        for message in requests[1]["messages"]
        for image in message.get("images", [])
    ]
    assert bool(images) == (image_support == "supported")
    assert files[saved.file_path] == large_image
    assert "Binary file exceeds" not in str(requests[1]["messages"])
    wire = [event.model_dump(mode="json", by_alias=True) for event in events]
    results = [event for event in wire if event["type"] == "TOOL_CALL_RESULT"]
    assert len(results) == 1 and results[0]["attachments"] == []
    assert sum(event["type"] == "RUN_FINISHED" for event in wire) == 1


@pytest.mark.parametrize(
    ("provider", "body", "path"),
    [
        (
            "ollama",
            {"models": [{"name": "qwen3:14b"}, {"name": "qwen3:14b"}]},
            "/api/tags",
        ),
        ("openai", {"data": [{"id": "chat"}]}, "/models"),
    ],
)
async def test_discovery_uses_selected_interface_and_does_not_invent_capabilities(
    provider, body, path
):
    seen = []

    async def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await discover_models(
            provider, "http://service.local:11434", "", client
        )
    assert result.outcome == "success" and len(result.items) == 1
    assert seen[0].url.path == path
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_discovery_failure_does_not_claim_model_unavailable(status):
    async def respond(request):
        return httpx.Response(status, json={"error": "secret"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await discover_models(
            "ollama", "http://service.local:11434", "owner", client
        )
    assert result.outcome == "inconclusive" and result.items == []
    assert "secret" not in result.model_dump_json()


async def test_cancelled_ollama_call_closes_response_and_propagates_cancellation():
    import asyncio

    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    async def respond(request):
        return httpx.Response(200, stream=WaitingBody(), request=request)

    async with httpx.MockTransport(respond) as transport:
        model = create_chat_model(configuration(), http_async_transport=transport)
        task = asyncio.create_task(model.ainvoke("hello"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


async def test_openai_compatible_no_auth_does_not_use_environment_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-process-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "unrelated-admin-key")
    headers = []

    async def respond(request):
        headers.append(request.headers.get("authorization", ""))
        chunk = {
            "id": "message",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "model",
            "choices": [
                {"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        settings = configuration(provider="openai").model_copy(
            update={"reasoning_enabled": False, "chat_options": ChatOptions()}
        )
        response = await create_chat_model(settings, http_async_client=client).ainvoke(
            "hello"
        )
    assert response.text == "OK"
    assert headers == [""]
