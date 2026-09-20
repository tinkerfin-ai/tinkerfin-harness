import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import ModelErrorCode, SystemException
from tinkerfin_studio.models.entity import ModelConnection
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ModelConnectionSave
from tinkerfin_studio.models.service import AgentModelService


async def test_model_catalog_returns_only_enabled_safe_fields(
    session: AsyncSession,
) -> None:
    """模型目录只返回选择所需的模型与提供方信息"""

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
        "connectionId",
        "connectionDisplayName",
        "reasoningEnabled",
        "imageSupport",
        "isDefault",
    }
    assert payload["items"][0]["connectionId"] == "deepseek"
    assert payload["items"][0]["connectionDisplayName"] == "deepseek"
    assert "private-draft-key" not in serialized
    assert "models.example" not in serialized
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


async def test_catalog_groups_by_owned_connections_and_preserves_model_order(
    session: AsyncSession,
) -> None:
    """同名提供方分别成组，组内排序稳定且只显示本人的启用对话模型"""
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    for connection_id in ("configured", "deepseek"):
        await service.save_connection(
            ModelConnectionSave(
                connection_id=connection_id,
                display_name="同名提供方",
                provider_id="custom",
                api_type="openai_chat_completions",
                base_url="https://models.example/v1",
            )
        )
    for model_id, connection_id, order, enabled, purpose in (
        ("second-provider", "deepseek", 0, True, "chat"),
        ("later", "configured", 20, True, "chat"),
        ("earlier", "configured", 10, True, "chat"),
        ("disabled", "configured", 0, False, "chat"),
        ("image", "configured", 0, True, "image"),
    ):
        await service.save_settings(
            AgentModelSave.model_validate(
                {
                    "model_id": model_id,
                    "connection_id": connection_id,
                    "display_name": "模型",
                    "model_name": model_id,
                    "sort_order": order,
                    "enabled": enabled,
                    "purpose": purpose,
                }
            )
        )
    other = AgentModelService(AgentModelRepository(session, user_id=2))
    await other.save_settings(
        AgentModelSave(
            connection_id="configured",
            model_id="other-owner",
            display_name="其他用户的模型",
            model_name="other-owner",
        )
    )

    items = (await service.list_catalog()).items
    assert [item.model_id for item in items] == ["earlier", "later", "second-provider"]
    assert [item.connection_id for item in items] == [
        "configured",
        "configured",
        "deepseek",
    ]
    assert {item.connection_display_name for item in items} == {"同名提供方"}


async def test_catalog_rejects_missing_owned_connection(session: AsyncSession) -> None:
    """本人连接缺失时不能借用其他用户的同名连接"""
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(
        AgentModelSave(
            connection_id="configured",
            model_id="orphan",
            display_name="模型",
            model_name="orphan",
        )
    )
    await session.execute(
        delete(ModelConnection).where(
            ModelConnection.user_id == 1,
            ModelConnection.connection_id == "configured",
        )
    )
    with pytest.raises(SystemException) as error:
        await service.list_catalog()
    assert error.value.error_code == ModelErrorCode.CATALOG_UNAVAILABLE


pytestmark = pytest.mark.usefixtures("model_connections")
