"""本机工作区由云端会话直接调用，并保持文件审批与正常任务分工"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from ag_ui.core import RunFinishedInterruptOutcome
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage, BaseMessage
from test_agent_runtime import _Workspace
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_tool_policy import Model, collect, proposal

from tinkerfin import AgUiResumeRequest, SkillSource
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.devices.profiles import DeviceProfileBinding
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService


@pytest.mark.parametrize("access_mode", ["full", "write_approval"])
@pytest.mark.parametrize("delegate", [False, True], ids=["direct", "cloud-subagent"])
async def test_cloud_runtime_uses_local_workspace_and_file_review(
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
        proposal(
            1,
            name="write_file",
            args={"file_path": "/result.txt", "content": "local result"},
        ),
        proposal(2, name="read_file", args={"file_path": "/result.txt"}),
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
        local_profile=DeviceProfileBinding(
            "authorization", "device", "project-1", "Project"
        ),
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
    assert "write_file" in names and "read_file" in names
    assert "delegate_task" not in names
    assert ("task" in names) is delegate
    assert workspace.opened == workspace.closed
    assert len(model.requests) == (5 if delegate else 3)


async def test_local_enabled_skills_reach_main_and_general_purpose_agents(
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
        local_profile=DeviceProfileBinding(
            "authorization", "device", "project-1", "Project"
        ),
    )
    _events, error = await collect(runtime, thread_id="local-skills")
    assert error is None
    assert len(model.requests) == 3
    for messages in model.requests[:2]:
        system = str(messages[0].content)
        assert "enabled browser guidance" in system
        assert str(skills / "enabled/SKILL.md") in system
        assert "disabled browser guidance" not in system
    assert workspace.opened == workspace.closed
