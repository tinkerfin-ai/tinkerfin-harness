from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import png
from test_skills_library import skill_files

from tinkerfin import AgentRuntime, AgUiResumeRequest, RunIdentity, TinkerFin
from tinkerfin_gateway import (
    CommittedRunEvent,
    Gateway,
    RunAcceptance,
    RunCommand,
    StartRun,
)
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notification, NotificationScope
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    ModelErrorCode,
    ServiceErrorCode,
)
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation import service as service_module
from tinkerfin_studio.conversation.delivery import ConversationAdmission
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
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
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import SearchConfig, ServiceBindings, ServiceSave
from tinkerfin_studio.services.service import ResolvedService, ServiceConfigService
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.packages import parse_package
from tinkerfin_studio.skills.repository import SkillOrigin, SkillRepository
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload
from tinkerfin_tracing import Tracer


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
    session.add(
        User(
            id=1,
            username="test",
            display_name="测试用户",
            password_hash="unused",
            roles=[],
            disabled=False,
        )
    )
    await session.flush()
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
        search_service: ResolvedService | None,
        image_service: ResolvedService | None,
        access_mode: AccessMode,
        skill_snapshot: SkillSnapshotPayload,
    ) -> AgentRuntime[None]:
        del thread_id, model_config, search_service, image_service, access_mode
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


@pytest.mark.parametrize("caller", ["preparer", "service"])
async def test_rejected_first_registration_rolls_back_its_new_conversation(
    notifications, database, session, attachments, monkeypatch, caller
):
    """模型快照失效时，新会话与首条登记一起回滚"""
    request = _ordinary_request()
    changed = _model().model_copy(update={"model_name": "changed"})
    with pytest.raises(BusinessException) as caught:
        if caller == "preparer":
            preparer = ConversationRunPreparer(
                session, user_id=1, attachments=attachments, notifications=notifications
            )
            intent = classify_intent(request)
            resolved = await preparer.resolve_thread(
                thread_id="", run_id=request.run_id, intent=intent
            )
            await preparer.register(
                intent=intent,
                prepared=prepare_run_request(
                    request, user_id=1, thread_id=resolved.thread.thread_id
                ),
                model=changed,
                thread=resolved.thread,
                thread_created=resolved.created,
            )
        else:
            monkeypatch.setattr(
                AgentModelService, "resolve", AsyncMock(return_value=changed)
            )
            service = ConversationChatService(
                session,
                user=UserContext(1, "user", "用户", (), False),
                resources=cast(
                    ApplicationResources,
                    SimpleNamespace(
                        database=database,
                        attachments=attachments,
                        notifications=notifications,
                        conversation_trace=_TraceCoordinator(),
                    ),
                ),
            )
            await service.start(request, last_event_id=None)
    assert caught.value.error_code is ModelErrorCode.CONFIGURATION_CHANGED
    async with database.session() as verification:
        assert list(await verification.scalars(select(ConversationThread))) == []
        assert (
            list(await verification.scalars(select(ConversationRunRegistration))) == []
        )


async def test_conversation_is_announced_only_when_independent_history_can_read_it(
    notifications, database, session, attachments
):
    """准备中不公开会话，受理通知时其他读取者已能读取首条输入"""

    class ReplyModel(FakeListChatModel):
        def bind_tools(self, tools: Sequence[object], **kwargs: object) -> ReplyModel:
            return self

    writer = Tracer(projections=(ConversationFailureProjection(),))
    reader = Tracer(store=writer.store, projections=(ConversationFailureProjection(),))
    runtime = (
        TinkerFin()
        .with_namespace("ns_1")
        .with_observer(writer)
        .build(ReplyModel(responses=["答复"]))
    )
    request = _ordinary_request()
    scope = NotificationScope("ns_1")
    async with (
        notifications.subscribe(scope=scope) as changes,
        Messaging() as messaging,
    ):
        preparer = ConversationRunPreparer(
            session, user_id=1, attachments=attachments, notifications=notifications
        )
        intent = classify_intent(request)
        resolved = await preparer.resolve_thread(
            thread_id="", run_id=request.run_id, intent=intent
        )
        prepared = prepare_run_request(
            request, user_id=1, thread_id=resolved.thread.thread_id
        )
        execution = await preparer.register(
            intent=intent,
            prepared=prepared,
            model=_model(),
            thread=resolved.thread,
            thread_created=resolved.created,
        )
        async with database.session() as check:
            history = ConversationHistoryService(
                ConversationRepository(check),
                user_id=1,
                tracer=reader,
                history_queries=HistoryQueryAdmission(),
            )
            assert (await history.list_history(page_size=10, cursor=None)).items == []
        await notifications.publish(
            Notification(scope=scope, topic="test.boundary", key="prepared")
        )
        boundary = await anext(changes)
        assert isinstance(boundary, Notification) and boundary.topic == "test.boundary"
        admission = ConversationAdmission(
            cast(
                ApplicationResources,
                SimpleNamespace(
                    database=database,
                    attachments=attachments,
                    notifications=notifications,
                ),
            ),
            user_id=1,
            thread_pk=execution.thread.id,
            identity=prepared.identity,
            registered=execution.registered,
            thread_created=execution.thread_created,
        )
        observed: list[str] = []

        class ReadableAdmission:
            async def confirm(self, acceptance: RunAcceptance) -> None:
                assert acceptance.kind == "new"
                await admission.confirm(acceptance)
                async with database.session() as check:
                    history = ConversationHistoryService(
                        ConversationRepository(check),
                        user_id=1,
                        tracer=reader,
                        history_queries=HistoryQueryAdmission(),
                    )
                    listed = await history.list_history(page_size=10, cursor=None)
                    assert [item.thread_id for item in listed.items] == [
                        prepared.identity.thread_id
                    ]
                    detail = await history.get_detail(
                        prepared.identity.thread_id, include_task_trace=False
                    )
                    assert detail.head_run_id == request.run_id
                    assert detail.messages[0].content == "完成任务"
                    observed.append(detail.status.execution)

            async def release(self) -> None:
                await admission.release()

        gateway = Gateway(messaging=messaging, notifications=notifications)
        stream = await gateway.stream(
            runtime,
            StartRun(
                thread_id=prepared.identity.thread_id,
                run_id=request.run_id,
                messages=prepared.messages,
            ),
            registration=ReadableAdmission(),
        )
        async with stream:
            events = [delivery.data.type async for delivery in stream]
        assert observed == ["running"]
        assert events[0] == "RUN_STARTED" and events[-1] == "RUN_FINISHED"
        announced = await anext(changes)
        assert isinstance(announced, Notification)
        assert (announced.topic, announced.key) == (
            "studio.conversation.changed",
            prepared.identity.thread_id,
        )


async def test_run_registration_persists_model_and_input(
    notifications, session, attachments
) -> None:
    request = _ordinary_request()
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(
        session, user_id=1, attachments=attachments, notifications=notifications
    )
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
        thread_id=execution.thread.thread_id,
        thread_pk=execution.thread.id,
        identity_run_id=request.run_id,
        registered=execution.registered,
    )

    assert execution.thread.last_run_id == request.run_id
    assert execution.thread.last_model == "model-main"
    assert execution.thread.status == "running"
    assert registration.status == "starting"


@pytest.mark.parametrize("configured", [False, True])
async def test_resume_keeps_original_service_choice_and_rejects_changed_key(
    notifications, session, attachments, configured
) -> None:
    services = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
    if configured:
        await services.save(
            "web_search",
            ServiceSave(
                configuration=SearchConfig(), api_key=SecretStr("original-key")
            ),
        )
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1, thread_id="service-bindings", title="服务绑定", model_id="model-main"
    )
    request = _ordinary_request(thread_id=thread.thread_id, run_id="source")
    preparer = ConversationRunPreparer(
        session, user_id=1, attachments=attachments, notifications=notifications
    )
    captured = await preparer.register(
        intent=classify_intent(request),
        prepared=prepare_run_request(request, user_id=1, thread_id=thread.thread_id),
        model=_model(),
        thread=thread,
    )
    source = await repository.get_run(thread_pk=thread.id, run_id="source")
    assert source is not None
    bindings = ServiceBindings.model_validate(source.service_bindings)
    assert (bindings.web_search is not None) == configured
    assert "original-key" not in str(source.service_bindings)
    thread.last_run_id = "source"
    thread.status = "waiting_approval"
    thread.has_pending_interrupt = True
    thread.pending_interaction_kind = "tool_approval"
    await repository.commit()
    await services.save(
        "web_search",
        ServiceSave(configuration=SearchConfig(), api_key=SecretStr("replacement-key")),
    )
    if captured.search_service is not None:
        assert captured.search_service.api_key == "original-key"
    resume = _resume_request(thread_id=thread.thread_id, run_id="resume")

    async def register_resume():
        return await preparer.register(
            intent=classify_intent(resume),
            prepared=prepare_run_request(resume, user_id=1, thread_id=thread.thread_id),
            model=_model(),
            thread=thread,
        )

    if configured:
        with pytest.raises(BusinessException) as error:
            await register_resume()
        assert error.value.error_code == ServiceErrorCode.CONFIGURATION_CHANGED
    else:
        restored = await register_resume()
        assert restored.search_service is None
        assert restored.image_service is None


async def test_resume_registration_stores_only_claim_identity(
    notifications, session, attachments
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
    await SkillRepository(session, 1).capture(
        RunIdentity(
            namespace="ns_1", thread_id=thread.thread_id, run_id="run-interrupted"
        )
    )
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
        session, user_id=1, attachments=attachments, notifications=notifications
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
    notifications,
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
            session, user_id=1, attachments=attachments, notifications=notifications
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
    notifications, session, attachments
) -> None:
    request = _ordinary_request(run_id="run-model")
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(
        session, user_id=1, attachments=attachments, notifications=notifications
    )
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


class _Gateway:
    def __init__(self) -> None:
        self.after: int | None = None
        self.commands: list[RunCommand] = []

    async def stream(self, runtime, command, *, after, registration, **kwargs):
        self.after = after
        self.commands.append(command)
        await registration.confirm(
            RunAcceptance(
                runtime.run_identity(command.thread_id, command.run_id), "new"
            )
        )
        from ag_ui.core import RunStartedEvent

        await kwargs["on_committed"](
            CommittedRunEvent(
                runtime.run_identity(command.thread_id, command.run_id),
                RunStartedEvent(thread_id=command.thread_id, run_id=command.run_id),
            )
        )
        return _Body()


class _Body:
    def to_sse(self):
        return self

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

    async def recover(self, *, thread_pk: int | None = None):
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
@pytest.mark.parametrize("with_skill", [False, True])
async def test_chat_accepts_images_and_uses_messaging_only_for_delivery(
    notifications,
    database,
    session,
    attachments,
    image_support,
    with_skill,
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
    content = SkillContentStore(TinkerFin(store=InMemoryStore()))
    selected_ids: list[str] = []
    if with_skill:
        package = parse_package(skill_files())
        await content.save(1, package)
        installed = await SkillRepository(session, 1).install(
            package, SkillOrigin(kind="zip", name="ZIP")
        )
        selected_ids = [installed.id]
        await session.commit()
    channel = _Gateway()
    trace = _TraceCoordinator()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            skills=SimpleNamespace(content=content),
            attachments=attachments,
            model_http_transport=None,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(),
            conversation_channel=channel,
            conversation_trace=trace,
            conversation_titles=AsyncMock(spec=ConversationTitles),
            notifications=notifications,
            gateway=channel,
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
    request = _ordinary_request()
    request.forwarded_props.skill_ids = selected_ids
    payload = request.model_dump()
    payload["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "  用 /reports 技能帮我\n"},
                {
                    "type": "image",
                    "source": {"type": "url", "value": f"attachment:{file.id}"},
                },
            ],
        }
    ]
    prepared = await service.start(
        ChatRequest.model_validate(payload), last_event_id=None
    )
    prepared_body_stream = prepared.stream.to_sse()

    assert channel.after is None
    command = channel.commands[0]
    assert isinstance(command, StartRun)
    assert len(command.messages) == (2 if with_skill else 1)
    blocks = command.messages[0]["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "  用 /reports 技能帮我\n"}
    assert isinstance(blocks[1], dict)
    source = blocks[1]["source"]
    assert isinstance(source, dict) and source["value"] == f"attachment:{file.id}"
    if with_skill:
        assert command.messages[1]["role"] == "user"
        source = command.messages[1]["source"]
        assert isinstance(source, dict) and source["kind"] == "context"
        assert "读取 references/data.bin" in str(command.messages[1]["content"])
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
    assert [chunk async for chunk in prepared_body_stream] == []


async def test_trace_notification_failure_retains_the_running_conversation(
    notifications,
    database,
    session,
    attachments,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要观察失败不撤销已经受理的运行，后续重连仍可读取"""

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
                settings=SimpleNamespace(),
                conversation_channel=channel,
                conversation_trace=trace,
                conversation_titles=AsyncMock(spec=ConversationTitles),
                notifications=notifications,
                gateway=Gateway(
                    messaging=messaging, notifications=notifications, name=channel.name
                ),
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
        async with prepared.stream as output:
            events = [item.data.type.value async for item in output]
        assert events.count("RUN_STARTED") == events.count("RUN_FINISHED") == 1
        assert "RUN_ERROR" not in events
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
    notifications,
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
    channel = _Gateway()
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
            settings=SimpleNamespace(),
            conversation_channel=channel,
            conversation_trace=trace,
            conversation_titles=AsyncMock(spec=ConversationTitles),
            notifications=notifications,
            gateway=channel,
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
    prepared_body_stream = prepared.stream.to_sse()

    assert trace.reconcile_transaction_states == [False]
    assert [chunk async for chunk in prepared_body_stream] == []


@pytest.mark.parametrize("ending", ["title", "finish", "disconnect"])
async def test_title_survives_main_finish_and_response_disconnect(
    notifications,
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
        error = None
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
            database=database,
            http_client=client,
            http_transport=None,
            notifications=notifications,
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
                settings=SimpleNamespace(),
                conversation_channel=messaging.agui_channel(name="conversation"),
                conversation_trace=_TraceCoordinator(),
                conversation_titles=titles,
                notifications=notifications,
                gateway=Gateway(
                    messaging=messaging,
                    notifications=notifications,
                    name="conversation",
                ),
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
        prepared_body_stream = prepared.stream.to_sse()
        try:
            first = await anext(prepared_body_stream)
            assert b'"type":"RUN_STARTED"' in first
            await model_requested.wait()
            # 同Run重连只附着原源；新的响应不拥有标题生成任务
            attached = await service.start(
                _ordinary_request(thread_id=prepared.thread_id), last_event_id="1"
            )
            await attached.stream.aclose()
            assert source_prepares == 1 and model_calls == 1
            if ending == "title":
                model_release.set()
                await title_finished.wait()
                main_release.set()
                tail = await _collect_body(prepared_body_stream)
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
            elif ending == "finish":
                main_release.set()
                tail = await _collect_body(prepared_body_stream)
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
                assert not title_finished.is_set()
            else:
                await prepared.stream.aclose()
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
            await prepared.stream.aclose()
            await titles.aclose()


async def _collect_body(body):
    return [chunk async for chunk in body]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["service-resolution", "runtime"])
@pytest.mark.parametrize(
    "failure_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
async def test_pre_delivery_failure_keeps_only_preexisting_business_registration(
    notifications,
    database,
    session,
    attachments,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    stage: str,
    failure_type: type[BaseException],
) -> None:
    """服务读取或运行构建失败时，只清理本次新建的会话与运行登记"""
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
            session, user_id=1, attachments=attachments, notifications=notifications
        ).register(
            intent=intent,
            prepared=prepared,
            model=_model(),
            thread=thread,
        )
    failure = failure_type("configuration preparation failed")

    def reject_runtime(**_kwargs: object) -> AgentRuntime[None]:
        raise failure

    async def reject_service(
        _service: ServiceConfigService, _capability: str, *, for_update: bool = False
    ) -> ResolvedService | None:
        del for_update
        raise failure

    if stage == "runtime":
        monkeypatch.setattr(
            service_module, "build_conversation_runtime", reject_runtime
        )
    else:
        monkeypatch.setattr(ServiceConfigService, "resolve", reject_service)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            conversation_trace=_TraceCoordinator(),
            conversation_titles=AsyncMock(spec=ConversationTitles),
            notifications=notifications,
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
    notifications,
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
    original_commit = ConversationRepository.commit

    async def commit(repository) -> None:
        if build_failed.is_set():
            cleanup_started.set()
            await release.wait()
        await original_commit(repository)
        if build_failed.is_set() and cleanup_failure is not None:
            raise cleanup_failure

    def reject_runtime(**_kwargs: object) -> AgentRuntime[None]:
        build_failed.set()
        raise build_failure

    monkeypatch.setattr(ConversationRepository, "commit", commit)
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
                notifications=notifications,
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
    notifications, database, session, attachments, monkeypatch, caplog
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
                settings=SimpleNamespace(),
                conversation_channel=messaging.agui_channel(name="failure-logging"),
                conversation_trace=_TraceCoordinator(),
                conversation_titles=AsyncMock(spec=ConversationTitles),
                notifications=notifications,
                gateway=Gateway(
                    messaging=messaging,
                    notifications=notifications,
                    name=messaging.agui_channel(name="failure-logging").name,
                ),
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
            first_body_stream = first.stream.to_sse()
            first_body = b"".join([chunk async for chunk in first_body_stream])
            replay = await service.start(request, last_event_id="0")
            replay_body_stream = replay.stream.to_sse()
            replay_body = b"".join([chunk async for chunk in replay_body_stream])
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
    notifications,
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
                notifications=notifications,
                gateway=Gateway(
                    messaging=messaging,
                    notifications=notifications,
                    name=messaging.agui_channel(name="compact").name,
                ),
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
            prepared_body_stream = prepared.stream.to_sse()
            try:
                bodies.append(b"".join([chunk async for chunk in prepared_body_stream]))
            finally:
                await prepared.stream.aclose()
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
    notifications,
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
            notifications=notifications,
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


async def test_retried_registration_survives_first_submission_cleanup(
    notifications, database, session, attachments
) -> None:
    """并发提交已经共享登记时，最初请求失败不能删除另一提交的运行"""
    request = _ordinary_request(run_id="shared-registration")
    intent = classify_intent(request)
    first = ConversationRunPreparer(
        session, user_id=1, attachments=attachments, notifications=notifications
    )
    resolved = await first.resolve_thread(
        thread_id="", run_id=request.run_id, intent=intent
    )
    prepared = prepare_run_request(
        request, user_id=1, thread_id=resolved.thread.thread_id
    )
    execution = await first.register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=resolved.thread,
        thread_created=resolved.created,
    )
    saved_before_retry = await ConversationRepository(session).get_run(
        thread_pk=execution.thread.id, run_id=request.run_id
    )
    assert saved_before_retry is not None
    await session.commit()
    async with database.session() as retry_session:
        retry = ConversationRunPreparer(
            retry_session,
            user_id=1,
            attachments=attachments,
            notifications=notifications,
        )
        retry_thread = await retry.resolve_thread(
            thread_id=prepared.identity.thread_id, run_id=request.run_id, intent=intent
        )
        attached = await retry.register(
            intent=intent, prepared=prepared, model=_model(), thread=retry_thread.thread
        )
        assert not attached.registered.created
    await first.cleanup_unstarted(
        thread_pk=execution.thread.id,
        thread_id=prepared.identity.thread_id,
        identity_run_id=request.run_id,
        registered=execution.registered,
        thread_created=execution.thread_created,
    )
    async with database.session() as check:
        saved = await ConversationRepository(check).get_run(
            thread_pk=execution.thread.id, run_id=request.run_id
        )
        assert saved is not None
        assert saved.preparation_id != execution.registered.preparation_id
