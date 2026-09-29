"""个人服务配置的归属、凭证和唯一性契约"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api import service_router
from tinkerfin_studio.api.dependencies import (
    get_network_user_context,
    get_service_config_service,
    get_user_context,
)
from tinkerfin_studio.api.errors import BusinessException, ServiceErrorCode
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.services.entity import ServiceConfig
from tinkerfin_studio.services.http import SearchResult, ServiceHTTPError
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
                display_name=f"Owner {user_id}",
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


def test_service_payload_rejects_cross_capability_and_invalid_custom_result() -> None:
    with pytest.raises(ValidationError):
        ServiceSave.model_validate(
            {
                "configuration": {
                    "capability": "image_generation",
                    "provider_id": "tavily",
                    "endpoint": "https://example.com",
                }
            }
        )
    with pytest.raises(ValidationError):
        SearchConfig.model_validate(
            {
                "provider_id": "custom",
                "endpoint": "https://example.com/search",
                "request": {"items_pointer": "results"},
            }
        )


@pytest.mark.usefixtures("owners")
async def test_database_rejects_duplicate_capability_for_one_owner(
    session: AsyncSession,
) -> None:
    for id in ("first", "second"):
        session.add(
            ServiceConfig(
                id=id,
                user_id=1,
                capability="web_search",
                provider_id="tavily",
                enabled=True,
                config=SearchConfig().model_dump(mode="json"),
                api_key="secret",
            )
        )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.usefixtures("owners")
async def test_service_settings_route_saves_without_supplier_request_and_masks_key(
    session: AsyncSession,
) -> None:
    app = create_application(lifespan=None)
    user = UserContext(
        user_id=1,
        username="owner-1",
        display_name="Owner 1",
        avatar_url=None,
        roles=(),
        disabled=False,
    )
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    app.dependency_overrides[get_user_context] = lambda: user
    app.dependency_overrides[get_service_config_service] = lambda: service
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        saved = await client.put(
            "/api/services/web_search",
            json={
                "configuration": SearchConfig().model_dump(mode="json"),
                "api_key": "private-key",
                "enabled": True,
            },
        )
        overview = await client.get("/api/services/settings")
        wrong = await client.put(
            "/api/services/image_generation",
            json={"configuration": SearchConfig().model_dump(mode="json")},
        )
        removed = await client.delete("/api/services/web_search")
    assert saved.status_code == 200
    assert saved.json()["data"]["has_key"] is True
    assert overview.json()["data"]["image_generation"] is None
    assert "private-key" not in saved.text + overview.text
    assert wrong.status_code == 422
    assert removed.status_code == 200


@pytest.mark.usefixtures("owners")
async def test_active_service_test_calls_supplier_once_and_records_safe_result(
    database, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    await service.save(
        "web_search",
        ServiceSave(configuration=SearchConfig(), api_key=SecretStr("private-key")),
    )
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(database=database)
    app.dependency_overrides[get_network_user_context] = lambda: UserContext(
        user_id=1,
        username="owner-1",
        display_name="Owner 1",
        avatar_url=None,
        roles=(),
        disabled=False,
    )
    calls = 0

    async def execute(request, operation):
        return await operation()

    async def search(resolved, request):
        nonlocal calls
        calls += 1
        assert resolved.api_key == "private-key"
        if calls == 2:
            raise ServiceHTTPError(429)
        return SearchResult()

    monkeypatch.setattr(service_router, "connected_operation", execute)
    monkeypatch.setattr(service_router, "search_web", search)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        success = await client.post("/api/services/web_search/test")
        failure = await client.post("/api/services/web_search/test")
    assert success.json()["data"] == {"outcome": "success", "code": "success"}
    assert failure.json()["data"] == {"outcome": "failed", "code": "rate_limited"}
    assert calls == 2
    saved = await service.settings("web_search")
    assert saved is not None
    assert saved.test_status == "failed"
    assert saved.test_code == "rate_limited"
    assert "private-key" not in success.text + failure.text + saved.model_dump_json()


@pytest.mark.usefixtures("owners")
@pytest.mark.parametrize(
    "change", ["disable", "clear", "recreate", "key", "parameters"]
)
async def test_bound_service_rejects_changes_and_ignores_stale_test_results(
    session: AsyncSession,
    change: str,
) -> None:
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    await service.save(
        "web_search",
        ServiceSave(configuration=SearchConfig(), api_key=SecretStr("key")),
    )
    bound = await service.resolve("web_search")
    assert bound is not None
    if change in {"clear", "recreate"}:
        await service.clear("web_search")
    if change != "clear":
        await service.save(
            "web_search",
            ServiceSave(
                configuration=SearchConfig(
                    depth="advanced" if change == "parameters" else "basic"
                ),
                enabled=change != "disable",
                api_key=SecretStr("new-key" if change == "key" else "key"),
            ),
        )
    with pytest.raises(BusinessException) as error:
        await service.require_bound(
            "web_search", id=bound.id, fingerprint=bound.fingerprint
        )
    assert error.value.error_code == ServiceErrorCode.CONFIGURATION_CHANGED
    await service.record_test(bound, status="success", code="success")
    current = await service.settings("web_search")
    assert current is None or current.test_status is None


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


@pytest.mark.usefixtures("owners")
async def test_save_returns_its_committed_configuration_despite_later_clear(
    database,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    committed = asyncio.Event()
    cleared = asyncio.Event()
    original_commit = session.commit

    async def commit() -> None:
        await original_commit()
        committed.set()
        await cleared.wait()

    monkeypatch.setattr(session, "commit", commit)
    service = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    saving = asyncio.create_task(
        service.save(
            "web_search",
            ServiceSave(configuration=SearchConfig(), api_key=SecretStr("key")),
        )
    )
    try:
        await committed.wait()
        async with database.session() as other:
            concurrent = ServiceConfigService(ServiceConfigRepository(other, user_id=1))
            await concurrent.clear("web_search")
        cleared.set()
        result = await saving
        assert result.configuration.provider_id == "tavily"
        assert result.has_key
        assert await service.settings("web_search") is None
    finally:
        cleared.set()
        if not saving.done():
            saving.cancel()
        await asyncio.gather(saving, return_exceptions=True)
