"""提供方连接、模型配置的用户隔离、认证和运行保护"""

import asyncio

import pytest
from pydantic import SecretStr

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave, ModelConnectionSave
from tinkerfin_studio.models.service import AgentModelService


def connection(key: str | None = "secret", **updates):
    return ModelConnectionSave.model_validate(
        {
            "connection_id": "shared",
            "display_name": "连接",
            "provider_id": "custom",
            "api_type": "openai_chat_completions",
            "base_url": "https://models.example/v1",
            "api_key": None if key is None else SecretStr(key),
            **updates,
        }
    )


def model(**updates):
    return AgentModelSave.model_validate(
        {
            "model_id": "main",
            "display_name": "模型",
            "model_name": "provider-model",
            "connection_id": "shared",
            **updates,
        }
    )


def service(session, user_id=1):
    return AgentModelService(AgentModelRepository(session, user_id=user_id))


async def test_connections_and_models_are_owned_and_credentials_never_returned(session):
    first, second = service(session), service(session, 2)
    await first.save_connection(connection("owner-one"))
    await second.save_connection(connection("owner-two"))
    await first.save_settings(model())
    await first.save_settings(model(model_id="another"))
    await second.save_settings(model())
    assert (await first.resolve("main")).api_key.get_secret_value() == "owner-one"
    assert (await second.resolve("main")).api_key.get_secret_value() == "owner-two"
    assert len(await first.connections()) == 1
    assert len(await first.settings()) == 2
    assert "owner-one" not in (await first.connections())[0].model_dump_json()
    assert "api_key" not in (await first.settings())[0].model_dump_json()
    await second.delete_connection("shared")
    assert await second.settings() == []
    assert len(await first.settings()) == 2


async def test_batch_cannot_reference_another_users_connection(session):
    await service(session, 2).save_connection(connection(connection_id="private"))
    owner = service(session)
    await owner.save_connection(connection())
    with pytest.raises(BusinessException):
        await owner.save_models(
            [model(), model(model_id="other", connection_id="private")]
        )
    assert await owner.settings() == []
    await owner.save_models([model(), model(model_id="other")])
    assert {saved.model_id for saved in await owner.settings()} == {"main", "other"}
    assert await service(session, 2).settings() == []


@pytest.mark.parametrize(
    "address",
    ["http://user:secret@localhost:11434", "https://user:secret@models.example"],
)
async def test_embedded_url_credentials_are_rejected(session, address):
    with pytest.raises(BusinessException):
        await service(session).save_connection(connection(base_url=address))
    assert await service(session).connections() == []


async def test_failed_model_commit_rolls_back_default_change(session, monkeypatch):
    repository = AgentModelRepository(session, user_id=1)
    owner = AgentModelService(repository)
    await owner.save_connection(connection())
    await owner.save_settings(model(is_default=True))

    async def fail_commit():
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(repository, "commit", fail_commit)
    with pytest.raises(RuntimeError):
        await owner.save_settings(model(model_id="other", is_default=True))
    assert not session.in_transaction()
    assert (await owner.list_catalog()).default_model_id == "main"


async def test_cancelled_connection_save_rolls_back_and_propagates(
    session, monkeypatch
):
    repository = AgentModelRepository(session, user_id=1)
    owner = AgentModelService(repository)
    await owner.save_connection(connection())

    async def cancelled():
        raise asyncio.CancelledError()

    monkeypatch.setattr(repository, "commit", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await owner.save_connection(connection("changed"))
    assert not session.in_transaction()
    assert (await owner.require_connection("shared")).api_key == "secret"


pytestmark = pytest.mark.usefixtures("projects")
