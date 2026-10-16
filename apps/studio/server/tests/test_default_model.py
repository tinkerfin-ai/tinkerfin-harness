"""直接默认切换的用户隔离、配置保留、原子性与 HTTP 契约"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.models.entity import AgentModel
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ChatOptions
from tinkerfin_studio.models.service import AgentModelService


def configuration(
    model_id: str,
    *,
    enabled: bool = True,
    default: bool = False,
) -> AgentModelSave:
    return AgentModelSave(
        connection_id="configured",
        model_id=model_id,
        display_name=f"模型 {model_id}",
        model_name="provider-model",
        chat_options=ChatOptions(temperature=0.3),
        image_support="supported",
        reasoning_enabled=False,
        sort_order=17,
        enabled=enabled,
        is_default=default,
    )


async def test_default_switch_preserves_configuration_and_isolates_owner(
    session: AsyncSession,
) -> None:
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    other = AgentModelService(AgentModelRepository(session, user_id=2))
    await service.save_settings(configuration("old", default=True))
    await service.save_settings(configuration("target", enabled=False))
    await other.save_settings(configuration("old", default=True))
    await other.save_settings(configuration("target", enabled=False))
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
        other_states = {row.model_id: row for row in await other.settings()}
        assert other_states["old"].is_default
        assert not other_states["target"].enabled
        assert not other_states["target"].is_default


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
        await committing.wait()
        task.cancel()
        await rolling_back.wait()
        task.cancel()
        delivered = asyncio.Event()
        asyncio.get_running_loop().call_soon(delivered.set)
        await delivered.wait()
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


pytestmark = [
    pytest.mark.usefixtures("model_connections"),
    pytest.mark.usefixtures("projects"),
]
