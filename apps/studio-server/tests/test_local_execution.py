"""本机工作区由云端会话直接调用，并保持文件审批与正常任务分工"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from ag_ui.core import RunFinishedInterruptOutcome
from deepagents.backends import CompositeBackend, FilesystemBackend
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from test_agent_runtime import _Workspace
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_tool_policy import Model, collect, proposal

from tinkerfin import AgUiResumeRequest, SkillSource
from tinkerfin_contracts import ToolDescriptor, ToolResult, WorkspaceTool
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.devices.profiles import DeviceProfileBinding, DeviceProfiles
from tinkerfin_studio.devices.service import DeviceService
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService


@pytest.fixture(autouse=True)
def authorized_computer(monkeypatch):
    async def capture(self, *, identity, project_id, access_mode):
        return DeviceProfileBinding("authorization", "device", project_id, "Project")

    async def list_devices(self, user_id):
        return [SimpleNamespace(device_id="device", online=True)]

    monkeypatch.setattr(DeviceProfiles, "capture", capture)
    monkeypatch.setattr(DeviceService, "list_devices", list_devices)


@pytest.mark.parametrize("access_mode", ["full", "write_approval"])
@pytest.mark.parametrize("delegate", [False, True], ids=["direct", "cloud-subagent"])
async def test_cloud_runtime_uses_named_computer_tools_and_local_review(
    automation_resources, monkeypatch, tmp_path, access_mode, delegate
):
    resources = automation_resources
    workspace = _Workspace(
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )
    selected = []

    def endpoint(*, namespace, target):
        def bind(*, authorization):
            selected.append((namespace, target, authorization))
            return workspace

        return SimpleNamespace(workspace=bind)

    monkeypatch.setattr(
        resources, "remote_tools", SimpleNamespace(endpoint=endpoint), raising=False
    )
    responses: list[BaseMessage] = [
        proposal(10, name="computer_tools"),
        proposal(
            1,
            name="computer_write_file",
            args={"file_path": "/result.txt", "content": "local result"},
        ),
        proposal(2, name="computer_read_file", args={"file_path": "/result.txt"}),
        AIMessage(content="完成"),
    ]
    if delegate:
        responses = [
            proposal(
                0,
                name="task",
                args={
                    "subagent_type": "general-purpose",
                    "description": "写入并核验结果",
                },
            ),
            *responses,
            AIMessage(content="任务完成"),
        ]
    model = Model(responses=responses)
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda *a, **k: model)
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=1,
        thread_id="local-files",
        project_id="project-1",
        model_config=model_config,
        search_service=None,
        image_service=None,
        access_mode=access_mode,
    )
    events, error = await collect(runtime, thread_id="local-files")
    assert error is None
    assert selected == [("ns_1", "device", "authorization")]
    if access_mode == "write_approval":
        assert not (tmp_path / "result.txt").exists()
        outcome = events[-1].outcome
        assert isinstance(outcome, RunFinishedInterruptOutcome)
        resume = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": item.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    for item in outcome.interrupts
                ]
            }
        )
        resumed, error = await collect(
            runtime, thread_id="local-files", run_id="approved", resume=resume
        )
        assert error is None
        events.extend(resumed)
    assert (tmp_path / "result.txt").read_text() == "local result"
    starts = [event for event in events if event.type == "TOOL_CALL_START"]
    names = [event.tool_call_name for event in starts]
    assert "computer_write_file" in names and "computer_read_file" in names
    assert "delegate_task" not in names
    assert ("task" in names) is delegate
    assert workspace.opened == workspace.closed
    assert len(model.requests) == (6 if delegate else 4)


async def test_authorized_file_delivery_does_not_interrupt_write_approval_mode(
    automation_resources, monkeypatch, tmp_path
):
    calls = []

    async def deliver(arguments):
        calls.append(arguments)
        return ToolResult(value={"delivered": True})

    class DeliveryWorkspace(_Workspace):
        @asynccontextmanager
        async def prepare(self, identity):
            async with super().prepare(identity) as prepared:
                yield replace(
                    prepared,
                    tools=(
                        WorkspaceTool(
                            ToolDescriptor(
                                name="deliver_files",
                                description="Deliver selected files",
                                input_schema={"type": "object", "properties": {}},
                            ),
                            deliver,
                        ),
                    ),
                )

    workspace = DeliveryWorkspace(
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )
    resources = automation_resources
    monkeypatch.setattr(
        resources,
        "remote_tools",
        SimpleNamespace(
            endpoint=lambda **kwargs: SimpleNamespace(
                workspace=lambda **kwargs: workspace
            )
        ),
        raising=False,
    )
    model = Model(
        responses=[
            proposal(10, name="computer_tools"),
            proposal(1, name="computer_deliver_files"),
            AIMessage(content="已交付"),
        ]
    )
    monkeypatch.setattr(
        runtime_module, "create_chat_model", lambda *args, **kwargs: model
    )
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=1,
        thread_id="delivery",
        project_id="project-1",
        model_config=model_config,
        search_service=None,
        image_service=None,
        access_mode="write_approval",
    )
    events, error = await collect(runtime, thread_id="delivery")
    assert error is None and calls == [{}]
    assert not isinstance(events[-1].outcome, RunFinishedInterruptOutcome)
    assert workspace.opened == workspace.closed


async def test_computer_discovery_exposes_authorized_skill_sources_to_subagent(
    automation_resources, monkeypatch, tmp_path
):
    skills = tmp_path / "prepared-skills"
    for name in ("enabled", "disabled"):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} browser guidance\n---\n"
            f"Use {name} instructions.\n"
        )

    class SkillWorkspace(_Workspace):
        @asynccontextmanager
        async def prepare(self, identity):
            async with super().prepare(identity) as prepared:
                yield replace(
                    prepared,
                    skill_sources=(SkillSource(str(skills), names=("enabled",)),),
                )

    workspace = SkillWorkspace(
        backend=FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)
    )
    resources = automation_resources
    monkeypatch.setattr(
        resources,
        "remote_tools",
        SimpleNamespace(
            endpoint=lambda **_identity: SimpleNamespace(
                workspace=lambda **_authorization: workspace
            )
        ),
        raising=False,
    )
    model = Model(
        responses=[
            proposal(
                0,
                name="task",
                args={
                    "subagent_type": "general-purpose",
                    "description": "使用当前启用技能检查网页",
                },
            ),
            proposal(1, name="computer_tools"),
            AIMessage(content="检查完成"),
            AIMessage(content="完成"),
        ]
    )
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda *a, **k: model)
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")
    runtime = build_conversation_runtime(
        resources=resources,
        user_id=1,
        thread_id="local-skills",
        project_id="project-1",
        model_config=model_config,
        search_service=None,
        image_service=None,
    )
    _events, error = await collect(runtime, thread_id="local-skills")
    assert error is None
    assert len(model.requests) == 4
    discovery = next(
        item
        for messages in model.requests
        for item in messages
        if isinstance(item, ToolMessage) and item.name == "computer_tools"
    )
    assert str(skills) in str(discovery.content)
    assert '"names": ["enabled"]' in str(discovery.content)
    assert "disabled" not in str(discovery.content)
    assert workspace.opened == workspace.closed


@pytest.mark.parametrize("delegate", [False, True], ids=["main", "subagent"])
async def test_cloud_memory_remains_shared_when_using_computer_tools(
    automation_resources, monkeypatch, tmp_path, delegate
):
    resources = automation_resources
    local = _Workspace(
        backend=FilesystemBackend(
            root_dir=str(tmp_path / "computer"), virtual_mode=True
        )
    )
    monkeypatch.setattr(
        resources,
        "remote_tools",
        SimpleNamespace(
            endpoint=lambda **_: SimpleNamespace(workspace=lambda **_: local)
        ),
        raising=False,
    )

    def cloud_workspace(key, *, workspace_key, routes):
        return _Workspace(
            backend=CompositeBackend(
                default=FilesystemBackend(
                    root_dir=str(tmp_path / key / workspace_key), virtual_mode=True
                ),
                routes=routes,
            )
        )

    monkeypatch.setattr(resources.sandbox_manager, "workspace", cloud_workspace)
    actions: list[BaseMessage] = [
        proposal(
            1,
            name="write_file",
            args={"file_path": "/memories/project.md", "content": "项目长期约定"},
        ),
        proposal(2, name="computer_tools"),
        proposal(
            3,
            name="computer_write_file",
            args={"file_path": "/work.txt", "content": "本机文件"},
        ),
        AIMessage(content="已保存"),
    ]
    if delegate:
        actions: list[BaseMessage] = [
            proposal(
                0,
                name="task",
                args={
                    "subagent_type": "general-purpose",
                    "description": "保存项目约定和本机文件",
                },
            ),
            *actions,
            AIMessage(content="完成"),
        ]
    model = Model(responses=actions)
    monkeypatch.setattr(runtime_module, "create_chat_model", lambda *a, **k: model)
    async with resources.database.session() as session:
        model_config = await AgentModelService(
            AgentModelRepository(session, user_id=1)
        ).resolve("main")

    def build(thread_id: str):
        return build_conversation_runtime(
            resources=resources,
            user_id=1,
            thread_id=thread_id,
            project_id="project-1",
            model_config=model_config,
            search_service=None,
            image_service=None,
            access_mode="full",
        )

    runtime = build("shared-memory")
    _events, error = await collect(runtime, thread_id="shared-memory")
    assert error is None
    files = resources.tinkerfin.with_namespace("ns_1").files(
        ("projects", "project-1", "memories")
    )
    assert (await files.read("/project.md")).content.decode() == "项目长期约定"
    assert (tmp_path / "computer/work.txt").read_text() == "本机文件"
    assert not (tmp_path / "computer/memories/project.md").exists()
    for namespace, project in (("ns_2", "project-1"), ("ns_1", "project-2")):
        other = resources.tinkerfin.with_namespace(namespace).files(
            ("projects", project, "memories")
        )
        with pytest.raises(FileNotFoundError):
            await other.read("/project.md")
    model.responses = [
        proposal(4, name="read_file", args={"file_path": "/memories/project.md"}),
        AIMessage(content="已读取"),
    ]
    model.i = 0
    before = len(local.opened)
    _events, error = await collect(
        build("cloud-memory"),
        thread_id="cloud-memory",
    )
    assert error is None
    assert any(
        isinstance(item, ToolMessage) and "项目长期约定" in str(item.content)
        for item in model.requests[-1]
    )
    assert len(local.opened) == before and local.opened == local.closed
