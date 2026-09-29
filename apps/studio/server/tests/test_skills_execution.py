"""技能内容准备、手动选择和执行资源归还"""

import asyncio
import json
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
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.execution import (
    MAX_SELECTED_INSTRUCTIONS_BYTES,
    SkillsWorkspace,
    build_selected_skill_message,
    skill_source_path,
)
from tinkerfin_studio.skills.packages import SkillFile, parse_package
from tinkerfin_studio.skills.schemas import SkillReference, SkillSnapshotPayload
from tinkerfin_tracing import Tracer


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
        )
    )
    context = await build_selected_skill_message(
        content,
        user_id=1,
        identity=RunIdentity(namespace="ns_1", thread_id="thread", run_id="run"),
        snapshot=snapshot,
    )
    assert context is not None
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="run",
        messages=[
            {"id": "question", "role": "user", "content": "用 /reports 技能帮我"},
            context,
        ],
    )
    _ = [event async for event in stream]
    assert stream.error is None
    assert workspace.opened == workspace.closed == 1
    assert "用户本次请求明确选择技能 reports" in model.prompts[0]
    human = [
        message for message in model.inputs[0] if isinstance(message, HumanMessage)
    ]
    assert [message.content for message in human] == [
        "用 /reports 技能帮我",
        context["content"],
    ]
    assert all(
        "用户本次请求明确选择技能" not in str(message.content)
        for message in model.inputs[0]
        if message.type == "system"
    )
    assert "读取 references/data.bin 并执行 scripts/run.py" in model.prompts[0]
    history = await tracer.get(runtime.thread_identity("thread"))
    selected = [message for message in history.messages if message.source]
    assert len(selected) == 1 and selected[0].content == context["content"]
    assert (
        selected[0].source is not None and selected[0].source.name == "skill-invocation"
    )
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


async def test_skill_message_is_stable_combines_only_selected_skills_and_checks_owner() -> (
    None
):
    content, snapshot = await content_and_snapshot()
    other = parse_package(skill_files("analysis"))
    await content.save(1, other)
    snapshot = snapshot.model_copy(
        update={
            "skills": (
                *snapshot.skills,
                SkillReference(
                    installation_id="other",
                    name=other.name,
                    digest=other.digest,
                    selected=True,
                ),
            )
        }
    )
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    first = await build_selected_skill_message(
        content, user_id=1, identity=identity, snapshot=snapshot
    )
    assert first == await build_selected_skill_message(
        content, user_id=1, identity=identity, snapshot=snapshot
    )
    assert first is not None
    text = first["content"]
    assert isinstance(text, str) and text.index("技能 reports") < text.index(
        "技能 analysis"
    )
    assert first["source"] == {
        "kind": "context",
        "name": "skill-invocation",
        "metadata": {
            "skills": [
                {
                    "id": skill.installation_id,
                    "name": skill.name,
                    "digest": skill.digest,
                }
                for skill in snapshot.skills
            ]
        },
    }
    unselected = snapshot.model_copy(
        update={
            "skills": tuple(
                skill.model_copy(update={"selected": False})
                for skill in snapshot.skills
            )
        }
    )
    assert (
        await build_selected_skill_message(
            content, user_id=1, identity=identity, snapshot=unselected
        )
        is None
    )
    with pytest.raises(BusinessException) as failure:
        await build_selected_skill_message(
            content, user_id=2, identity=identity, snapshot=snapshot
        )
    assert failure.value.error_code == SkillErrorCode.CONTENT_UNAVAILABLE


@pytest.mark.parametrize("extra", [0, 1])
async def test_skill_message_capacity_preserves_the_complete_body(extra: int) -> None:
    content, snapshot = await content_and_snapshot()
    identity = RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
    baseline = await build_selected_skill_message(
        content, user_id=1, identity=identity, snapshot=snapshot
    )
    assert baseline is not None
    size = len(
        json.dumps(
            baseline["content"], ensure_ascii=False, separators=(",", ":")
        ).encode()
    )
    original = skill_files()
    padding = b"x" * (MAX_SELECTED_INSTRUCTIONS_BYTES - size + extra)
    package = parse_package(
        (SkillFile("SKILL.md", original[0].content + padding), *original[1:])
    )
    await content.save(1, package)
    snapshot = snapshot.model_copy(
        update={
            "skills": (
                snapshot.skills[0].model_copy(update={"digest": package.digest}),
            )
        }
    )
    if extra:
        with pytest.raises(BusinessException) as failure:
            await build_selected_skill_message(
                content, user_id=1, identity=identity, snapshot=snapshot
            )
        assert failure.value.error_code == SkillErrorCode.INSTRUCTIONS_TOO_LARGE
    else:
        message = await build_selected_skill_message(
            content, user_id=1, identity=identity, snapshot=snapshot
        )
        assert message is not None and str(message["content"]).endswith(
            package.markdown
        )
        assert (
            len(
                json.dumps(
                    message["content"], ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            == MAX_SELECTED_INSTRUCTIONS_BYTES
        )
