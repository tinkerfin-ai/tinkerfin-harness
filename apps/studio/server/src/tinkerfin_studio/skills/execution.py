"""把已捕获的技能内容准备到沙箱，并应用用户本轮的明确选择"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from tinkerfin import trace_contribution
from tinkerfin_contracts import PreparedWorkspace, RunIdentity, Workspace
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.schemas import SkillReference, SkillSnapshotPayload


def skill_source_path(snapshot: SkillSnapshotPayload, skill: SkillReference) -> str:
    """来源为技能子目录的父级，快照目录在恢复期间保持固定"""
    return f"/skills/{snapshot.directory_id}/{skill.digest}/"


class SkillsWorkspace:
    """在框架拥有的工作区借用期间准备技能，释放职责留在公共上下文内"""

    def __init__(
        self,
        workspace: Workspace[RootedOpenSandboxBackend, BackendProtocol],
        *,
        content: SkillContentStore,
        user_id: int,
        snapshot: SkillSnapshotPayload,
    ) -> None:
        self._workspace = workspace
        self._content = content
        self._user_id = user_id
        self._snapshot = snapshot

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol]]:
        """先完整上传固定文件，失败或取消时结束本轮借用，禁止继续执行"""
        async with self._workspace.prepare(identity) as prepared:
            locations: list[str] = []
            for skill in self._snapshot.skills:
                package = await self._content.load(self._user_id, skill.digest)
                if package.name != skill.name:
                    raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE)
                root = skill_source_path(self._snapshot, skill) + skill.name
                files = [
                    (f"{root}/{file.path}", file.content) for file in package.files
                ]
                uploaded = await prepared.workspace.aupload_files(files)
                if len(uploaded) != len(files) or any(
                    result.error is not None or result.path != path
                    for result, (path, _) in zip(uploaded, files, strict=True)
                ):
                    raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE)
                locations.append(
                    f"{skill.name}: `{prepared.workspace.to_shell_path(root)}`"
                )
            instructions = (
                "\n技能脚本和资源已准备到工作区。执行相对路径脚本前先进入对应技能目录：\n"
                + "\n".join(locations)
            )
            yield replace(
                prepared,
                filesystem_instructions=(prepared.filesystem_instructions or "")
                + instructions,
            )


class SelectedSkillInstructions(AgentMiddleware):
    """将用户明确选择的固定技能指令加入本轮模型上下文，不改写用户消息"""

    def __init__(
        self,
        content: SkillContentStore,
        user_id: int,
        snapshot: SkillSnapshotPayload,
        skills: tuple[SkillReference, ...],
    ) -> None:
        self._content = content
        self._user_id = user_id
        self._snapshot = snapshot
        self._skills = skills

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        blocks: list[str] = []
        # 公共轨迹能力记录本轮实际应用的技能，实时和历史都能定位该上下文步骤
        async with trace_contribution(
            kind="custom",
            name="应用技能：" + "、".join(skill.name for skill in self._skills),
        ):
            for skill in self._skills:
                package = await self._content.load(self._user_id, skill.digest)
                blocks.append(
                    f"用户本轮明确选择技能 {skill.name}。技能目录：{skill_source_path(self._snapshot, skill)}{skill.name}\n{package.markdown}"
                )
        addition = "\n\n".join(blocks)
        current = request.system_message
        current_content = "" if current is None else current.content
        if isinstance(current_content, str):
            content = current_content + "\n\n" + addition
        else:
            content = [*current_content, {"type": "text", "text": addition}]
        message = (
            SystemMessage(content=content)
            if current is None
            else current.model_copy(update={"content": content})
        )
        return await handler(request.override(system_message=message))
