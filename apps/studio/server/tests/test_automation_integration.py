"""贡献者可在隔离 SQLite 和本地模型上运行的自动化接口闭环"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from test_agent_runtime import _ToolModel, _Workspace

from tinkerfin import TinkerFin
from tinkerfin_automation import (
    Automation,
    SqlAlchemyAutomationStore,
    next_run_after,
)
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.api.dependencies import get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.automation.schemas import SaveTask, TaskConfiguration
from tinkerfin_studio.automation.service import NAMESPACE, StudioAutomationService
from tinkerfin_studio.automation.target import (
    StudioAutomationTarget,
    fail_interactive_execution,
)
from tinkerfin_studio.models.entity import AgentModel, ModelConnection
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import Tracer


@pytest.fixture
async def automation_environment(
    database, components_database, attachments, monkeypatch
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
        def workspace(self, key, *, routes):
            assert key == "users/1"
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
                    automation=automation,
                    tracer=tracer,
                    tinkerfin=TinkerFin(
                        checkpointer=InMemorySaver(), store=InMemoryStore()
                    ).with_observer(tracer),
                    model_http_transport=None,
                    model_http_client=client,
                    sandbox_manager=Sandboxes(),
                    agent_subagents={},
                    settings=SimpleNamespace(
                        tavily_api_key=None, model_allowed_origins=()
                    ),
                ),
            )
            automation.target("studio_agent", StudioAutomationTarget(resources))
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
        transport=httpx.ASGITransport(app=application), base_url="http://test"
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
            await resources.automation.for_owner("1").get_run(execution_id)
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
        assert (
            await client.get(f"/api/automation/runs/{execution_id}")
        ).status_code == 404
        assert (await client.get("/api/automation/tasks")).json()["data"]["items"] == []


async def test_saved_schedule_survives_service_restart(automation_resources):
    resources = automation_resources
    service = StudioAutomationService(resources, user_id=1)
    task = await service.save(
        SaveTask(
            request_id="durable",
            configuration=TaskConfiguration.model_validate(configuration()),
        )
    )
    second_store = SqlAlchemyAutomationStore(resources.components_database.engine)
    try:
        async with Automation(namespace=NAMESPACE, store=second_store) as second:
            restored = (await second.for_owner("1").task(task.id)).snapshot
            assert restored.name == "日报"
            assert restored.next_run_at == task.next_run_at
    finally:
        await second_store.close()


async def test_read_only_automation_fails_instead_of_approving_a_write(
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
    service = StudioAutomationService(resources, user_id=1)
    config = {**configuration(), "accessMode": "write_approval"}
    task = await service.save(
        SaveTask(
            request_id="approval",
            configuration=TaskConfiguration.model_validate(config),
        )
    )
    task_handle = await resources.automation.for_owner("1").task(task.id)
    run = await task_handle.run()
    await automation_worker.wait_until_idle()
    result = await service.result(run.id)
    assert result.status == "failed"
    assert result.error == "任务需要人工处理，自动化不会继续执行"


async def test_batch_preserves_individual_conflicts(automation_resources):
    from tinkerfin_studio.automation.schemas import BatchCommand, BatchItem

    service = StudioAutomationService(automation_resources, user_id=1)
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
