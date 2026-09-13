import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave
from tinkerfin_studio.models.service import AgentModelService


async def test_model_catalog_returns_only_enabled_safe_fields(
    session: AsyncSession,
) -> None:
    """模型目录不得暴露 provider 连接和明文密钥"""

    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(
        AgentModelSave(
            connection_id="deepseek",
            model_id="deepseek-v4-pro",
            display_name="DeepSeek V4 Pro",
            model_name="deepseek-v4-pro",
            reasoning_enabled=True,
            enabled=True,
            is_default=True,
            sort_order=20,
        )
    )
    await service.save_settings(
        AgentModelSave(
            connection_id="configured",
            model_id="disabled",
            display_name="Disabled",
            model_name="disabled-model",
            enabled=False,
            is_default=False,
            sort_order=10,
        )
    )

    catalog = await service.list_catalog()
    payload = catalog.model_dump(by_alias=True)
    serialized = catalog.model_dump_json(by_alias=True)

    assert catalog.default_model_id == "deepseek-v4-pro"
    assert [item.model_id for item in catalog.items] == ["deepseek-v4-pro"]
    assert set(payload["items"][0]) == {
        "modelId",
        "displayName",
        "reasoningEnabled",
        "imageSupport",
        "isDefault",
    }
    assert "database-plain-secret" not in serialized
    assert "models.example.test" not in serialized
    assert "provider" not in serialized


async def test_setting_a_new_default_clears_the_previous_default(
    session: AsyncSession,
) -> None:
    """同一事务中只能留下一个启用的默认模型"""

    service = AgentModelService(AgentModelRepository(session, user_id=1))
    for model_id, is_default in (("first", True), ("second", True)):
        await service.save_settings(
            AgentModelSave(
                connection_id="configured",
                model_id=model_id,
                display_name=model_id.title(),
                model_name=f"provider-{model_id}",
                enabled=True,
                is_default=is_default,
            )
        )

    catalog = await service.list_catalog()
    resolved = await service.resolve("second")

    assert catalog.default_model_id == "second"
    assert sum(item.is_default for item in catalog.items) == 1
    assert resolved.api_key.get_secret_value() == "private-draft-key"
    assert resolved.provider == "openai"


pytestmark = pytest.mark.usefixtures("model_connections")
