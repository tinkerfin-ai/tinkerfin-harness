"""按服务、账户和项目隔离本机文件，目录身份变化时拒绝恢复执行"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .configuration import CompanionConfiguration, WorkspacePermissions
from .lifecycle import settle


class _DirectoryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: int
    inode: int


class _ProjectIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str
    namespace: str
    directories: dict[str, _DirectoryIdentity] = Field(
        default_factory=dict, max_length=8192
    )


class _WorkspacePermissions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str
    workspace_scope_id: str
    permissions: WorkspacePermissions
    revoked: bool


class ManagedProjects:
    """执行准备的目录所有者；锁内的短磁盘操作由受控线程执行

    目录名只使用服务地址摘要、账号命名空间和项目 UUID。状态索引保存在
    工具进程不可读写的私有目录，删除或替换既有目录后不会静默恢复到空工作区。
    """

    def __init__(self, configuration: CompanionConfiguration, state: Path) -> None:
        self._configuration = configuration
        self._state = state
        self._lock = asyncio.Lock()

    async def prepare(self, project_id: str) -> Path:
        """创建新项目目录或核验既有目录，拒绝路径注入及符号链接"""
        if str(UUID(project_id)) != project_id:
            raise ValueError("项目身份必须是规范 UUID")
        async with self._lock:
            return await settle(asyncio.to_thread(self._prepare, project_id))

    async def permissions_for(
        self, project_id: str, workspace_scope_id: str
    ) -> WorkspacePermissions:
        """固定工作区权限；恢复保留原范围，配置撤销后拒绝继续使用原权限"""
        if not workspace_scope_id or len(workspace_scope_id) > 256:
            raise ValueError("工作区执行身份无效")
        async with self._lock:
            return await settle(
                asyncio.to_thread(self._permissions_for, project_id, workspace_scope_id)
            )

    async def browser_profile(self, project_id: str, thread_id: str) -> Path:
        """准备当前账号、项目和会话独占的浏览器资料，既有资料原位保留"""
        if str(UUID(project_id)) != project_id:
            raise ValueError("项目身份必须是规范 UUID")
        async with self._lock:
            return await settle(
                asyncio.to_thread(self._browser_profile, project_id, thread_id)
            )

    def _browser_profile(self, project_id: str, thread_id: str) -> Path:
        if not thread_id or len(thread_id) > 1024:
            raise ValueError("浏览器会话身份无效")
        name = hashlib.sha256(thread_id.encode()).hexdigest()
        directory = self._prepare(project_id)
        # 使用目录句柄逐级创建，不能跟随工具替换的链接或修改链接目标权限
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in (".tinkerfin", "browser-profiles", name):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise ValueError("浏览器资料目录不能被文件或链接替换") from error
                try:
                    info = os.fstat(child)
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                        raise ValueError("浏览器资料目录归属无效")
                    os.fchmod(child, 0o700)
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)
        return directory / ".tinkerfin" / "browser-profiles" / name

    def _permissions_for(
        self, project_id: str, workspace_scope_id: str
    ) -> WorkspacePermissions:
        root = self._state / "workspace-permissions"
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("执行权限目录归属无效或被链接替换")
        target = root / (
            hashlib.sha256(workspace_scope_id.encode()).hexdigest() + ".json"
        )
        current = self._configuration.permissions
        if target.exists() or target.is_symlink():
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("执行权限文件归属无效")
                content = stream.read(128 * 1024 + 1)
            if len(content) > 128 * 1024:
                raise ValueError("执行权限超过容量")
            saved = _WorkspacePermissions.model_validate_json(content)
            if (
                saved.project_id != project_id
                or saved.workspace_scope_id != workspace_scope_id
            ):
                raise ValueError("执行项目发生变化")
            original = saved.permissions
            if saved.revoked:
                raise ValueError("执行工具权限已撤销，请发起新的执行")
            if (
                original.browser
                and not current.browser
                or original.desktop
                and not current.desktop
                or (
                    current.network == "restricted"
                    and (
                        original.network == "normal"
                        or not set(original.allowed_domains).issubset(
                            current.allowed_domains
                        )
                    )
                )
                or any(
                    server not in current.mcp_servers for server in original.mcp_servers
                )
            ):
                raise ValueError("执行工具权限已撤销")
            return original
        descriptor, temporary = tempfile.mkstemp(prefix=".permissions-", dir=root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(
                    _WorkspacePermissions(
                        project_id=project_id,
                        workspace_scope_id=workspace_scope_id,
                        permissions=current,
                        revoked=False,
                    ).model_dump_json()
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return current

    def _prepare(self, project_id: str) -> Path:
        root = self._configuration.workspace_root
        if root.is_relative_to(self._state) or self._state.is_relative_to(root):
            raise ValueError("项目目录与应用私有状态必须分离")
        for parent in reversed((root, *root.parents)):
            if parent.is_symlink():
                raise ValueError("项目目录不能经过符号链接")
        address = hashlib.sha256(self._configuration.server.encode()).hexdigest()
        directory = root / address / self._configuration.namespace / project_id
        target = self._state / "projects.json"
        expected = _ProjectIndex(
            server=self._configuration.server, namespace=self._configuration.namespace
        )
        if target.exists():
            with target.open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("项目索引超过容量")
            expected = _ProjectIndex.model_validate_json(content)
            if (expected.server, expected.namespace) != (
                self._configuration.server,
                self._configuration.namespace,
            ):
                raise ValueError("项目状态属于其他服务或账号")
        previous = expected.directories.get(project_id)
        if previous is not None and not directory.exists():
            raise ValueError("项目目录已被移除，无法恢复既有执行")
        for path in (
            root,
            root / address,
            root / address / self._configuration.namespace,
            directory,
        ):
            try:
                path.mkdir(mode=0o700, parents=path == root)
            except FileExistsError:
                pass
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("项目目录归属无效或被链接替换")
            os.chmod(path, 0o700)
        info = directory.stat()
        current = _DirectoryIdentity(device=info.st_dev, inode=info.st_ino)
        if previous is not None and previous != current:
            raise ValueError("项目目录身份已变化，无法恢复既有执行")
        expected.directories[project_id] = current
        if len(expected.directories) > 8192:
            raise ValueError("本机项目索引已满")
        descriptor, temporary = tempfile.mkstemp(prefix=".projects-", dir=self._state)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(expected.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return directory
