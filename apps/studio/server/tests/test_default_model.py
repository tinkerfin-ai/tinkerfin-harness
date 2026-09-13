"""直接默认切换的用户隔离、配置保留、原子性与 HTTP 契约"""

import asyncio
from datetime import UTC, datetime
from typing import Literal

import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.dependencies import get_model_service
from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.application import create_application
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave
from tinkerfin_studio.models.service import AgentModelService


def configuration(
    model_id: str,
    *,
    purpose: Literal["chat", "image"] = "chat",
    enabled: bool = True,
    default: bool = False,
) -> AgentModelSave:
    return AgentModelSave(
        connection_id="configured",
        model_id=model_id,
        display_name=f"模型 {model_id}",
        model_name="provider-model",
        generation_options={"temperature": 0.3, "custom": {"values": [1, 2]}},
        purpose=purpose,
        image_support="supported",
        reasoning_enabled=False,
        sort_order=17,
        enabled=enabled,
        is_default=default,
    )


@pytest.mark.parametrize("purpose", ["chat", "image"])
async def test_default_switch_preserves_configuration_and_isolates_owner_and_purpose(
    session: AsyncSession, purpose: Literal["chat", "image"]
) -> None:
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    other = AgentModelService(AgentModelRepository(session, user_id=2))
    opposite = "image" if purpose == "chat" else "chat"
    await service.save_settings(configuration("old", purpose=purpose, default=True))
    await service.save_settings(configuration("target", purpose=purpose, enabled=False))
    await service.save_settings(
        configuration("opposite", purpose=opposite, default=True)
    )
    await other.save_settings(configuration("old", purpose=purpose, default=True))
    await other.save_settings(configuration("target", purpose=purpose, enabled=False))
    target = await repository.get("target")
    assert target is not None
    preserved = {
        column.name: getattr(target, column.name)
        for column in AgentModel.__table__.columns
        if column.name not in {"enabled", "is_default", "updated_at"}
    }
    for _ in range(2):
        await service.set_default("target")
        assert not session.in_transaction()
        session.expire_all()
        target = await repository.get("target")
        assert target is not None and target.enabled and target.is_default
        assert {name: getattr(target, name) for name in preserved} == preserved
        states = {row.model_id: row for row in await service.settings()}
        assert states["old"].enabled and not states["old"].is_default
        assert states["opposite"].is_default
        other_states = {row.model_id: row for row in await other.settings()}
        assert other_states["old"].is_default
        assert not other_states["target"].enabled
        assert not other_states["target"].is_default


@pytest.mark.parametrize("model_id", ["missing", "other-user"])
async def test_missing_or_foreign_default_target_is_not_created(
    session: AsyncSession, model_id: str
) -> None:
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    other = AgentModelService(AgentModelRepository(session, user_id=2))
    await service.save_settings(configuration("old", default=True))
    await other.save_settings(configuration("other-user", default=True))
    with pytest.raises(BusinessException) as rejected:
        await service.set_default(model_id)
    assert rejected.value.error_code == ModelErrorCode.NOT_FOUND
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "old"
    assert len(await service.settings()) == 1
    assert (await other.list_catalog()).default_model_id == "other-user"


@pytest.mark.parametrize("key", ["", "  "])
async def test_default_requires_saved_key_without_changing_existing_default(
    session: AsyncSession, key: str
) -> None:
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(configuration("old", default=True))
    await service.save_settings(configuration("target", enabled=False))
    await session.execute(
        update(ModelConnection).where(ModelConnection.user_id == 1).values(api_key=key)
    )
    await session.commit()
    with pytest.raises(BusinessException) as rejected:
        await service.set_default("target")
    assert rejected.value.error_code == ModelErrorCode.KEY_REQUIRED
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "old"
    assert not (await service.settings())[1].enabled


@pytest.mark.parametrize("status", ["preparing", "starting", "running", "waiting"])
async def test_default_can_change_during_run_without_changing_run_or_connection(
    session: AsyncSession, status: str
) -> None:
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(configuration("target"))
    connection = await service.resolve("target")
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = ConversationThread(
        user_id=1, thread_id="active", title="运行", created_at=now, updated_at=now
    )
    session.add(thread)
    await session.flush()
    registration = ConversationRunRegistration(
        conversation_thread_id=thread.id,
        run_id="active-run",
        model_id="target",
        status=status,
        input_json={},
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(registration)
    await session.commit()
    await service.set_default("target")
    assert await service.resolve("target") == connection
    await session.refresh(registration)
    assert registration.status == status
    assert registration.model_id == "target"
    assert registration.updated_at == now
    assert (await service.list_catalog()).default_model_id == "target"


async def test_failed_default_commit_rolls_back_both_default_and_enablement(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    await service.save_settings(configuration("old", default=True))
    await service.save_settings(configuration("target", enabled=False))

    async def fail_commit() -> None:
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(repository, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        await service.set_default("target")
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "old"
    assert not (await service.settings())[1].enabled


async def test_repeated_cancel_waits_for_default_rollback_and_propagates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    await service.save_settings(configuration("old", default=True))
    await service.save_settings(configuration("target", enabled=False))
    committing = asyncio.Event()
    rolling_back = asyncio.Event()
    release = asyncio.Event()
    rollback = repository.rollback

    async def wait_commit() -> None:
        committing.set()
        await asyncio.Event().wait()

    async def wait_rollback() -> None:
        rolling_back.set()
        await release.wait()
        await rollback()

    monkeypatch.setattr(repository, "commit", wait_commit)
    monkeypatch.setattr(repository, "rollback", wait_rollback)
    task = asyncio.create_task(service.set_default("target"))
    try:
        async with asyncio.timeout(3):
            await committing.wait()
            task.cancel()
            await rolling_back.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "old"
    assert not (await service.settings())[1].enabled


async def test_anyio_cancellation_finishes_default_rollback(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    await service.save_settings(configuration("old", default=True))
    await service.save_settings(configuration("target", enabled=False))
    with anyio.CancelScope() as scope:

        async def cancel_commit() -> None:
            scope.cancel()
            await anyio.sleep(0)

        monkeypatch.setattr(repository, "commit", cancel_commit)
        await service.set_default("target")
        pytest.fail("取消必须继续传播")
    assert scope.cancelled_caught
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "old"


async def test_default_http_action_has_no_body_and_returns_null_envelope(
    session: AsyncSession,
) -> None:
    application = create_application(lifespan=None)
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(configuration("target", enabled=False))
    application.dependency_overrides[get_model_service] = lambda: service
    path = "/api/models/configurations/target/default"
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.put(path)
        assert response.status_code == 200
        assert response.json() == {"code": 0, "message": "success", "data": None}
        assert (await service.list_catalog()).default_model_id == "target"
        missing = await client.put("/api/models/configurations/missing/default")
        assert missing.status_code == ModelErrorCode.NOT_FOUND.http_status
        assert missing.json() == {
            "code": int(ModelErrorCode.NOT_FOUND),
            "message": ModelErrorCode.NOT_FOUND.message,
            "data": None,
        }
    operation = application.openapi()["paths"][
        "/api/models/configurations/{model_id}/default"
    ]["put"]
    assert "requestBody" not in operation
    assert "200" in operation["responses"]
    assert "204" not in operation["responses"]
    assert len(list(await session.scalars(select(AgentModel)))) == 1


pytestmark = pytest.mark.usefixtures("model_connections")
