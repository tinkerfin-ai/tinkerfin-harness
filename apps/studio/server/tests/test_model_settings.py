"""提供方连接、模型配置的用户隔离、认证和运行保护"""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
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


async def test_connection_key_retention_replacement_and_explicit_clearing(session):
    owner = service(session)
    await owner.save_connection(connection("original"))
    await owner.save_settings(model())
    await owner.save_connection(connection(None, display_name="重命名"))
    assert (await owner.resolve("main")).api_key.get_secret_value() == "original"
    await owner.save_connection(connection("replacement"))
    assert (await owner.resolve("main")).api_key.get_secret_value() == "replacement"
    await owner.save_connection(connection(None, auth_type="none"))
    assert (await owner.resolve("main")).api_key.get_secret_value() == ""
    assert not (await owner.connections())[0].has_key


@pytest.mark.parametrize("invalid", ["missing_connection", "duplicate_model"])
async def test_batch_rejection_preserves_existing_models_and_default(session, invalid):
    owner = service(session)
    await owner.save_connection(connection())
    await owner.save_settings(model(is_default=True))
    first = model(model_id="new", is_default=True)
    second = (
        model(model_id="other", connection_id="missing")
        if invalid == "missing_connection"
        else first
    )
    with pytest.raises(BusinessException):
        await owner.save_models([first, second])
    assert not session.in_transaction()
    saved = await owner.settings()
    assert len(saved) == 1
    assert saved[0].model_id == "main" and saved[0].is_default


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


async def test_endpoint_change_requires_explicit_key_and_missing_owner_cannot_reuse(
    session,
):
    owner = service(session)
    await owner.save_connection(connection())
    with pytest.raises(BusinessException) as rejected:
        await owner.save_connection(
            connection(None, base_url="https://other.example/v1")
        )
    assert rejected.value.error_code == ModelErrorCode.KEY_ENDPOINT_CHANGED
    with pytest.raises(BusinessException):
        await service(session, 2).save_connection(connection(None))
    with pytest.raises(BusinessException):
        await service(session, 2).save_settings(model())
    assert not session.in_transaction()


@pytest.mark.parametrize(
    "address",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://ollama:11434",
        "http://192.168.1.20:11434",
    ],
)
async def test_ollama_can_be_saved_and_defaulted_without_dummy_key(session, address):
    owner = service(session)
    await owner.save_connection(
        connection(
            None,
            provider_id="ollama",
            api_type="ollama",
            base_url=address,
            auth_type="none",
        )
    )
    await owner.save_settings(
        model(
            model_name="qwen3:14b",
            chat_options={"context_window": 8192, "keep_alive": 300},
        )
    )
    await owner.set_default("main")
    saved = await owner.resolve("main")
    assert saved.provider == "ollama" and saved.base_url.rstrip("/") == address
    assert saved.api_key.get_secret_value() == ""
    assert saved.chat_options.context_window == 8192
    assert (await owner.list_catalog()).default_model_id == "main"


@pytest.mark.parametrize(
    "address",
    ["http://user:secret@localhost:11434", "https://user:secret@models.example"],
)
async def test_embedded_url_credentials_are_rejected(session, address):
    with pytest.raises(BusinessException):
        await service(session).save_connection(connection(base_url=address))
    assert await service(session).connections() == []


@pytest.mark.parametrize("status", ["preparing", "starting", "running", "waiting"])
async def test_active_run_protects_model_and_its_shared_connection(session, status):
    owner = service(session)
    await owner.save_connection(connection())
    await owner.save_settings(model())
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = ConversationThread(
        user_id=1, thread_id="active", title="对话", created_at=now, updated_at=now
    )
    session.add(thread)
    await session.flush()
    session.add(
        ConversationRunRegistration(
            conversation_thread_id=thread.id,
            run_id="run",
            model_id="main",
            status=status,
            input_json={},
            started_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    for operation in [
        lambda: owner.save_connection(connection("changed")),
        lambda: owner.delete_connection("shared"),
        lambda: owner.save_settings(model(display_name="changed")),
        lambda: owner.delete_settings("main"),
    ]:
        with pytest.raises(BusinessException) as rejected:
            await operation()
        assert rejected.value.error_code == ModelErrorCode.IN_USE
    await owner.set_default("main")
    assert (await owner.resolve("main")).api_key.get_secret_value() == "secret"


async def test_image_and_chat_defaults_are_independent_and_missing_image_has_no_fallback(
    session,
):
    owner = service(session)
    await owner.save_connection(connection())
    await owner.save_settings(model(is_default=True))
    await owner.save_settings(model(model_id="image", purpose="image"))
    assert await owner.resolve_image_model() is None
    await owner.set_default("image")
    image = await owner.resolve_image_model()
    assert image is not None and image.model_id == "image"
    assert [m.model_id for m in (await owner.list_catalog()).items] == ["main"]
    with pytest.raises(BusinessException) as rejected:
        await owner.resolve("image")
    assert rejected.value.error_code == ModelErrorCode.PURPOSE_MISMATCH


async def test_invalid_model_parameters_do_not_change_existing_default(session):
    owner = service(session)
    await owner.save_connection(connection())
    await owner.save_settings(model(is_default=True))
    with pytest.raises(BusinessException):
        await owner.save_settings(
            model(
                model_id="invalid",
                is_default=True,
                chat_options={"context_window": 8192},
            )
        )
    assert (await owner.list_catalog()).default_model_id == "main"


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
