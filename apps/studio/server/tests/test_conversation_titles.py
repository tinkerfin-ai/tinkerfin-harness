from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest
from langchain_core.language_models import BaseChatModel

from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import ConversationTitle
from tinkerfin_studio.conversation.titles import summarize_conversation_title
from tinkerfin_studio.infrastructure.database import Database


async def create_thread(database: Database):
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1, thread_id="title-thread", title="临时标题", model_id=None
        )
        await repository.commit()
        return thread.id


@pytest.mark.parametrize("phase", ["claim", "settlement"])
async def test_title_repeated_cancellation_finishes_committed_claim(
    database, monkeypatch, phase: str
) -> None:
    thread_pk = await create_thread(database)
    claim_entered, model_entered, finish_entered, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_claim = ConversationRepository.claim_title
    original_finish = ConversationRepository.finish_title
    claims = []

    async def claim(repository, thread_pk):
        claims.append(asyncio.current_task())
        claim_entered.set()
        if phase == "claim":
            await release.wait()
        return await original_claim(repository, thread_pk)

    async def finish(repository, thread_pk, title):
        finish_entered.set()
        if phase == "settlement":
            await release.wait()
        return await original_finish(repository, thread_pk, title)

    async def invoke(*_args: object, **_kwargs: object) -> None:
        model_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ConversationRepository, "claim_title", claim)
    monkeypatch.setattr(ConversationRepository, "finish_title", finish)
    model = create_autospec(BaseChatModel, instance=True)
    model.ainvoke.side_effect = invoke
    request = asyncio.create_task(
        summarize_conversation_title(
            database=database, thread_pk=thread_pk, text="测试标题", model=model
        )
    )
    await (claim_entered if phase == "claim" else model_entered).wait()
    request.cancel("首次取消")
    if phase == "settlement":
        await finish_entered.wait()
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()
    request.cancel("重复取消")
    checked = asyncio.Event()
    asyncio.get_running_loop().call_soon(checked.set)
    await checked.wait()
    premature = request.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.gather(
        *(task for task in claims if task is not None), return_exceptions=True
    )
    assert not premature
    assert finish_entered.is_set()
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None
        assert thread.title_generation_status == "failed"


@pytest.mark.parametrize("source", ["user", "generated", "unknown"])
async def test_fixed_or_generated_title_cannot_be_claimed(database, source):
    thread_pk = await create_thread(database)
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.get_thread_by_pk(thread_pk)
        assert thread is not None
        thread.title_source = source
        await repository.commit()
        assert not await repository.claim_title(thread_pk)


async def test_concurrent_title_claim_has_one_winner(database):
    thread_pk = await create_thread(database)

    async def claim():
        async with database.session() as session:
            repository = ConversationRepository(session)
            claimed = await repository.claim_title(thread_pk)
            await repository.commit()
            return claimed

    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]


async def test_unchanged_manual_title_prevents_inflight_generation(database):
    thread_pk = await create_thread(database)
    async with database.session() as session:
        repository = ConversationRepository(session)
        assert await repository.claim_title(thread_pk)
        await repository.commit()
        thread = await repository.get_thread_by_pk(thread_pk)
        assert thread is not None
        await repository.update_thread_meta(thread, title=thread.title, pinned=None)
        await repository.commit()
        assert not await repository.finish_title(thread_pk, "自动标题")
        await repository.commit()
        await session.refresh(thread)
        assert (thread.title, thread.title_source, thread.title_seq) == (
            "临时标题",
            "user",
            2,
        )
        assert (
            ConversationTitle.model_validate(thread).model_dump(by_alias=True)[
                "titleSource"
            ]
            == "user"
        )


@pytest.mark.parametrize("title", [None, "总结标题"])
async def test_title_attempt_is_not_repeated_after_settlement(database, title):
    thread_pk = await create_thread(database)
    async with database.session() as session:
        repository = ConversationRepository(session)
        assert await repository.claim_title(thread_pk)
        assert await repository.finish_title(thread_pk, title)
        await repository.commit()
        assert not await repository.claim_title(thread_pk)
        thread = await repository.get_thread_by_pk(thread_pk)
        assert thread is not None
        assert thread.title_generation_status == ("succeeded" if title else "failed")
        assert thread.title == (title or "临时标题")


@pytest.mark.parametrize("character", ["中", "a", "😀"])
def test_title_contract_counts_32_unicode_characters(character):
    from pydantic import ValidationError
    from sqlalchemy import String

    from tinkerfin_studio.conversation.models import ConversationThread
    from tinkerfin_studio.conversation.schemas import ConversationThreadUpdate

    title_type = ConversationThread.__table__.c.title.type
    assert isinstance(title_type, String) and title_type.length == 32
    assert ConversationThreadUpdate(title=character * 32).title == character * 32
    assert (
        ConversationTitle(threadId="thread", title=character * 32).title
        == character * 32
    )
    with pytest.raises(ValidationError):
        ConversationThreadUpdate(title=character * 33)
    with pytest.raises(ValidationError):
        ConversationTitle(threadId="thread", title=character * 33)


@pytest.mark.parametrize("character", ["中", "a", "😀"])
def test_default_title_uses_first_16_characters(character):
    from tinkerfin_studio.conversation.request import ChatRequest
    from tinkerfin_studio.conversation.run_preparation import (
        StartChatIntent,
        classify_intent,
    )

    request = ChatRequest.model_validate(
        {
            "runId": "run",
            "state": {},
            "tools": [],
            "context": [],
            "messages": [{"role": "user", "content": character * 40}],
            "forwardedProps": {"model": "model", "command": {"plan": "off"}},
        }
    )
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    assert intent.title == character * 16


async def test_title_uses_one_bounded_nonreasoning_request(database):
    import json

    import httpx
    from pydantic import SecretStr
    from sqlalchemy import event

    from tinkerfin_studio.conversation.titles import summarize_conversation_title
    from tinkerfin_studio.models.chat import create_chat_model
    from tinkerfin_studio.models.schemas import AgentModelConfig

    thread_pk = await create_thread(database)
    bodies = []
    connections = 0

    def checkout(*args):
        nonlocal connections
        connections += 1

    def checkin(*args):
        nonlocal connections
        connections -= 1

    event.listen(database.engine.sync_engine, "checkout", checkout)
    event.listen(database.engine.sync_engine, "checkin", checkin)

    async def handle(request):
        assert connections == 0
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: "
            + json.dumps(
                {
                    "id": "title",
                    "object": "chat.completion.chunk",
                    "model": "chosen-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "新能源汽车分析"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            + "\n\ndata: [DONE]\n\n",
        )

    config = AgentModelConfig(
        model_id="chosen",
        display_name="已选模型",
        provider="deepseek",
        model_name="chosen-model",
        base_url="https://example.invalid",
        api_key=SecretStr("secret"),
        reasoning_enabled=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = create_chat_model(
            config,
            reasoning_enabled=False,
            max_retries=0,
            max_tokens=256,
            http_async_client=client,
        )
        result = await summarize_conversation_title(
            database=database,
            thread_pk=thread_pk,
            text="介绍新能源汽车" * 5000,
            model=model,
        )
        assert result is not None and result.title == "新能源汽车分析"
        assert (
            result.title_source == "generated"
            and result.title_generation_status == "succeeded"
        )
        assert (
            await summarize_conversation_title(
                database=database, thread_pk=thread_pk, text="后续发言", model=model
            )
            is None
        )
    assert len(bodies) == 1
    body = bodies[0]
    assert body["model"] == "chosen-model" and body["max_tokens"] == 256
    assert body["thinking"] == {"type": "disabled"}
    assert len(body["messages"][1]["content"].encode()) <= 4096


@pytest.mark.parametrize("outcome", ["error", "timeout", "manual", "cancel"])
async def test_title_failures_and_manual_rename_never_retry(database, outcome):
    import httpx
    from pydantic import SecretStr

    from tinkerfin_studio.conversation.titles import summarize_conversation_title
    from tinkerfin_studio.models.chat import create_chat_model
    from tinkerfin_studio.models.schemas import AgentModelConfig

    thread_pk = await create_thread(database)
    requested = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        requested.set()
        if outcome == "error":
            return httpx.Response(500, json={"error": "private-provider-error"})
        await release.wait()
        raise httpx.ReadTimeout("受控超时", request=request)

    config = AgentModelConfig(
        model_id="chosen",
        display_name="已选模型",
        provider="openai",
        model_name="chosen-model",
        base_url="https://example.invalid",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = create_chat_model(config, max_retries=0, http_async_client=client)
        task = asyncio.create_task(
            summarize_conversation_title(
                database=database,
                thread_pk=thread_pk,
                text="介绍新能源汽车",
                model=model,
            )
        )
        try:
            await requested.wait()
            if outcome == "manual":
                async with database.session() as session:
                    repository = ConversationRepository(session)
                    thread = await repository.get_thread_by_pk(thread_pk)
                    assert thread is not None
                    await repository.update_thread_meta(
                        thread, title="手动标题", pinned=None
                    )
                    await repository.commit()
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert await task is None
            assert (
                await summarize_conversation_title(
                    database=database, thread_pk=thread_pk, text="再次请求", model=model
                )
                is None
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert calls == 1
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None
        assert thread.title == ("手动标题" if outcome == "manual" else "临时标题")
        assert thread.title_generation_status == (
            "skipped" if outcome == "manual" else "failed"
        )


async def test_title_save_commit_failure_settles_claim(database, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tinkerfin_studio.conversation.titles import summarize_conversation_title

    thread_pk = await create_thread(database)
    original = ConversationRepository.commit
    commits = 0

    async def commit(repository):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("提交失败")
        await original(repository)

    monkeypatch.setattr(ConversationRepository, "commit", commit)
    assert (
        await summarize_conversation_title(
            database=database,
            thread_pk=thread_pk,
            text="a",
            model=FakeListChatModel(responses=["title"]),
        )
        is None
    )
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None
        assert (thread.title, thread.title_generation_status) == ("临时标题", "failed")


async def test_cancellation_joins_claim_commit_before_settlement(database, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tinkerfin_studio.conversation.titles import summarize_conversation_title

    thread_pk = await create_thread(database)
    committed, release = asyncio.Event(), asyncio.Event()
    original = ConversationRepository.commit
    calls = 0

    async def commit(repository):
        nonlocal calls
        calls += 1
        await original(repository)
        if calls == 1:
            committed.set()
            await release.wait()

    monkeypatch.setattr(ConversationRepository, "commit", commit)
    task = asyncio.create_task(
        summarize_conversation_title(
            database=database,
            thread_pk=thread_pk,
            text="a",
            model=FakeListChatModel(responses=["title"]),
        )
    )
    try:
        await committed.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None and thread.title_generation_status == "failed"


@pytest.mark.parametrize("text", ["中" * 40, "a" * 40, "😀" * 40, "短"])
async def test_generated_title_is_at_most_32_characters(database, text):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tinkerfin_studio.conversation.titles import summarize_conversation_title

    thread_pk = await create_thread(database)
    result = await summarize_conversation_title(
        database=database,
        thread_pk=thread_pk,
        text="a",
        model=FakeListChatModel(responses=[text]),
    )
    assert result is not None and result.title == text[:32]


@pytest.mark.parametrize("started", [False, True])
async def test_application_close_settles_queued_and_running_titles(
    database, monkeypatch, started
):
    import httpx
    from pydantic import SecretStr

    from tinkerfin_studio.conversation import titles as module
    from tinkerfin_studio.models.schemas import AgentModelConfig

    thread_pk = await create_thread(database)
    entered = asyncio.Event()

    async def invoke(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    model = create_autospec(BaseChatModel, instance=True)
    model.ainvoke.side_effect = invoke
    monkeypatch.setattr(module, "create_chat_model", lambda *args, **kwargs: model)
    config = AgentModelConfig(
        model_id="main",
        display_name="主模型",
        provider="deepseek",
        model_name="deepseek-chat",
        base_url="https://example.invalid",
        api_key=SecretStr("test"),
        reasoning_enabled=False,
    )
    async with httpx.AsyncClient() as client:
        titles = module.ConversationTitles(
            database=database, http_client=client, http_transport=None
        )
        await titles.start(thread_pk=thread_pk, text="标题", model=config)
        await titles.start(thread_pk=thread_pk, text="重复请求", model=config)
        if started:
            await entered.wait()
        await titles.aclose()
        await titles.aclose()
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None and thread.title_generation_status == "failed"
    assert model.ainvoke.call_count == (1 if started else 0)


async def test_title_queue_overflow_settles(database, monkeypatch):
    import httpx
    from pydantic import SecretStr

    from tinkerfin_studio.conversation import titles as module
    from tinkerfin_studio.models.schemas import AgentModelConfig

    thread_pk = await create_thread(database)
    config = AgentModelConfig(
        model_id="main",
        display_name="主模型",
        provider="deepseek",
        model_name="deepseek-chat",
        base_url="https://example.invalid",
        api_key=SecretStr("test"),
        reasoning_enabled=False,
    )
    monkeypatch.setattr(module, "_TITLE_MAX_PENDING", 0)
    async with httpx.AsyncClient() as client:
        titles = module.ConversationTitles(
            database=database, http_client=client, http_transport=None
        )
        await titles.start(thread_pk=thread_pk, text="标题", model=config)
        await titles.aclose()
    async with database.session() as session:
        thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert thread is not None and thread.title_generation_status == "failed"
        assert thread.title == "临时标题"


@pytest.mark.parametrize("user_id", [1, 2])
async def test_title_query_checks_owner_and_returns_current_snapshot(
    database, session, user_id
):
    from httpx import ASGITransport, AsyncClient

    from tinkerfin_studio.api.dependencies import get_session, get_user_context
    from tinkerfin_studio.application import create_application
    from tinkerfin_studio.auth.types import UserContext

    await create_thread(database)
    app = create_application(lifespan=None)
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=user_id, username="user", display_name="用户", roles=(), disabled=False
    )
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/conversation/title-thread/title")
    if user_id == 1:
        assert response.status_code == 200
        assert response.json()["data"] == {
            "threadId": "title-thread",
            "title": "临时标题",
            "titleSource": "default",
            "titleGenerationStatus": "idle",
            "titleSeq": 0,
        }
    else:
        assert response.json()["code"] != 0
        assert response.json()["data"] is None
