"""贡献者可在隔离 SQLite 和本地模型上运行的自动化接口闭环"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from test_agent_runtime import _ToolModel, _Workspace
from test_attachments import png

from tinkerfin import TinkerFin
from tinkerfin_automation import (
    Automation,
    SqlAlchemyAutomationStore,
    next_run_after,
)
from tinkerfin_gateway import Gateway
from tinkerfin_messaging import Messaging
from tinkerfin_notifications import Notification, NotificationScope
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.api.dependencies import get_user_context
from tinkerfin_studio.api.errors import AttachmentErrorCode, BusinessException
from tinkerfin_studio.application import create_application
from tinkerfin_studio.attachments.entity import AttachmentCollection
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.automation import target as target_module
from tinkerfin_studio.automation.ownership import automation_execution_namespace
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import NAMESPACE, StudioAutomationService
from tinkerfin_studio.automation.target import (
    StudioAutomationTarget,
    fail_interactive_execution,
)
from tinkerfin_studio.conversation.command import ConversationCommandService
from tinkerfin_studio.conversation.coordinator import ConversationTraceCoordinator
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.conversation.todo_groups import TodoGroupProjection
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import SearchConfig, ServiceSave
from tinkerfin_studio.services.service import ServiceConfigService
from tinkerfin_tracing import Tracer


@pytest.fixture
async def automation_environment(
    database,
    components_database,
    attachments,
    skill_library,
    monkeypatch,
    projects,
    persistent_store,
    notifications,
):
    async with database.session() as session:
        session.add(
            User(
                id=1,
                username="owner",
                password_hash="not-used",
                disabled=False,
            )
        )
        session.add(
            ModelConnection(
                user_id=1,
                connection_id="auto",
                display_name="自动化",
                provider_id="custom",
                api_type="openai_chat_completions",
                base_url="https://model.invalid/v1",
                auth_type="api_key",
                api_key="key",
            )
        )
        session.add(
            AgentModel(
                user_id=1,
                model_id="main",
                display_name="Main",
                connection_id="auto",
                model_name="test-model",
                enabled=True,
                is_default=True,
            )
        )
        await session.commit()
    monkeypatch.setattr(
        runtime_module,
        "create_chat_model",
        lambda *args, **kwargs: _ToolModel(responses=["任务完成"]),
    )
    workspace = _Workspace()

    class Sandboxes:
        def workspace(self, key, *, workspace_key, routes):
            assert key == "users/1"
            assert workspace_key == "project-1"
            return workspace

    store = SqlAlchemyAutomationStore(
        components_database.engine, notifications=notifications
    )
    automation = Automation(namespace=NAMESPACE, store=store)
    try:
        tracer = Tracer(
            projections=(ConversationFailureProjection(), TodoGroupProjection())
        )
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(500))
            ) as client,
            Messaging() as messaging,
        ):
            channel = messaging.agui_channel(name="automation-test")
            gateway = Gateway(
                messaging=messaging, notifications=notifications, name=channel.name
            )
            coordinator = ConversationTraceCoordinator(
                database=database,
                notifications=notifications,
                tracer=tracer,
                conversation_channel=channel,
            )
            resources = cast(
                ApplicationResources,
                SimpleNamespace(
                    database=database,
                    components_database=components_database,
                    attachments=attachments,
                    skills=skill_library,
                    automation=automation,
                    tracer=tracer,
                    notifications=notifications,
                    gateway=gateway,
                    messaging=messaging,
                    conversation_channel=channel,
                    conversation_trace=coordinator,
                    history_queries=HistoryQueryAdmission(),
                    tinkerfin=TinkerFin(
                        checkpointer=InMemorySaver(), store=persistent_store
                    ).with_observer(tracer),
                    model_http_transport=None,
                    model_http_client=client,
                    sandbox_manager=Sandboxes(),
                    agent_subagents={},
                    settings=SimpleNamespace(),
                ),
            )
            automation.target(
                "studio_agent",
                StudioAutomationTarget(resources),
                execution_namespace=automation_execution_namespace,
            )
            try:
                async with automation.worker(
                    on_interrupt=fail_interactive_execution
                ) as worker:
                    yield resources, worker
            finally:
                await coordinator.aclose()
    finally:
        await store.close()


@pytest.fixture
async def automation_resources(automation_environment):
    return automation_environment[0]


@pytest.fixture
async def automation_worker(automation_environment):
    return automation_environment[1]


def configuration():
    return {
        "projectId": "project-1",
        "name": "日报",
        "prompt": "生成日报",
        "modelId": "main",
        "accessMode": "full",
        "schedule": {
            "kind": "once",
            "date": "2099-09-22",
            "time": "09:00",
        },
        "attachments": [],
        "startsOn": None,
        "endsOn": None,
    }


@pytest.mark.parametrize("missing_binding", [False, True])
async def test_explicit_retry_preserves_service_binding_and_new_run_uses_current_config(
    automation_resources, automation_worker, monkeypatch, missing_binding
):
    resources = automation_resources
    async with resources.database.session() as session:
        services = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
        await services.save(
            "web_search",
            ServiceSave(
                configuration=SearchConfig(), api_key=SecretStr("original-key")
            ),
        )
    service = StudioAutomationService(resources, project_id="project-1", user_id=1)
    task = await service.save(
        SaveTask(
            request_id="bound-task",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    builder = target_module.build_automation_runtime

    def fail_runtime(**kwargs):
        raise ValueError("runtime setup failed")

    monkeypatch.setattr(target_module, "build_automation_runtime", fail_runtime)
    handle = await resources.automation.for_owner("1:project-1").task(task.id)
    original = await handle.run()
    await automation_worker.wait_until_idle()
    snapshot = await resources.attachments.collection_configuration(
        user_id=1, collection_id=original.id
    )
    assert snapshot is not None and snapshot["services"] is not None
    assert "original-key" not in str(snapshot)
    async with resources.database.session() as session:
        services = ServiceConfigService(ServiceConfigRepository(session, user_id=1))
        await services.save(
            "web_search",
            ServiceSave(
                configuration=SearchConfig(), api_key=SecretStr("replacement-key")
            ),
        )
    if missing_binding:
        async with resources.database.session() as session:
            row = await session.get(AttachmentCollection, original.id)
            assert row is not None
            row.configuration = {**row.configuration, "services": None}
            await session.commit()
    monkeypatch.setattr(target_module, "build_automation_runtime", builder)
    original = await resources.automation.for_owner("1:project-1").get_run(original.id)
    retried = await original.retry()
    await automation_worker.wait_until_idle()
    retry_result = await service.result(retried.id)
    assert retry_result.status == "failed"
    assert retry_result.error == "原执行使用的服务配置已变化，请新建一次运行"
    fresh = await handle.run()
    await automation_worker.wait_until_idle()
    assert (await service.result(fresh.id)).status == "succeeded"


@pytest.mark.parametrize("clock_offset", [-400_000, 400_000])
def test_interval_first_occurrence_waits_one_period_from_saved_anchor(clock_offset):
    anchor = datetime(2026, 9, 23, 1, 52, 44, tzinfo=UTC)
    config = TaskConfiguration.model_validate(
        {
            **configuration(),
            "schedule": {"kind": "interval", "every": 5, "unit": "minutes"},
        }
    )
    schedule = config.framework_schedule(anchor)
    assert next_run_after(
        schedule, anchor + timedelta(microseconds=clock_offset)
    ) == anchor + timedelta(minutes=5)


def test_interval_with_start_date_keeps_the_requested_midnight():
    anchor = datetime(2026, 9, 23, 1, 52, 44, tzinfo=UTC)
    config = TaskConfiguration.model_validate(
        {
            **configuration(),
            "schedule": {"kind": "interval", "every": 5, "unit": "minutes"},
            "startsOn": "2026-09-24",
        }
    )
    assert next_run_after(config.framework_schedule(anchor), anchor) == datetime(
        2026, 9, 23, 16, tzinfo=UTC
    )


async def test_http_crud_idempotency_real_result_and_owner_isolation(
    automation_resources,
    automation_worker,
):
    resources = automation_resources
    application = create_application(lifespan=None)
    application.state.resources = resources
    user = UserContext(user_id=1, username="owner", roles=(), disabled=False)
    application.dependency_overrides[get_user_context] = lambda: user
    async with (
        resources.notifications.subscribe(
            scope=NotificationScope("ns_1"), topics=("studio.memories.changed",)
        ) as memory_changes,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            params={"projectId": "project-1"},
        ) as client,
    ):
        payload = {"requestId": "create", "configuration": configuration()}
        created = await client.post("/api/automation/tasks", json=payload)
        assert created.status_code == 200, created.text
        task = created.json()["data"]
        repeat = await client.post("/api/automation/tasks", json=payload)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["data"]["id"] == task["id"]
        assert (
            len(
                (
                    await client.get("/api/automation/tasks", params={"query": "日报"})
                ).json()["data"]["items"]
            )
            == 1
        )
        command = {"requestId": "run", "expectedRevision": task["revision"]}
        queued = await client.post(
            f"/api/automation/tasks/{task['id']}/run", json=command
        )
        assert queued.status_code == 200, queued.text
        execution_id = queued.json()["data"]["id"]
        assert (
            await client.post(f"/api/automation/tasks/{task['id']}/run", json=command)
        ).json()["data"]["id"] == execution_id
        await automation_worker.wait_until_idle()
        await automation_worker.check_ready()
        memory_notice = await anext(memory_changes)
        assert isinstance(memory_notice, Notification)
        assert memory_notice.key == "project-1"
        result = await client.get(f"/api/automation/runs/{execution_id}")
        assert result.status_code == 200, result.text
        detail = result.json()["data"]
        saved_run = (
            await resources.automation.for_owner("1:project-1").get_run(execution_id)
        ).snapshot
        assert detail["status"] == "succeeded", (
            saved_run.failure_code,
            saved_run.failure_message,
        )
        assert any(message["content"] == "任务完成" for message in detail["messages"])
        history = (await client.get("/api/conversation/history")).json()["data"]
        assert len(history["items"]) == 1
        conversation = history["items"][0]
        assert conversation["threadId"] == saved_run.identity.thread_id
        assert conversation["lastRunId"] == saved_run.identity.run_id
        assert conversation["status"] == "idle"
        full = await client.get(
            f"/api/conversation/{saved_run.identity.thread_id}/history"
        )
        assert full.status_code == 200, full.text
        assert any(
            message["role"] == "user" for message in full.json()["data"]["messages"]
        )
        continued = await client.post(
            "/api/conversation/chat",
            json={
                "threadId": saved_run.identity.thread_id,
                "runId": "continue-automation",
                "parentRunId": saved_run.identity.run_id,
                "state": {},
                "tools": [],
                "context": [],
                "messages": [{"id": "followup", "role": "user", "content": "继续追问"}],
                "forwardedProps": {
                    "projectId": "project-1",
                    "model": "main",
                    "command": {"plan": "off"},
                },
            },
        )
        assert continued.status_code == 200, continued.text
        assert '"type":"RUN_FINISHED"' in continued.text
        full = await client.get(
            f"/api/conversation/{saved_run.identity.thread_id}/history"
        )
        assert full.status_code == 200, full.text
        assert len(full.json()["data"]["messages"]) == 4
        original = (await client.get(f"/api/automation/runs/{execution_id}")).json()[
            "data"
        ]
        assert len(original["messages"]) == 1
        changed = {
            "requestId": "edit",
            "expectedRevision": 1,
            "configuration": {**configuration(), "name": "更名"},
        }
        edited = await client.put(f"/api/automation/tasks/{task['id']}", json=changed)
        assert edited.status_code == 200, edited.text
        conflict = await client.put(
            f"/api/automation/tasks/{task['id']}",
            json={**changed, "requestId": "conflict"},
        )
        assert conflict.status_code == 409
        deleted = await client.post(
            "/api/automation/tasks/batch",
            json={
                "operation": "delete",
                "items": [
                    {
                        "taskId": task["id"],
                        "requestId": "delete",
                        "expectedRevision": 2,
                    }
                ],
            },
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"][0]["succeeded"] is True
        assert (await client.get(f"/api/automation/runs/{execution_id}")).json()[
            "data"
        ]["name"] == "日报"
        user = UserContext(
            user_id=2,
            username="other",
            roles=(),
            disabled=False,
        )
        client.params = {"projectId": "project-2"}
        assert (
            await client.get(f"/api/automation/runs/{execution_id}")
        ).status_code == 404
        assert (await client.get("/api/automation/tasks")).json()["data"]["items"] == []


async def test_task_input_survives_conversation_move_and_can_be_kept_when_editing(
    automation_resources, automation_worker
):
    resources = automation_resources
    async with resources.database.session() as session:
        destination = await ProjectRepository(session, 1).create("目的项目")
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            project_id="project-1",
            thread_id="reference-conversation",
            title="参考文件",
            model_id="main",
        )
        thread.last_run_id = "completed"
        await session.commit()
    file = await resources.attachments.upload(
        user_id=1,
        thread_id=thread.thread_id,
        name="reference.md",
        chunks=byte_chunks(b"# reference"),
    )
    service = StudioAutomationService(resources, project_id="project-1", user_id=1)
    config = TaskConfiguration.model_validate(
        {**configuration(), "attachments": [file.id]}
    )
    task = await service.save(SaveTask(request_id="retain", configuration=config))
    async with resources.database.session() as session:
        await ConversationRepository(session).organize_thread(
            user_id=1,
            thread_id=thread.thread_id,
            project_id=destination.id,
            archived=None,
        )
        await session.commit()

    handle = await resources.automation.for_owner("1:project-1").task(task.id)
    run = await handle.run()
    await automation_worker.wait_until_idle()
    assert (await service.result(run.id)).status == "succeeded"
    assert (await resources.attachments.read(file.id, user_id=1, collection_id=run.id))[
        1
    ] == b"# reference"
    changed = await service.save(
        SaveTask(
            request_id="edit-retained",
            expected_revision=task.revision,
            configuration=config.model_copy(update={"prompt": "核对参考文件"}),
        ),
        task_id=task.id,
    )
    assert [item.id for item in changed.input_files] == [file.id]
    with pytest.raises(BusinessException) as denied:
        await service.save(SaveTask(request_id="unrelated", configuration=config))
    assert denied.value.error_code == AttachmentErrorCode.NOT_FOUND

    assert (
        await resources.attachments.read(
            file.id, user_id=1, thread_id=run.identity.thread_id
        )
    )[1] == b"# reference"
    async with resources.notifications.subscribe(
        scope=NotificationScope("ns_1"), topics=("studio.attachments.changed",)
    ) as notices:
        output = await resources.attachments.upload(
            user_id=1,
            collection_id=run.id,
            name="result.md",
            chunks=byte_chunks(b"# result"),
            source="tool",
        )
        notice = await anext(notices)
        assert isinstance(notice, Notification)
        assert notice.details["thread_id"] == run.identity.thread_id
    async with resources.database.session() as session:
        repository = ConversationRepository(session)
        await repository.organize_thread(
            user_id=1,
            thread_id=run.identity.thread_id,
            project_id=destination.id,
            archived=None,
        )
        await resources.attachments.bind(
            session,
            [file.id, output.id],
            user_id=1,
            thread_id=run.identity.thread_id,
            message_id="followup-with-files",
        )
        await session.commit()
    assert {
        item.id
        for item in await resources.attachments.list_thread(
            user_id=1, thread_id=run.identity.thread_id
        )
    } == {file.id, output.id}
    assert (
        await resources.attachments.get(
            output.id,
            user_id=1,
            project_id=destination.id,
            thread_id=run.identity.thread_id,
        )
    ).id == output.id
    with pytest.raises(BusinessException):
        await resources.attachments.get(
            output.id, user_id=2, thread_id=run.identity.thread_id
        )
    with pytest.raises(BusinessException):
        await resources.attachments.get(
            output.id, user_id=1, thread_id=thread.thread_id
        )
    followup_file = await resources.attachments.upload(
        user_id=1,
        thread_id=run.identity.thread_id,
        name="followup.md",
        chunks=byte_chunks(b"followup"),
    )
    async with resources.database.session() as session:
        await ConversationCommandService(
            ConversationRepository(session), user_id=1, resources=resources
        ).delete(thread_id=run.identity.thread_id)
    with pytest.raises(BusinessException):
        await resources.attachments.get(
            followup_file.id, user_id=1, thread_id=run.identity.thread_id
        )
    with pytest.raises(BusinessException):
        await resources.attachments.list_thread(
            user_id=1, thread_id=run.identity.thread_id
        )
    async with resources.database.session() as session:
        assert (
            await ConversationRepository(session).get_thread(
                user_id=1, thread_id=run.identity.thread_id
            )
            is None
        )
    await resources.attachments.cleanup()
    retained = await service.result(run.id)
    assert retained.messages
    assert [item.id for item in retained.output_files] == [output.id]
    assert (
        await resources.attachments.read(output.id, user_id=1, collection_id=run.id)
    )[1] == b"# result"
    with pytest.raises(BusinessException):
        await resources.attachments.get(
            output.id, user_id=1, thread_id=run.identity.thread_id
        )


async def test_saved_schedule_survives_service_restart(automation_resources):
    resources = automation_resources
    service = StudioAutomationService(resources, project_id="project-1", user_id=1)
    task = await service.save(
        SaveTask(
            request_id="durable",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    second_store = SqlAlchemyAutomationStore(resources.components_database.engine)
    try:
        async with Automation(namespace=NAMESPACE, store=second_store) as second:
            restored = (await second.for_owner("1:project-1").task(task.id)).snapshot
            assert restored.name == "日报"
            assert restored.next_run_at == task.next_run_at
    finally:
        await second_store.close()


async def test_automation_requiring_write_approval_stops_for_human_review(
    automation_resources, automation_worker, monkeypatch
):
    from collections.abc import Callable, Sequence
    from typing import Any

    from langchain_core.language_models import BaseChatModel
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage
    from langchain_core.tools import BaseTool

    class WriteModel(FakeMessagesListChatModel):
        def bind_tools(
            self,
            tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
            **kwargs: Any,
        ) -> BaseChatModel:
            return self

    monkeypatch.setattr(
        runtime_module,
        "create_chat_model",
        lambda *args, **kwargs: WriteModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "id": "write",
                            "args": {"file_path": "/result.txt", "content": "result"},
                        }
                    ],
                ),
                AIMessage(content="完成"),
            ]
        ),
    )
    resources = automation_resources
    service = StudioAutomationService(resources, project_id="project-1", user_id=1)
    config = {**configuration(), "accessMode": "write_approval"}
    task = await service.save(
        SaveTask(
            request_id="approval",
            configuration=TaskConfiguration.model_validate(config),
        )
    )
    task_handle = await resources.automation.for_owner("1:project-1").task(task.id)
    run = await task_handle.run()
    await automation_worker.wait_until_idle()
    result = await service.result(run.id)
    assert result.status == "failed"
    assert result.error == "任务需要人工处理，自动化不会继续执行"


async def test_batch_preserves_individual_conflicts(automation_resources):
    from tinkerfin_studio.automation.schemas import BatchCommand, BatchItem

    service = StudioAutomationService(
        automation_resources, project_id="project-1", user_id=1
    )
    task = await service.save(
        SaveTask(
            request_id="batch",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    result = await service.batch(
        BatchCommand(
            operation="pause",
            items=[
                BatchItem(task_id=task.id, expected_revision=1, request_id="pause"),
                BatchItem(task_id="missing", expected_revision=1, request_id="missing"),
            ],
        )
    )
    assert [(item.task_id, item.succeeded) for item in result] == [
        (task.id, True),
        ("missing", False),
    ]
    assert not (await service.get_task(task.id)).enabled


def test_configuration_rejects_end_date_without_representable_exclusive_bound():
    """结束日期必须能转换为次日零点的排他边界"""
    value = configuration()
    value["endsOn"] = "9999-12-31"
    with pytest.raises(ValueError, match="结束日期"):
        TaskConfiguration.model_validate(value)


@pytest.mark.parametrize("image_support", ["supported", "unsupported", "unknown"])
async def test_automation_accepts_and_runs_authorized_image_inputs(
    automation_resources, automation_worker, image_support
):
    resources = automation_resources
    async with resources.database.session() as session:
        from sqlalchemy import select

        model = await session.scalar(select(AgentModel).where(AgentModel.user_id == 1))
        assert model is not None
        model.image_support = image_support
        await session.commit()
    file = await resources.attachments.upload(
        project_id="project-1", user_id=1, name="image.png", chunks=byte_chunks(png())
    )
    service = StudioAutomationService(resources, project_id="project-1", user_id=1)
    task = await service.save(
        SaveTask(
            request_id="image-input",
            configuration=TaskConfiguration.model_validate(
                {**configuration(), "attachments": [file.id]}
            ),
        )
    )
    assert [item.id for item in task.input_files] == [file.id]
    handle = await resources.automation.for_owner("1:project-1").task(task.id)
    run = await handle.run()
    await automation_worker.wait_until_idle()
    result = await service.result(run.id)
    assert result.status == "succeeded"
    inputs = await resources.attachments.list_collection(
        user_id=1, collection_id=run.id
    )
    assert [item.id for item in inputs] == [file.id]


pytestmark = pytest.mark.usefixtures("projects")


async def test_cancelled_automation_announces_project_memories(
    automation_resources, automation_worker, monkeypatch
):
    entered = asyncio.Event()

    class WaitingModel(_ToolModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                yield chunk

    monkeypatch.setattr(
        runtime_module,
        "create_chat_model",
        lambda *args, **kwargs: WaitingModel(responses=["完成"]),
    )
    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="cancel-memory-run",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    async with resources.notifications.subscribe(
        scope=NotificationScope("ns_1"), topics=("studio.memories.changed",)
    ) as changes:
        handle = await resources.automation.for_owner("1:project-1").task(task.id)
        execution = await handle.run()
        await entered.wait()
        async with resources.database.session() as session:
            response = await ConversationChatService(
                session,
                user=UserContext(user_id=1, username="owner", roles=(), disabled=False),
                resources=resources,
            ).cancel(
                thread_id=execution.identity.thread_id, run_id=execution.identity.run_id
            )
        assert response.cancelled is False
        await automation_worker.wait_until_idle()
        notice = await anext(changes)
        assert isinstance(notice, Notification)
        assert notice.key == "project-1"
        assert (await execution.refresh()).status == "needs_attention"


async def test_removed_automation_rejects_followup_waiting_for_registration(
    automation_resources, automation_worker, monkeypatch
):
    from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer

    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    task = await service.save(
        SaveTask(
            request_id="delete-race",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    execution = await (
        await resources.automation.for_owner("1:project-1").task(task.id)
    ).run()
    await automation_worker.wait_until_idle()
    async with resources.database.session() as session:
        thread = await ConversationRepository(session).get_thread(
            user_id=1, thread_id=execution.identity.thread_id
        )
        assert thread is not None
        assert (
            thread.title_source,
            thread.title_generation_status,
            thread.title_seq,
        ) == ("generated", "succeeded", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    register = ConversationRunPreparer.register

    async def wait_for_registration(self, **kwargs):
        if kwargs["prepared"].identity.run_id == "late-followup":
            entered.set()
            await release.wait()
        return await register(self, **kwargs)

    monkeypatch.setattr(ConversationRunPreparer, "register", wait_for_registration)
    application = create_application(lifespan=None)
    application.state.resources = resources
    application.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1, username="owner", roles=(), disabled=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post(
                "/api/conversation/chat",
                json={
                    "threadId": execution.identity.thread_id,
                    "runId": "late-followup",
                    "parentRunId": execution.identity.run_id,
                    "state": {},
                    "tools": [],
                    "context": [],
                    "messages": [{"id": "followup", "role": "user", "content": "继续"}],
                    "forwardedProps": {
                        "projectId": "project-1",
                        "model": "main",
                        "command": {"plan": "off"},
                    },
                },
            )
        )
        try:
            await entered.wait()
            async with resources.database.session() as session:
                await ConversationCommandService(
                    ConversationRepository(session), user_id=1, resources=resources
                ).delete(thread_id=execution.identity.thread_id)
        finally:
            release.set()
            response = await request
        assert response.status_code in (404, 409)
        assert "RUN_STARTED" not in response.text
    async with resources.database.session() as session:
        assert (
            await ConversationRepository(session).get_run(
                thread_pk=thread.id, run_id="late-followup"
            )
            is None
        )
    assert (await service.result(execution.id)).messages


async def test_preparing_automation_does_not_expose_conversation(
    automation_resources, automation_worker, monkeypatch
):
    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1, project_id="project-1")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_stream = resources.gateway.stream

    async def paused_stream(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_stream(*args, **kwargs)

    monkeypatch.setattr(resources.gateway, "stream", paused_stream)
    task = await service.save(
        SaveTask(
            request_id="preparing",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    handle = await resources.automation.for_owner("1:project-1").task(task.id)
    run = await handle.run()
    try:
        await entered.wait()
        assert (await service.status(run.id)).conversation.state == "not_created"
    finally:
        release.set()
        await automation_worker.wait_until_idle()
    status = await service.status(run.id)
    assert status.conversation.state == "available"
    assert status.conversation.thread_id == run.identity.thread_id
