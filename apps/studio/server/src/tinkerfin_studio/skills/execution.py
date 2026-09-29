"""把已捕获的技能内容准备到沙箱，并应用用户本轮的明确选择"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from deepagents.backends.protocol import BackendProtocol
from pydantic import JsonValue

from tinkerfin_contracts import MessageSource, PreparedWorkspace, RunIdentity, Workspace
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


# 与公开历史单条正文的 512 KiB 预算保持一致，避免已注入的正文在刷新后被省略
MAX_SELECTED_INSTRUCTIONS_BYTES = 512 * 1024


async def build_selected_skill_message(
    content: SkillContentStore,
    *,
    user_id: int,
    identity: RunIdentity,
    snapshot: SkillSnapshotPayload,
) -> dict[str, JsonValue] | None:
    """从运行固定快照生成一条可随会话保留的技能指令

    Args:
        content: 当前应用的技能内容仓储
        user_id: 已认证且拥有该快照的用户
        identity: 当前普通提问的稳定运行身份
        snapshot: 已完成授权并固定的技能选择和内容摘要

    Returns:
        带稳定身份及公开来源的用户角色消息；未选择技能时返回 None

    Raises:
        BusinessException: 固定技能内容缺失、不一致，或完整正文超过历史容量
    """
    selected = tuple(skill for skill in snapshot.skills if skill.selected)
    if not selected:
        return None
    blocks: list[str] = []
    references: list[JsonValue] = []
    for skill in selected:
        package = await content.load(user_id, skill.digest)
        if package.name != skill.name:
            raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE)
        blocks.append(
            f"用户本次请求明确选择技能 {skill.name}。技能目录：{skill_source_path(snapshot, skill)}{skill.name}\n{package.markdown}"
        )
        references.append(
            {"id": skill.installation_id, "name": skill.name, "digest": skill.digest}
        )
    instructions = "\n\n".join(blocks)
    encoded = json.dumps(
        instructions, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_SELECTED_INSTRUCTIONS_BYTES:
        raise BusinessException(SkillErrorCode.INSTRUCTIONS_TOO_LARGE)
    source = MessageSource(
        kind="context", name="skill-invocation", metadata={"skills": references}
    )
    return {
        "id": "message-"
        + str(
            uuid5(
                NAMESPACE_URL,
                f"tinkerfin-studio:skill-instructions:{identity.namespace}:{identity.thread_id}:{identity.run_id}",
            )
        ),
        "role": "user",
        "content": instructions,
        "source": source.model_dump(mode="json"),
    }
