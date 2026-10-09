"""当前技能文件准备与用户选择消息"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from test_skills_library import add_users

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    RootedOpenSandboxBackend,
    SandboxWorkspace,
    WorkspaceDirectoryContents,
)
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.skills.execution import (
    SkillsWorkspace,
    build_selected_skill_message,
)
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import (
    InstallSkillRequest,
    SkillEnabledRequest,
    SkillSnapshotPayload,
)

pytestmark = pytest.mark.usefixtures("projects")


class RecordingModel(FakeMessagesListChatModel):
    prompts: list[str] = Field(default_factory=list)
    inputs: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> "RecordingModel":
        return self

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.inputs.append(messages)
        self.prompts.append("\n".join(str(message.content) for message in messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


async def test_workspace_preparation_reads_current_installation_after_historical_selection(
    session: AsyncSession,
    skill_library: SkillLibrary,
) -> None:
    await add_users(session)
    installed = (
        await skill_library.install(
            1,
            InstallSkillRequest(
                request_id="install",
                source_id="catalog",
                skill_id="author/reports",
                revision="first",
            ),
        )
    ).installation
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    snapshot = await SkillRepository(session, 1).capture(
        identity,
        project_id="project-1",
        selected_ids=(installed.id,),
    )
    await session.commit()
    backend = create_autospec(RootedOpenSandboxBackend, instance=True)
    workspace = create_autospec(SandboxWorkspace, instance=True)
    workspace.with_directories.return_value = workspace

    @asynccontextmanager
    async def prepare(identity: RunIdentity) -> AsyncIterator[PreparedWorkspace]:
        yield PreparedWorkspace(workspace=backend, backend=backend)

    workspace.prepare.side_effect = prepare
    execution = SkillsWorkspace(
        workspace, library=skill_library, user_id=1, project_id="project-1"
    )
    async with execution.prepare(identity) as prepared:
        declaration = workspace.with_directories.call_args.args[0][0]
        assert isinstance(declaration, WorkspaceDirectoryContents)
        assert declaration.path == "/skills"
        assert dict(declaration.files)["reports/SKILL.md"].endswith(b"Original")
        assert (
            prepared.filesystem_instructions
            and "/skills/<name>/" in prepared.filesystem_instructions
        )
    await skill_library.set_enabled(
        1,
        installed.id,
        SkillEnabledRequest(
            request_id="disable",
            project_id="project-1",
            enabled=False,
        ),
    )
    async with execution.prepare(
        RunIdentity(namespace="ns_1", thread_id="thread", run_id="resume")
    ):
        assert workspace.with_directories.call_args.args[0][0].files == ()
    assert (await SkillRepository(session, 1).snapshot(identity)) == snapshot
    with pytest.raises(BusinessException):
        await skill_library.workspace_files(2, project_id="project-1")


def test_selection_message_points_to_current_file_and_preserves_provenance() -> None:
    snapshot = SkillSnapshotPayload.model_validate(
        {
            "skills": [
                {
                    "installation_id": "one",
                    "name": "reports",
                    "digest": "a" * 64,
                    "selected": True,
                },
                {
                    "installation_id": "two",
                    "name": "analysis",
                    "digest": "b" * 64,
                    "selected": False,
                },
            ]
        }
    )
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    message = build_selected_skill_message(identity=identity, snapshot=snapshot)
    assert message == build_selected_skill_message(identity=identity, snapshot=snapshot)
    assert message is not None
    assert (
        message["content"]
        == "用户本次请求明确选择技能 reports。请先读取 /skills/reports/SKILL.md，按当前文件执行。"
    )
    assert message["source"] == {
        "kind": "context",
        "name": "skill-invocation",
        "metadata": {"skills": [{"id": "one", "name": "reports", "digest": "a" * 64}]},
    }
    assert (
        build_selected_skill_message(
            identity=identity, snapshot=SkillSnapshotPayload(skills=())
        )
        is None
    )
