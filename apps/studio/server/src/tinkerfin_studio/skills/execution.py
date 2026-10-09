"""准备项目当前技能文件，并记录用户本轮的明确选择"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from deepagents.backends.protocol import BackendProtocol
from pydantic import JsonValue

from tinkerfin_contracts import MessageSource, PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    RootedOpenSandboxBackend,
    SandboxWorkspace,
    WorkspaceDirectoryContents,
)
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload


class SkillsWorkspace:
    """按项目当前安装准备技能，由框架发布文件并管理执行资源

    安装变化在后续执行准备时生效，文件本地修改遵循工作区权限。
    已存在的相同安装内容不会覆盖本地修改，更新冲突由框架明确报错。
    """

    def __init__(
        self,
        workspace: SandboxWorkspace[str],
        *,
        library: SkillLibrary,
        user_id: int,
        project_id: str,
    ) -> None:
        self._workspace = workspace
        self._library = library
        self._user_id = user_id
        self._project_id = project_id

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol]]:
        """普通执行与审批恢复均读取当前安装，不用历史选择重建技能"""
        files = await self._library.workspace_files(
            self._user_id, project_id=self._project_id
        )
        workspace = self._workspace.with_directories(
            [WorkspaceDirectoryContents("/skills", files)]
        )
        async with workspace.prepare(identity) as prepared:
            instructions = (
                "\n当前技能位于 /skills/<name>/。使用技能前先读取该目录下的 SKILL.md。"
                "执行相对路径脚本前，先进入 skills/<name> 目录。\n"
            )
            yield replace(
                prepared,
                filesystem_instructions=(prepared.filesystem_instructions or "")
                + instructions,
            )


def build_selected_skill_message(
    *, identity: RunIdentity, snapshot: SkillSnapshotPayload
) -> dict[str, JsonValue] | None:
    """记录用户选择并要求读取当前文件，避免历史正文替代当前技能

    Args:
        identity: 当前普通提问的稳定运行身份
        snapshot: 注册事务已完成授权的技能选择记录

    Returns:
        带稳定身份及公开来源的用户角色消息；未选择技能时返回 None
    """
    selected = tuple(skill for skill in snapshot.skills if skill.selected)
    if not selected:
        return None
    instructions = "\n".join(
        f"用户本次请求明确选择技能 {skill.name}。请先读取 /skills/{skill.name}/SKILL.md，按当前文件执行。"
        for skill in selected
    )
    references: list[JsonValue] = [
        {"id": skill.installation_id, "name": skill.name, "digest": skill.digest}
        for skill in selected
    ]
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
