"""贡献者可在隔离 SQLite 和本地模型上运行的自动化接口闭环"""

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
from tinkerfin_studio.conversation.repository import ConversationRepository
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
):
    async with database.session() as session:
        session.add(
            User(
                id=1,
                username="owner",
                display_name="Owner",
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

    store = SqlAlchemyAutomationStore(components_database.engine)
    automation = Automation(namespace=NAMESPACE, store=store)
    try:
        tracer = Tracer()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ) as client:
            resources = cast(
                ApplicationResources,
                SimpleNamespace(
                    database=database,
                    components_database=components_database,
                    attachments=attachments,
                    skills=skill_library,
                    automation=automation,
                    tracer=tracer,
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
            async with automation.worker(
                on_interrupt=fail_interactive_execution
            ) as worker:
                yield resources, worker
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
    user = UserContext(
        user_id=1, username="owner", display_name="Owner", roles=(), disabled=False
    )
    application.dependency_overrides[get_user_context] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        params={"projectId": "project-1"},
    ) as client:
        payload = {"requestId": "create", "configuration": configuration()}
        created = await client.post("/api/automation/tasks", json=payload)
        assert created.status_code == 200, created.text
        task = created.json()["data"]
        repeat = await client.post("/api/automation/tasks", json=payload)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["data"]["id"] == task["id"]
        assert (await client.get("/api/automation/tasks/counts")).json()["data"] == {
            "enabled": 1,
            "paused": 0,
        }
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
        deleted = await client.request(
            "DELETE",
            f"/api/automation/tasks/{task['id']}",
            json={"requestId": "delete", "expectedRevision": 2},
        )
        assert deleted.status_code == 200, deleted.text
        assert (await client.get(f"/api/automation/runs/{execution_id}")).json()[
            "data"
        ]["name"] == "日报"
        user = UserContext(
            user_id=2,
            username="other",
            display_name="Other",
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
