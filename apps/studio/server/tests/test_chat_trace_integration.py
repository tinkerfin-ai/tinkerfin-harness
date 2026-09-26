from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import png

from tinkerfin import AgentRuntime, AgUiResumeRequest, RunIdentity, TinkerFin
from tinkerfin_messaging import MessageSubscription, Messaging
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation import service as service_module
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    ResumeChatIntent,
    StartChatIntent,
    classify_intent,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.conversation.titles import ConversationTitles
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelConfig,
    AgentModelSave,
    ModelConnectionSave,
)
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources


def _model(model_id: str = "model-main") -> AgentModelConfig:
    return AgentModelConfig(
        model_id=model_id,
        display_name="主模型",
        provider="deepseek",
        provider_id="deepseek",
        model_name="deepseek-chat",
        base_url="https://example.invalid/v1",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
    )


@pytest.fixture(autouse=True)
async def stored_model_configs(session):
    """登记测试使用的真实模型配置，运行登记校验当前配置未变化"""
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_connection(
        ModelConnectionSave(
            connection_id="trace",
            display_name="Trace",
            provider_id="deepseek",
            api_type="openai_chat_completions",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
        )
    )
    for model_id in ("model-main", "model-other"):
        await service.save_settings(
            AgentModelSave.model_validate(
                {
                    **_model(model_id).model_dump(
                        exclude={"provider", "provider_id", "base_url", "api_key"}
                    ),
                    "connection_id": "trace",
                }
            )
        )


@pytest.fixture(autouse=True)
def conversation_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话登记和传输测试使用本地模型，业务工具装配由 Runtime 测试覆盖"""

    class ReplyModel(FakeListChatModel):
        def bind_tools(self, tools: Sequence[object], **kwargs: object) -> ReplyModel:
            del tools, kwargs
            return self

    def build_runtime(
        *,
        resources: ApplicationResources,
        user_id: int,
        thread_id: str,
        model_config: AgentModelConfig,
        image_model: AgentModelConfig | None,
        access_mode: AccessMode,
    ) -> AgentRuntime[None]:
        del thread_id, model_config, image_model, access_mode
        return resources.tinkerfin.with_namespace(f"ns_{user_id}").build(
            model=ReplyModel(responses=["unused"])
        )

    monkeypatch.setattr(service_module, "build_conversation_runtime", build_runtime)


def _ordinary_request(
    *,
    thread_id: str = "",
    run_id: str = "run-1",
    model_id: str = "model-main",
    parent_run_id: str | None = None,
) -> ChatRequest:
    payload = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [{"role": "user", "content": "完成任务"}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "model": model_id,
            "command": {"plan": "off"},
        },
    }
    if parent_run_id is not None:
        payload["parentRunId"] = parent_run_id
    return ChatRequest.model_validate(payload)


def _resume_request(
    *,
    thread_id: str,
    run_id: str,
    model_id: str = "model-main",
) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "threadId": thread_id,
            "runId": run_id,
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "model": model_id,
                "command": {"plan": "off"},
            },
            "resume": [
                {
                    "interruptId": "interrupt-root#0",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )


async def test_run_registration_persists_model_and_input(session, attachments) -> None:
    request = _ordinary_request()
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1, attachments=attachments)
    resolved = await preparer.resolve_thread(
        thread_id=request.thread_id, run_id=request.run_id, intent=intent
    )
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=resolved.thread.thread_id,
    )

    execution = await preparer.register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=resolved.thread,
        thread_created=resolved.created,
    )

    repository = ConversationRepository(session)
    registration = await repository.get_run(
        thread_pk=execution.thread.id,
        run_id=request.run_id,
    )
    assert registration is not None
    assert registration.model_id == "model-main"
    assert registration.input_json == prepared.input_json
    assert execution.thread.last_run_id is None
    assert execution.thread.status == "idle"

    await preparer.activate_started(
        thread_pk=execution.thread.id,
        identity_run_id=request.run_id,
        registered=execution.registered,
    )

    assert execution.thread.last_run_id == request.run_id
    assert execution.thread.last_model == "model-main"
    assert execution.thread.status == "running"
    assert registration.status == "starting"


async def test_resume_registration_stores_only_claim_identity(
    session, attachments
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id="thread-resume",
        title="恢复会话",
        model_id="model-main",
    )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-interrupted",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-interrupted"},
    )
    thread.last_run_id = "run-interrupted"
    thread.status = "waiting_approval"
    thread.has_pending_interrupt = True
    thread.pending_interaction_kind = "tool_approval"
    await repository.commit()
    request = _resume_request(thread_id=thread.thread_id, run_id="run-resume")
    intent = classify_intent(request)
    assert isinstance(intent, ResumeChatIntent)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=thread.thread_id,
    )

    execution = await ConversationRunPreparer(
        session, user_id=1, attachments=attachments
    ).register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=thread,
    )

    assert isinstance(execution.resume, AgUiResumeRequest)
    claims = await repository.list_claims_for_update(
        thread_pk=thread.id,
        interrupt_ids=frozenset({"interrupt-root#0"}),
    )
    assert len(claims) == 1
    assert claims[0].source_run_id == "run-interrupted"
    assert claims[0].claimed_run_id == "run-resume"
    assert not hasattr(claims[0], "request_json")
    assert not hasattr(claims[0], "resume_json")


@pytest.mark.parametrize("continuation", ["resume", "branch"])
@pytest.mark.parametrize("changed", ["model", "access"])
async def test_continuation_preserves_source_model_and_file_access(
    session,
    continuation: str,
    changed: str,
    attachments,
) -> None:
    selected_model = "model-other" if changed == "model" else "model-main"
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id=f"thread-model-fence-{continuation}",
        title="模型继承",
        model_id="model-main",
    )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-source",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-source"},
    )
    thread.last_run_id = "run-source"
    if continuation == "resume":
        thread.status = "waiting_approval"
        thread.has_pending_interrupt = True
        thread.pending_interaction_kind = "tool_approval"
        request = _resume_request(
            thread_id=thread.thread_id,
            run_id="run-continuation",
            model_id=selected_model,
        )
    else:
        request = _ordinary_request(
            thread_id=thread.thread_id,
            run_id="run-continuation",
            model_id=selected_model,
            parent_run_id="run-source",
        )
    if changed == "access":
        request.forwarded_props.access_mode = "write_approval"
    thread_pk = thread.id
    await repository.commit()
    intent = classify_intent(request)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=thread.thread_id,
    )

    with pytest.raises(BusinessException) as captured:
        await ConversationRunPreparer(
            session, user_id=1, attachments=attachments
        ).register(
            intent=intent,
            prepared=prepared,
            model=_model(selected_model),
            thread=thread,
        )

    assert captured.value.error_code is ConversationErrorCode.RUN_IDENTITY_CONFLICT
    assert (
        await repository.get_run(
            thread_pk=thread_pk,
            run_id="run-continuation",
        )
        is None
    )


async def test_same_run_rejects_a_changed_registered_model(
    session, attachments
) -> None:
    request = _ordinary_request(run_id="run-model")
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1, attachments=attachments)
    resolved = await preparer.resolve_thread(
        thread_id=request.thread_id, run_id=request.run_id, intent=intent
    )
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=resolved.thread.thread_id,
    )
    execution = await preparer.register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=resolved.thread,
        thread_created=resolved.created,
    )
    repository = ConversationRepository(session)
    registration = await repository.get_run(
        thread_pk=execution.thread.id,
        run_id=request.run_id,
    )
    assert registration is not None
    registration.model_id = "model-other"
    await repository.commit()

    with pytest.raises(BusinessException) as captured:
        await preparer.register(
            intent=intent,
            prepared=prepared,
            model=_model(),
            thread=execution.thread,
        )

    assert captured.value.error_code is ConversationErrorCode.RUN_IDENTITY_CONFLICT


class _Channel:
    def __init__(self) -> None:
        self.after: int | None = None

    async def open_sse(
        self,
        _source,
        *,
        after: int | None = None,
        on_source_ready=None,
        on_subscribed=None,
        transform_event=None,
        on_run_started=None,
        on_run_finished=None,
        on_delivery_not_started=None,
    ):
        del on_delivery_not_started
        self.after = after
        if on_source_ready is not None:
            await on_source_ready()
        if on_subscribed is not None:
            await on_subscribed()

        return _Body()


class _Body:
    def __aiter__(self) -> _Body:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        pass


class _TraceCoordinator:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.ensured: list[RunIdentity] = []
        self._session = session
        self.reconcile_transaction_states: list[bool] = []

    async def recover_preparing(self, *, thread_pk: int | None = None):
        del thread_pk
        return frozenset()

    async def reconcile(self, *, thread_pk: int, identity: RunIdentity) -> int:
        del thread_pk, identity
        if self._session is not None:
            self.reconcile_transaction_states.append(self._session.in_transaction())
        return 1

    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        del thread_pk
        self.ensured.append(identity)


class _FailingTraceCoordinator(_TraceCoordinator):
    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        super().ensure(thread_pk=thread_pk, identity=identity)
        raise RuntimeError("trace follow unavailable")


@pytest.mark.parametrize("image_support", ["supported", "unsupported", "unknown"])
async def test_chat_accepts_images_and_uses_messaging_only_for_delivery(
    database,
    session,
    attachments,
    image_support,
) -> None:
    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            connection_id="deepseek",
            model_id="model-main",
            display_name="主模型",
            model_name="deepseek-chat",
            enabled=True,
            is_default=True,
            image_support=image_support,
        )
    )
    channel = _Channel()
    trace = _TraceCoordinator()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            model_http_transport=None,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
            conversation_channel=channel,
            conversation_trace=trace,
            conversation_titles=AsyncMock(spec=ConversationTitles),
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1,
            username="user",
            display_name="用户",
            roles=(),
            disabled=False,
        ),
        resources=resources,
    )

    file = await attachments.upload(
        user_id=1, name="image.png", chunks=byte_chunks(png())
    )
    payload = _ordinary_request().model_dump()
    payload["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "url", "value": f"attachment:{file.id}"},
                }
            ],
        }
    ]
    prepared = await service.start(
        ChatRequest.model_validate(payload), last_event_id=None
    )

    assert channel.after is None
    assert len(trace.ensured) == 1
    assert trace.ensured[0].run_id == "run-1"
    repository = ConversationRepository(session)
    thread = await repository.get_thread(user_id=1, thread_id=prepared.thread_id)
    assert thread is not None
    assert thread.last_run_id == "run-1"
    assert thread.status == "running"
    assert [
        item.id
        for item in await attachments.list_thread(user_id=1, thread_id=thread.thread_id)
    ] == [file.id]
    assert [chunk async for chunk in prepared.body] == []


async def test_trace_notification_failure_retains_the_running_conversation(
    database,
    session,
    attachments,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要跟随后台任务注册失败时释放订阅，保留已开始的会话供重连"""

    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            connection_id="deepseek",
            model_id="model-main",
            display_name="主模型",
            model_name="deepseek-chat",
            enabled=True,
            is_default=True,
        )
    )
    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="conversations")
        trace = _FailingTraceCoordinator()
        detached: list[MessageSubscription[object]] = []
        close = MessageSubscription.aclose

        async def close_subscription(subscription: MessageSubscription[object]) -> None:
            await close(subscription)
            detached.append(subscription)

        monkeypatch.setattr(MessageSubscription, "aclose", close_subscription)
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                model_http_transport=None,
                model_http_client=None,
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(),
                settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
                conversation_channel=channel,
                conversation_trace=trace,
                conversation_titles=AsyncMock(spec=ConversationTitles),
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=1,
                username="user",
                display_name="用户",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )

        with pytest.raises(RuntimeError, match="trace follow unavailable"):
            await service.start(_ordinary_request(), last_event_id=None)
        assert len(detached) == 1
        with pytest.raises(RuntimeError, match="subscription is closed"):
            aiter(detached[0])
        identity = trace.ensured[0]
        repository = ConversationRepository(session)
        thread = await repository.get_thread(user_id=1, thread_id=identity.thread_id)
        assert thread is not None and thread.last_run_id == identity.run_id
        assert await repository.get_run(thread_pk=thread.id, run_id=identity.run_id)
        replay = await channel.follow(identity=identity)
        events = [item.data.type.value async for item in replay]
        assert events.count("RUN_STARTED") == events.count("RUN_FINISHED") == 1
        assert "RUN_ERROR" not in events
        assert await channel.get_run_status(identity=identity) == "completed"


async def test_previous_head_reconcile_releases_the_request_transaction(
    database,
    session,
    attachments,
) -> None:
    """共享 Trace 查询前必须归还业务 Session 的池连接"""

    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            connection_id="deepseek",
            model_id="model-main",
            display_name="主模型",
            model_name="deepseek-chat",
            enabled=True,
            is_default=True,
        )
    )
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id="thread-existing",
        title="已有会话",
        model_id="model-main",
    )
    previous = await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-previous",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-previous"},
    )
    previous.status = "succeeded"
    thread.last_run_id = previous.run_id
    thread.status = "idle"
    await repository.commit()
    channel = _Channel()
    trace = _TraceCoordinator(session)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            model_http_transport=None,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
            conversation_channel=channel,
            conversation_trace=trace,
            conversation_titles=AsyncMock(spec=ConversationTitles),
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1,
            username="user",
            display_name="用户",
            roles=(),
            disabled=False,
        ),
        resources=resources,
    )

    prepared = await service.start(
        _ordinary_request(thread_id=thread.thread_id, run_id="run-next"),
        last_event_id=None,
    )

    assert trace.reconcile_transaction_states == [False]
    assert [chunk async for chunk in prepared.body] == []


@pytest.mark.parametrize("ending", ["title", "finish", "disconnect"])
async def test_title_survives_main_finish_and_response_disconnect(
    database,
    session,
    attachments,
    monkeypatch,
    ending,
):
    """真实聊天传输与模型请求验证标题独立完成，主回复不等待标题"""
    import json
    from collections.abc import AsyncGenerator

    import httpx
    from ag_ui.core import BaseEvent, RunFinishedEvent, RunStartedEvent

    from tinkerfin_messaging import Messaging

    main_release, model_requested, model_release = (asyncio.Event() for _ in range(3))
    model_calls = 0
    source_prepares = 0

    class Source:
        messaging_cancel_waits_for_first_item = True
        messaging_codec_profile = "agui.event"
        messaging_source_type = BaseEvent
        messaging_replay_type = BaseEvent

        def __init__(self, identity: RunIdentity):
            self.messaging_identity = identity
            self.iterator = self.events()

        async def messaging_owner_preflight(self) -> None:
            nonlocal source_prepares
            source_prepares += 1

        async def events(self) -> AsyncGenerator[BaseEvent, None]:
            identity = self.messaging_identity
            yield RunStartedEvent(thread_id=identity.thread_id, run_id=identity.run_id)
            await main_release.wait()
            yield RunFinishedEvent(thread_id=identity.thread_id, run_id=identity.run_id)

        def __aiter__(self):
            return self.iterator

        async def aclose(self):
            await self.iterator.aclose()

        async def messaging_cancel_callback(self, context):
            del context
            identity = self.messaging_identity
            return [
                RunFinishedEvent(thread_id=identity.thread_id, run_id=identity.run_id)
            ]

    def open_run(self: AgentRuntime[None], *, thread_id: str, run_id: str, **kwargs):
        del kwargs
        return Source(self.run_identity(thread_id, run_id))

    async def model_http(request):
        nonlocal model_calls
        del request
        model_calls += 1
        model_requested.set()
        await model_release.wait()
        payload = {
            "id": "title",
            "object": "chat.completion.chunk",
            "model": "deepseek-chat",
            "choices": [
                {"index": 0, "delta": {"content": "任务标题"}, "finish_reason": "stop"}
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
        )

    monkeypatch.setattr(AgentRuntime, "open_agui_run", open_run)
    async with (
        Messaging() as messaging,
        httpx.AsyncClient(transport=httpx.MockTransport(model_http)) as client,
    ):
        titles = ConversationTitles(
            database=database, http_client=client, http_transport=None
        )
        title_finished = asyncio.Event()
        from tinkerfin_studio.conversation import titles as titles_module

        original_summary = titles_module.summarize_conversation_title

        async def summarize(**kwargs):
            try:
                return await original_summary(**kwargs)
            finally:
                title_finished.set()

        monkeypatch.setattr(titles_module, "summarize_conversation_title", summarize)
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                model_http_transport=None,
                model_http_client=client,
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(),
                settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
                conversation_channel=messaging.agui_channel(name="conversation"),
                conversation_trace=_TraceCoordinator(),
                conversation_titles=titles,
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=1,
                username="user",
                display_name="用户",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )
        prepared = await service.start(_ordinary_request(), last_event_id=None)
        try:
            first = await anext(prepared.body)
            assert b'"type":"RUN_STARTED"' in first
            await model_requested.wait()
            # 同Run重连只附着原源；新的响应不拥有标题生成任务
            attached = await service.start(
                _ordinary_request(thread_id=prepared.thread_id), last_event_id="1"
            )
            await attached.body.aclose()
            assert source_prepares == 1 and model_calls == 1
            if ending == "title":
                model_release.set()
                await title_finished.wait()
                main_release.set()
                tail = await _collect_body(prepared.body)
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
            elif ending == "finish":
                main_release.set()
                tail = await _collect_body(prepared.body)
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
                assert not title_finished.is_set()
            else:
                await prepared.body.aclose()
                assert not title_finished.is_set()
                assert (
                    await resources.conversation_channel.get_run_status(
                        identity=RunIdentity(
                            namespace="ns_1",
                            thread_id=prepared.thread_id,
                            run_id="run-1",
                        )
                    )
                    == "running"
                )
            model_release.set()
            await title_finished.wait()
            async with database.session() as check:
                thread = await ConversationRepository(check).get_thread(
                    user_id=1, thread_id=prepared.thread_id
                )
                assert thread is not None
                assert thread.title_generation_status == "succeeded"
                assert thread.title == "任务标题" and thread.title_seq == 2
        finally:
            main_release.set()
            model_release.set()
            await prepared.body.aclose()
            await titles.aclose()


async def _collect_body(body):
    return [chunk async for chunk in body]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["image-model", "runtime"])
@pytest.mark.parametrize(
    "failure_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
async def test_pre_delivery_failure_keeps_only_preexisting_business_registration(
    database,
    session,
    attachments,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    stage: str,
    failure_type: type[BaseException],
) -> None:
    """模型读取或运行构建失败时，只清理本次新建的会话与运行登记"""
    request = _ordinary_request(run_id="setup-failure")
    if existing:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            thread_id="existing-conversation",
            title="已有会话",
            model_id="model-main",
        )
        await repository.commit()
        request = _ordinary_request(thread_id=thread.thread_id, run_id="setup-failure")
        intent = classify_intent(request)
        assert isinstance(intent, StartChatIntent)
        prepared = prepare_run_request(request, user_id=1, thread_id=thread.thread_id)
        await ConversationRunPreparer(
            session, user_id=1, attachments=attachments
        ).register(
            intent=intent,
            prepared=prepared,
            model=_model(),
            thread=thread,
        )
    failure = failure_type("configuration preparation failed")

    def reject_runtime(**_kwargs: object) -> AgentRuntime[None]:
        raise failure

    async def reject_image_model(
        _service: AgentModelService,
    ) -> AgentModelConfig | None:
        raise failure

    if stage == "runtime":
        monkeypatch.setattr(
            service_module, "build_conversation_runtime", reject_runtime
        )
    else:
        monkeypatch.setattr(
            AgentModelService, "resolve_image_model", reject_image_model
        )
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            conversation_trace=_TraceCoordinator(),
            conversation_titles=AsyncMock(spec=ConversationTitles),
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1, username="user", display_name="用户", roles=(), disabled=False
        ),
        resources=resources,
    )
    with pytest.raises(failure_type) as caught:
        await service.start(request, last_event_id=None)
    assert caught.value is failure
    async with database.session() as verification:
        runs = list(
            (await verification.scalars(select(ConversationRunRegistration))).all()
        )
        threads = list((await verification.scalars(select(ConversationThread))).all())
    if existing:
        assert [(run.run_id, run.status) for run in runs] == [
            ("setup-failure", "preparing")
        ]
        assert [thread.thread_id for thread in threads] == ["existing-conversation"]
    else:
        assert runs == []
        assert threads == []


@pytest.mark.parametrize("cancel_count", [0, 1, 2])
@pytest.mark.parametrize(
    "cleanup_failure_type", [None, OSError, KeyboardInterrupt, SystemExit]
)
async def test_business_registration_cleanup_settles_before_request_cancellation(
    database,
    session,
    attachments,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
    cleanup_failure_type: type[BaseException] | None,
) -> None:
    """清理提交期间的请求取消不回滚删除，并保留构建与清理的原始异常"""
    build_failed, cleanup_started, release = (asyncio.Event() for _ in range(3))
    build_failure = ValueError("runtime configuration rejected")
    build_cause = LookupError("configuration source")
    build_failure.__cause__ = build_cause
    cleanup_failure = (
        cleanup_failure_type("cleanup provider failure")
        if cleanup_failure_type is not None
        else None
    )
    cleanup_cause = LookupError("cleanup original cause")
    if cleanup_failure is not None:
        cleanup_failure.__cause__ = cleanup_cause
    original_commit = session.commit

    async def commit() -> None:
        if build_failed.is_set():
            cleanup_started.set()
            await release.wait()
        await original_commit()
        if build_failed.is_set() and cleanup_failure is not None:
            raise cleanup_failure

    def reject_runtime(**_kwargs: object) -> AgentRuntime[None]:
        build_failed.set()
        raise build_failure

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(service_module, "build_conversation_runtime", reject_runtime)
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1, username="user", display_name="用户", roles=(), disabled=False
        ),
        resources=cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                conversation_trace=_TraceCoordinator(),
                conversation_titles=AsyncMock(spec=ConversationTitles),
            ),
        ),
    )

    async def start_request() -> BaseException:
        try:
            await service.start(
                _ordinary_request(run_id="cleanup-failure"), last_event_id=None
            )
        except BaseException as error:  # noqa: BLE001 - 在请求调用方检查控制异常
            return error
        raise AssertionError("构建失败必须交回调用方")

    request = asyncio.create_task(start_request())
    try:
        await cleanup_started.wait()
        for index in range(cancel_count):
            request.cancel(f"client cancellation {index + 1}")
        assert not request.done()
        release.set()
        outcome = await request
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    await session.rollback()
    if cleanup_failure is not None and not isinstance(cleanup_failure, Exception):
        assert outcome is cleanup_failure
    elif cancel_count:
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert outcome is (
            build_failure if cleanup_failure is None else cleanup_failure
        )

    seen: set[int] = set()
    active: set[int] = set()

    def visit(error: BaseException) -> None:
        assert id(error) not in active, "异常图不应包含循环"
        if id(error) in seen:
            return
        seen.add(id(error))
        active.add(id(error))
        for nested in (error.__cause__, error.__context__):
            if nested is not None:
                visit(nested)
        if isinstance(error, BaseExceptionGroup):
            for nested in error.exceptions:
                visit(nested)
        active.remove(id(error))

    visit(outcome)
    assert id(build_failure) in seen and id(build_cause) in seen
    if cleanup_failure is not None:
        assert id(cleanup_failure) in seen and id(cleanup_cause) in seen
    async with database.session() as verification:
        assert (
            list(
                (await verification.scalars(select(ConversationRunRegistration))).all()
            )
            == []
        )
        assert (
            list((await verification.scalars(select(ConversationThread))).all()) == []
        )


pytestmark = pytest.mark.usefixtures("model_connections")


async def test_initialization_error_is_logged_once_and_replay_does_not_log_again(
    database, session, attachments, monkeypatch, caplog
):
    """初始化失败记录原始异常和会话身份，历史重放不再次记录错误"""
    import logging

    from tinkerfin_messaging import Messaging

    build_count = 0

    def failed_graph(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        raise RuntimeError("sandbox connection unavailable")

    monkeypatch.setattr("tinkerfin.deep_agent.create_agent_graph", failed_graph)
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1, thread_id="thread-log", title="日志验证", model_id="model-main"
    )
    thread.title_source = "user"
    await repository.commit()
    async with Messaging() as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                model_http_transport=None,
                model_http_client=None,
                tinkerfin=TinkerFin(),
                settings=SimpleNamespace(model_allowed_origins=()),
                conversation_channel=messaging.agui_channel(name="failure-logging"),
                conversation_trace=_TraceCoordinator(),
                conversation_titles=AsyncMock(spec=ConversationTitles),
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=1,
                username="user",
                display_name="用户",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )
        with caplog.at_level(logging.ERROR):
            request = _ordinary_request(thread_id="thread-log", run_id="run-log")
            first = await service.start(request, last_event_id=None)
            first_body = b"".join([chunk async for chunk in first.body])
            replay = await service.start(request, last_event_id="0")
            replay_body = b"".join([chunk async for chunk in replay.body])
        assert b'"type":"RUN_ERROR"' in first_body
        assert b'"type":"RUN_ERROR"' in replay_body
        records = [
            r for r in caplog.records if r.name.endswith("conversation.error_logging")
        ]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "thread_id=thread-log" in message
        assert "run_id=run-log" in message
        assert "model_id=model-main" in message
        assert "RuntimeError: sandbox connection unavailable" in message
        assert build_count == 1


async def test_manual_compaction_uses_registered_run_replay_without_chat_or_title(
    database,
    session,
    attachments,
    monkeypatch,
):
    """真实框架压缩使用现有运行登记，重复请求只重放并保留原始消息"""
    from deepagents.backends import StateBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_agent_runtime import _ToolModel

    from tinkerfin_messaging import Messaging
    from tinkerfin_studio.conversation.request import CompactRequest
    from tinkerfin_studio.conversation.todo_groups import (
        TODO_PROJECTION,
        TodoGroupProjection,
        TodoGroupProjectionResult,
        render_task_trace,
    )
    from tinkerfin_tracing import Tracer

    model = _ToolModel(responses=["最近回复", "保留项目目标与报告要求", "继续回复"])
    tracer = Tracer(projections=(TodoGroupProjection(),))
    factory = TinkerFin(checkpointer=InMemorySaver()).with_observer(tracer)
    runtime = factory.with_namespace("ns_1").build(
        model=model,
        middleware=[
            SummarizationMiddleware(
                model,
                backend=StateBackend(),
                trigger=("messages", 6),
                keep=("messages", 1),
            )
        ],
    )
    await runtime.ainvoke(
        thread_id="compact-thread",
        run_id="seed",
        input={
            "messages": [
                HumanMessage(content="项目要求 " * 400),
                AIMessage(content="调查结果 " * 400),
                HumanMessage(content="整理后继续"),
            ]
        },
    )
    before = (await runtime.agui.history(tracer).get("compact-thread")).snapshot
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1, thread_id="compact-thread", title="项目", model_id="model-main"
    )
    thread.last_access_mode = "write_approval"
    await repository.commit()
    monkeypatch.setattr(
        service_module, "build_conversation_runtime", lambda **_kwargs: runtime
    )
    titles = AsyncMock(spec=ConversationTitles)
    async with Messaging() as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                conversation_channel=messaging.agui_channel(name="compact"),
                conversation_trace=_TraceCoordinator(),
                conversation_titles=titles,
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=1,
                username="user",
                display_name="用户",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )
        request = CompactRequest(runId="compact-run", model="model-main")
        bodies = []
        for _ in range(2):
            prepared = await service.start(
                request, thread_id="compact-thread", last_event_id="0"
            )
            try:
                bodies.append(b"".join([chunk async for chunk in prepared.body]))
            finally:
                await prepared.body.aclose()
        assert bodies[0] == bodies[1]
        assert b'"type":"RUN_FINISHED"' in bodies[0]
        assert b"TEXT_MESSAGE_" not in bodies[0]
        assert model.i == 2
        titles.start.assert_not_called()
        after = (await runtime.agui.history(tracer).get("compact-thread")).snapshot
        assert before.messages == after.messages
        trace = await tracer.get(
            runtime.thread_identity("compact-thread"),
            projections=(TODO_PROJECTION,),
        )
        task_trace = render_task_trace(
            TodoGroupProjectionResult.model_validate(
                trace.projections[TODO_PROJECTION]
            ),
            status=trace.status,
            completeness=trace.completeness,
        )
        assert task_trace.status == "ready"
        assert not task_trace.todo_groups
        registration = await repository.get_run(
            thread_pk=thread.id, run_id="compact-run"
        )
        assert registration is not None
        assert registration.input_json == {
            "operation": "compact",
            "threadId": "compact-thread",
            "runId": "compact-run",
            "model": "model-main",
        }
        assert registration.access_mode == "write_approval"
        with pytest.raises(BusinessException) as caught:
            await service.start(
                CompactRequest(runId="compact-run", model="model-other"),
                thread_id="compact-thread",
                last_event_id="0",
            )
        assert caught.value.error_code == ConversationErrorCode.RUN_IDENTITY_CONFLICT


@pytest.mark.parametrize("condition", ["missing", "another-user", "busy", "approval"])
async def test_compaction_respects_conversation_ownership_and_pending_work(
    database,
    session,
    attachments,
    condition,
):
    """会话归属、正在执行和待审批沿用聊天的业务约束"""
    from tinkerfin_studio.conversation.request import CompactRequest

    repository = ConversationRepository(session)
    if condition != "missing":
        thread = await repository.create_thread(
            user_id=2 if condition == "another-user" else 1,
            thread_id="compact-thread",
            title="项目",
            model_id="model-main",
        )
        if condition == "busy":
            thread.last_run_id = "busy-run"
            thread.status = "running"
        elif condition == "approval":
            thread.has_pending_interrupt = True
            thread.status = "waiting_approval"
        await repository.commit()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            conversation_trace=_TraceCoordinator(),
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1, username="user", display_name="用户", roles=(), disabled=False
        ),
        resources=resources,
    )
    with pytest.raises(BusinessException) as caught:
        await service.start(
            CompactRequest(runId="compact", model="model-main"),
            thread_id="compact-thread",
            last_event_id=None,
        )
    assert (
        caught.value.error_code
        == {
            "missing": ConversationErrorCode.NOT_FOUND,
            "another-user": ConversationErrorCode.NOT_FOUND,
            "busy": ConversationErrorCode.RUN_CONFLICT,
            "approval": ConversationErrorCode.PENDING_INTERRUPT,
        }[condition]
    )
