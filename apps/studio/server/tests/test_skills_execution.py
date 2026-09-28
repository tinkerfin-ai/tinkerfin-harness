"""技能内容准备、手动选择和执行资源归还"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, FileUploadResponse
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_skills_library import skill_files

from tinkerfin import TinkerFin
from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.execution import (
    SelectedSkillInstructions,
    SkillsWorkspace,
    skill_source_path,
)
from tinkerfin_studio.skills.packages import parse_package
from tinkerfin_studio.skills.schemas import SkillReference, SkillSnapshotPayload
from tinkerfin_tracing import Tracer


class RecordingModel(FakeMessagesListChatModel):
    prompts: list[str] = Field(default_factory=list)

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
        self.prompts.append("\n".join(str(message.content) for message in messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


class OwnedTestWorkspace:
    """独立临时目录模拟工作区文件能力，显式信号控制上传取消"""

    def __init__(self, path: Path) -> None:
        self.backend = FilesystemBackend(root_dir=path, virtual_mode=True)
        self.opened = 0
        self.closed = 0
        self.entered = asyncio.Event()
        self.blocked = False
        self.fail = False
        self.workspace = create_autospec(RootedOpenSandboxBackend, instance=True)
        self.workspace.aupload_files.side_effect = self.upload
        self.workspace.to_shell_path.side_effect = lambda path: path.lstrip("/")

    async def upload(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        self.entered.set()
        if self.blocked:
            await asyncio.Event().wait()
        if self.fail:
            return [
                FileUploadResponse(path=path, error="permission_denied")
                for path, _ in files
            ]
        return await self.backend.aupload_files(files)

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol]]:
        self.opened += 1
        try:
            yield PreparedWorkspace(workspace=self.workspace, backend=self.backend)
        finally:
            self.closed += 1


async def content_and_snapshot() -> tuple[SkillContentStore, SkillSnapshotPayload]:
    content = SkillContentStore(TinkerFin(store=InMemoryStore()))
    package = parse_package(skill_files())
    await content.save(1, package)
    return content, SkillSnapshotPayload(
        directory_id="12345678-0000-0000-0000-000000000000",
        skills=(
            SkillReference(
                installation_id="installation",
                name=package.name,
                digest=package.digest,
                selected=True,
            ),
        ),
    )


async def test_selected_skill_instructions_and_binary_resources_reach_the_run(
    tmp_path: Path,
) -> None:
    content, snapshot = await content_and_snapshot()
    workspace = OwnedTestWorkspace(tmp_path)
    model = RecordingModel(responses=[AIMessage(content="done")])
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("ns_1")
        .with_observer(tracer)
        .build(
            model=model,
            backend=SkillsWorkspace(
                workspace, content=content, user_id=1, snapshot=snapshot
            ),
            skills=[skill_source_path(snapshot, snapshot.skills[0])],
            middleware=[
                SelectedSkillInstructions(content, 1, snapshot, snapshot.skills)
            ],
        )
    )
    await runtime.ainvoke(
        thread_id="thread",
        run_id="run",
        input={"messages": [HumanMessage(content="生成报告")]},
    )
    assert workspace.opened == workspace.closed == 1
    assert "用户本轮明确选择技能 reports" in model.prompts[0]
    assert "读取 references/data.bin 并执行 scripts/run.py" in model.prompts[0]
    history = await tracer.get(runtime.thread_identity("thread"))
    assert any(node.name == "应用技能：reports" for node in history.graph.nodes)
    path = (
        skill_source_path(snapshot, snapshot.skills[0]) + "reports/references/data.bin"
    )
    assert (await workspace.backend.adownload_files([path]))[
        0
    ].content == b"\x00\xff\x80\n"
    # 新沙箱仍从固定 Store 内容准备，不依赖之前沙箱的存活
    rebuilt = OwnedTestWorkspace(tmp_path / "rebuilt")
    async with SkillsWorkspace(
        rebuilt, content=content, user_id=1, snapshot=snapshot
    ).prepare(RunIdentity(namespace="ns_1", thread_id="thread", run_id="resume")):
        assert (await rebuilt.backend.adownload_files([path]))[
            0
        ].content == b"\x00\xff\x80\n"
    assert rebuilt.opened == rebuilt.closed == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_skill_preparation_releases_workspace_without_model_call(
    tmp_path: Path, cancel: bool
) -> None:
    content, snapshot = await content_and_snapshot()
    workspace = OwnedTestWorkspace(tmp_path)
    workspace.blocked = cancel
    workspace.fail = not cancel
    model = RecordingModel(responses=[AIMessage(content="done")])
    runtime = (
        TinkerFin()
        .with_namespace("ns_1")
        .build(
            model=model,
            backend=SkillsWorkspace(
                workspace, content=content, user_id=1, snapshot=snapshot
            ),
        )
    )
    task = asyncio.create_task(
        runtime.ainvoke(
            thread_id="thread",
            run_id="run",
            input={"messages": [HumanMessage(content="Work")]},
        )
    )
    await workspace.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else BusinessException):
        await task
    assert workspace.opened == workspace.closed == 1
    assert model.prompts == []
