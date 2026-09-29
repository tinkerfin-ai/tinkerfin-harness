"""实际模型配置的原生媒体声明，不把能力用作附件接收门槛"""

import httpx
import pytest
from langchain_core.language_models import ModelProfile
from pydantic import SecretStr

from tinkerfin_studio.models import providers
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig


@pytest.mark.parametrize(
    ("provider_id", "endpoint", "model_name", "declared", "expected"),
    [
        ("openai", "https://api.openai.com/v1", "gpt-4o", "unknown", True),
        ("openai", "https://api.openai.com/v1/", "gpt-4o", "unsupported", False),
        ("custom", "https://api.openai.com/v1", "gpt-4o", "unknown", False),
        ("openai", "https://gateway.example/v1", "gpt-4o", "unknown", False),
        ("openai", "https://api.openai.com/v1/proxy", "gpt-4o", "unknown", False),
        ("openai", "https://api.openai.com/v1", "unknown-model", "unknown", False),
        ("custom", "https://gateway.example/v1", "gpt-4o", "supported", True),
    ],
)
async def test_native_image_input_uses_owned_connection_and_explicit_declaration(
    monkeypatch, provider_id, endpoint, model_name, declared, expected
):
    def unexpected_request(request):
        raise AssertionError("创建模型不得发出能力探测请求")

    initializer = providers.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as sync_client:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(unexpected_request)
        ) as client:

            def initialize(name, **kwargs):
                return initializer(name, http_client=sync_client, **kwargs)

            monkeypatch.setattr(providers, "init_chat_model", initialize)
            config = AgentModelConfig.model_validate(
                {
                    "model_id": "fixture",
                    "display_name": "Fixture",
                    "provider": "openai",
                    "provider_id": provider_id,
                    "model_name": model_name,
                    "base_url": endpoint,
                    "api_key": SecretStr("fixture-key"),
                    "reasoning_enabled": False,
                    "image_support": declared,
                }
            )
            model = create_chat_model(config, http_async_client=client)
            assert model.profile is not None
            assert model.profile.get("image_inputs") is expected
            if provider_id == "custom" or endpoint.startswith("https://gateway"):
                assert model.profile.get("pdf_inputs") is False
                assert model.profile.get("audio_inputs") is False
                assert model.profile.get("video_inputs") is False
                assert model.profile.get("image_tool_message") is False
                assert "attachment" not in model.profile


async def test_manual_image_declaration_does_not_change_the_provider_catalog(
    monkeypatch,
):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    source: ModelProfile = {
        "image_inputs": True,
        "pdf_inputs": True,
        "video_inputs": True,
    }
    first = FakeListChatModel(responses=["unused"], profile=source)
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: first)
    config = AgentModelConfig(
        model_id="custom",
        display_name="Custom",
        provider="openai",
        model_name="gpt-4o",
        base_url="https://gateway.example/v1",
        api_key=SecretStr("fixture-key"),
        reasoning_enabled=False,
        image_support="supported",
    )
    result = create_chat_model(config)
    assert result.profile is not None
    assert result.profile.get("image_inputs") is True
    assert result.profile.get("pdf_inputs") is False
    assert result.profile.get("video_inputs") is False
    assert source == {"image_inputs": True, "pdf_inputs": True, "video_inputs": True}


@pytest.mark.parametrize(
    ("provider", "api", "url", "name", "expected"),
    [
        (
            "openai",
            "openai_chat_completions",
            "https://api.openai.com/v1",
            "gpt-4o",
            "supported",
        ),
        (
            "openai",
            "openai_chat_completions",
            "https://API.OPENAI.COM:443/v1/",
            "gpt-4o",
            "supported",
        ),
        (
            "openai",
            "openai_chat_completions",
            "https://api.openai.com/v1",
            "gpt-3.5-turbo",
            "unsupported",
        ),
        (
            "openai",
            "openai_chat_completions",
            "https://api.openai.com/v1",
            "gpt-4o-alias",
            "unknown",
        ),
        (
            "openai",
            "openai_chat_completions",
            "https://api.openai.com/v1?proxy=true",
            "gpt-4o",
            "unknown",
        ),
        (
            "openai",
            "openai_chat_completions",
            "https://api.openai.com.evil.test/v1",
            "gpt-4o",
            "unknown",
        ),
        ("openai", "ollama", "https://api.openai.com/v1", "gpt-4o", "unknown"),
        (
            "custom",
            "openai_chat_completions",
            "https://api.openai.com/v1",
            "gpt-4o",
            "unknown",
        ),
        (
            "dashscope",
            "openai_chat_completions",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-vl-max",
            "supported",
        ),
    ],
)
def test_image_capability_requires_exact_trusted_connection(
    provider, api, url, name, expected
):
    from tinkerfin_studio.models.capabilities import resolve_image_input

    result = resolve_image_input(
        provider_id=provider, api_type=api, base_url=url, model_name=name
    )
    assert result.automatic == result.effective == expected
    assert result.source == ("unknown" if expected == "unknown" else "catalog")


@pytest.mark.parametrize("declaration", ["supported", "unsupported"])
def test_manual_capability_keeps_the_automatic_evidence(declaration):
    from tinkerfin_studio.models.capabilities import resolve_image_input

    result = resolve_image_input(
        provider_id="openai",
        api_type="openai_chat_completions",
        base_url="https://api.openai.com/v1",
        model_name="gpt-4o",
        image_support=declaration,
    )
    assert result.automatic == "supported"
    assert result.effective == declaration
    assert result.source == "manual"
