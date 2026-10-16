"""个人服务配置的归属、凭证和唯一性契约"""

import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import BusinessException, ServiceErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import ImageConfig, SearchConfig, ServiceSave
from tinkerfin_studio.services.service import ServiceConfigService


@pytest_asyncio.fixture
async def owners(session: AsyncSession) -> None:
    for user_id in (1, 2):
        session.add(
            User(
                id=user_id,
                username=f"owner-{user_id}",
                password_hash="unused",
                roles=[],
                disabled=False,
            )
        )
    await session.commit()


@pytest.mark.usefixtures("owners")
async def test_one_search_config_per_user_keeps_key_private_and_replaces_id_after_clear(
    session: AsyncSession,
) -> None:
    first = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    other = ServiceConfigService(ServiceConfigRepository(session, user_id=2))
    saved = await first.save(
        "web_search",
        ServiceSave(
            configuration=SearchConfig(), api_key=SecretStr("owner-one-secret")
        ),
    )
    assert saved.has_key
    assert "owner-one-secret" not in saved.model_dump_json()
    assert await other.settings("web_search") is None

    updated = await first.save(
        "web_search",
        ServiceSave(configuration=SearchConfig(depth="advanced")),
    )
    assert updated.id == saved.id
    resolved = await first.resolve("web_search")
    assert resolved is not None
    assert resolved.api_key == "owner-one-secret"
    rows = list(await session.scalars(select(ServiceConfig)))
    assert len(rows) == 1

    await first.clear("web_search")
    assert await first.settings("web_search") is None
    recreated = await first.save(
        "web_search",
        ServiceSave(
            configuration=SearchConfig(), api_key=SecretStr("replacement-secret")
        ),
    )
    assert recreated.id != saved.id


@pytest.mark.usefixtures("owners")
async def test_auth_target_change_requires_new_key_and_disabled_config_is_not_resolved(
    session: AsyncSession,
) -> None:
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    await service.save(
        "image_generation",
        ServiceSave(
            configuration=ImageConfig(model="image-model"),
            api_key=SecretStr("old-secret"),
        ),
    )
    with pytest.raises(BusinessException) as error:
        await service.save(
            "image_generation",
            ServiceSave(
                configuration=ImageConfig(
                    endpoint="https://other.example/v1", model="image-model"
                )
            ),
        )
    assert error.value.error_code == ServiceErrorCode.KEY_ENDPOINT_CHANGED

    await service.save(
        "image_generation",
        ServiceSave(configuration=ImageConfig(model="image-model"), enabled=False),
    )
    assert await service.resolve("image_generation") is None


def test_custom_parameters_reject_credentials_and_nested_get_values() -> None:
    from tinkerfin_studio.services.schemas import HttpRequestConfig

    for parameters in ({"options": {"apiKey": "secret"}}, {"Authorization": "secret"}):
        with pytest.raises(ValidationError, match="凭证"):
            HttpRequestConfig.model_validate({"parameters": parameters})
    with pytest.raises(ValidationError, match="只支持标量"):
        HttpRequestConfig(method="GET", parameters={"filter": {"q": "test"}})


@pytest.mark.usefixtures("owners")
async def test_save_preserves_cancellation_when_rollback_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    original_rollback = session.rollback

    async def commit() -> None:
        raise asyncio.CancelledError()

    async def rollback() -> None:
        await original_rollback()
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(session, "rollback", rollback)
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    with pytest.raises(asyncio.CancelledError) as error:
        await service.save(
            "web_search",
            ServiceSave(configuration=SearchConfig(), api_key=SecretStr("key")),
        )
    assert isinstance(error.value.__cause__, RuntimeError)
    assert await service.settings("web_search") is None
