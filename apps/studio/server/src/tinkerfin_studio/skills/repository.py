"""技能安装和运行快照的事务操作"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.skills.entity import (
    ProjectSkillSetting,
    SkillImportDraft,
    SkillInstallation,
    SkillOperationReceipt,
    SkillRunSnapshot,
)
from tinkerfin_studio.skills.packages import SkillPackage
from tinkerfin_studio.skills.schemas import (
    SkillReference,
    SkillSnapshotPayload,
    SkillSourceKind,
)


@dataclass(frozen=True, slots=True)
class SkillOrigin:
    """安装时固定的来源信息，不包含凭据"""

    name: str
    kind: SkillSourceKind
    source_id: str | None = None
    external_id: str | None = None
    revision: str | None = None
    url: str | None = None
    author: str | None = None
    topics: tuple[str, ...] = ()


def snapshot_key(user_id: int, identity: RunIdentity) -> str:
    """以完整运行身份生成业务快照键，不参与框架命名空间编码"""
    return str(
        uuid5(
            NAMESPACE_URL,
            json.dumps(
                [
                    "tinkerfin-skills",
                    user_id,
                    identity.namespace,
                    identity.thread_id,
                    identity.run_id,
                ]
            ),
        )
    )


class SkillRepository:
    """由调用方提交事务，用户行锁串行化安装变更与新运行捕获"""

    def __init__(
        self, session: AsyncSession, user_id: int, *, project_id: str | None = None
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.project_id = project_id

    async def require_project(self) -> None:
        if self.project_id is not None:
            await ProjectRepository(self.session, self.user_id).require(self.project_id)

    async def overrides(self, *, project_id: str | None = None) -> dict[str, bool]:
        project_id = project_id if project_id is not None else self.project_id
        if project_id is None:
            return {}
        return {
            item.installation_id: item.enabled
            for item in await self.session.scalars(
                select(ProjectSkillSetting)
                .where(
                    ProjectSkillSetting.user_id == self.user_id,
                    ProjectSkillSetting.project_id == project_id,
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        }

    async def lock_owner(self) -> None:
        await self.require_project()
        if (
            await self.session.scalar(
                select(User.id).where(User.id == self.user_id).with_for_update()
            )
            is None
        ):
            raise BusinessException(SkillErrorCode.NOT_FOUND)

    async def list(self) -> list[SkillInstallation]:
        await self.require_project()
        return list(
            await self.session.scalars(
                select(SkillInstallation)
                .where(
                    SkillInstallation.user_id == self.user_id,
                    SkillInstallation.project_id.in_(("", self.project_id or "")),
                )
                .order_by(SkillInstallation.updated_at.desc(), SkillInstallation.id)
            )
        )

    async def get(self, installation_id: str) -> SkillInstallation:
        await self.require_project()
        item = await self.session.scalar(
            select(SkillInstallation)
            .where(
                SkillInstallation.id == installation_id,
                SkillInstallation.user_id == self.user_id,
                SkillInstallation.project_id.in_(("", self.project_id or "")),
            )
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return item

    async def install(
        self, package: SkillPackage, origin: SkillOrigin
    ) -> SkillInstallation:
        """调用方先保存完整内容；同名同内容幂等，同名不同内容明确冲突"""
        await self.lock_owner()
        current = await self.session.scalar(
            select(SkillInstallation)
            .where(
                SkillInstallation.user_id == self.user_id,
                SkillInstallation.name == package.name,
                SkillInstallation.project_id == (self.project_id or ""),
            )
            .execution_options(populate_existing=True)
        )
        if current is not None:
            if current.digest != package.digest:
                raise BusinessException(SkillErrorCode.NAME_CONFLICT)
            return current
        now = datetime.now(UTC).replace(tzinfo=None)
        item = SkillInstallation(
            id=str(uuid4()),
            user_id=self.user_id,
            project_id=self.project_id or "",
            name=package.name,
            description=package.description,
            digest=package.digest,
            source_kind=origin.kind,
            source_id=origin.source_id,
            source_name=origin.name,
            external_id=origin.external_id,
            source_revision=origin.revision,
            source_url=origin.url,
            author=origin.author,
            topics=list(origin.topics),
            enabled=True,
            file_count=len(package.files),
            byte_size=package.byte_size,
            created_at=now,
            updated_at=now,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def find_name(self, name: str) -> SkillInstallation | None:
        return await self.session.scalar(
            select(SkillInstallation)
            .where(
                SkillInstallation.user_id == self.user_id,
                SkillInstallation.name == name,
                SkillInstallation.project_id == (self.project_id or ""),
            )
            .execution_options(populate_existing=True)
        )

    async def replace(
        self,
        installation_id: str,
        expected_digest: str,
        package: SkillPackage,
        origin: SkillOrigin,
    ) -> tuple[SkillInstallation, bool]:
        """调用方持有用户行锁；替换内容时保留最新启用状态和安装身份"""
        item = await self.get(installation_id)
        if item.project_id != (self.project_id or ""):
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        if item.digest != expected_digest:
            raise BusinessException(SkillErrorCode.UPDATE_CONFLICT)
        collision = await self.find_name(package.name)
        if collision is not None and collision.id != item.id:
            raise BusinessException(SkillErrorCode.NAME_CONFLICT)
        changed = item.digest != package.digest
        item.source_revision = origin.revision
        item.source_url = origin.url
        if changed:
            item.name = package.name
            item.description = package.description
            item.digest = package.digest
            item.file_count = len(package.files)
            item.byte_size = package.byte_size
            item.author = origin.author
            item.topics = list(origin.topics)
            item.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.flush()
        return item, changed

    async def receipt(
        self, request_id: str, fingerprint: str
    ) -> dict[str, JsonValue] | None:
        receipt = await self.session.get(
            SkillOperationReceipt, (self.user_id, request_id), populate_existing=True
        )
        if receipt is None:
            return None
        if receipt.fingerprint != fingerprint:
            raise BusinessException(SkillErrorCode.OPERATION_CONFLICT)
        return receipt.result

    def save_receipt(
        self, request_id: str, fingerprint: str, result: dict[str, JsonValue]
    ) -> None:
        """由同一个业务事务同时提交安装关系与操作结果"""
        self.session.add(
            SkillOperationReceipt(
                user_id=self.user_id,
                request_id=request_id,
                fingerprint=fingerprint,
                result=result,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    async def set_enabled(
        self, installation_id: str, enabled: bool
    ) -> SkillInstallation:
        await self.lock_owner()
        item = await self.get(installation_id)
        if self.project_id is not None and not item.project_id:
            setting = await self.session.get(
                ProjectSkillSetting, (self.project_id, item.id)
            )
            if setting is None:
                self.session.add(
                    ProjectSkillSetting(
                        project_id=self.project_id,
                        installation_id=item.id,
                        user_id=self.user_id,
                        enabled=enabled,
                    )
                )
            else:
                setting.enabled = enabled
        else:
            item.enabled = enabled
            item.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.flush()
        return item

    async def uninstall(self, installation_id: str) -> None:
        """只移除安装关系，已被运行和导入引用的内容继续保留"""
        await self.lock_owner()
        item = await self.get(installation_id)
        if item.project_id != (self.project_id or ""):
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        await self.session.execute(
            delete(ProjectSkillSetting).where(
                ProjectSkillSetting.installation_id == item.id,
                ProjectSkillSetting.user_id == self.user_id,
            )
        )
        await self.session.delete(item)
        await self.session.flush()

    async def draft(self, draft_id: str) -> SkillImportDraft:
        item = await self.session.scalar(
            select(SkillImportDraft)
            .where(
                SkillImportDraft.id == draft_id,
                SkillImportDraft.user_id == self.user_id,
            )
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return item

    async def snapshot(
        self, identity: RunIdentity, *, project_id: str | None = None
    ) -> SkillSnapshotPayload:
        """读取运行固定内容，恢复执行时要求项目归属保持一致

        Args:
            identity: 已授权运行的身份
            project_id: 继续执行时的项目；历史读取可不限定移动前的项目

        Returns:
            已提交的技能集合

        Raises:
            BusinessException: 快照不存在或不属于要继续执行的项目
        """
        item = await self.session.scalar(
            select(SkillRunSnapshot)
            .where(
                SkillRunSnapshot.id == snapshot_key(self.user_id, identity),
                SkillRunSnapshot.user_id == self.user_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise BusinessException(SkillErrorCode.CONTENT_UNAVAILABLE)
        if project_id is not None and item.project_id != project_id:
            raise BusinessException(SkillErrorCode.NOT_FOUND)
        return SkillSnapshotPayload.model_validate(item.payload)

    async def capture(
        self,
        identity: RunIdentity,
        *,
        project_id: str,
        selected_ids: tuple[str, ...] = (),
        source: RunIdentity | None = None,
    ) -> SkillSnapshotPayload:
        """原子固定启用集合；幂等重试与恢复不读取最新安装状态"""
        await ProjectRepository(self.session, self.user_id).require(project_id)
        await self.lock_owner()
        key = snapshot_key(self.user_id, identity)
        existing = await self.session.get(SkillRunSnapshot, key, populate_existing=True)
        if existing is not None:
            if existing.user_id != self.user_id or existing.project_id != project_id:
                raise BusinessException(SkillErrorCode.NOT_FOUND)
            payload = SkillSnapshotPayload.model_validate(existing.payload)
            if source is None and set(selected_ids) != {
                skill.installation_id for skill in payload.skills if skill.selected
            }:
                raise BusinessException(SkillErrorCode.SNAPSHOT_CONFLICT)
            return payload
        if source is not None:
            payload = await self.snapshot(source, project_id=project_id)
        else:
            # 用户锁确定捕获次序，当前读保证看到锁前已提交的更新和启停
            installations = list(
                await self.session.scalars(
                    select(SkillInstallation)
                    .where(
                        SkillInstallation.user_id == self.user_id,
                        SkillInstallation.project_id.in_(("", project_id)),
                    )
                    .order_by(SkillInstallation.name)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            overrides = await self.overrides(project_id=project_id)
            preferred = {}
            for item in installations:
                if item.name not in preferred or item.project_id:
                    preferred[item.name] = item
            enabled = [
                item
                for item in preferred.values()
                if overrides.get(item.id, item.enabled)
            ]
            references = tuple(
                SkillReference(
                    installation_id=item.id,
                    name=item.name,
                    digest=item.digest,
                    selected=item.id in selected_ids,
                )
                for item in enabled
            )
            payload = SkillSnapshotPayload(directory_id=key, skills=references)
        # 先检查本事务已有的待写项，使后面的完整性错误只属于本次快照插入
        await self.session.flush()
        try:
            # 不锁不存在的键；不同用户可以并发首次捕获，唯一约束判定是否已登记
            async with self.session.begin_nested():
                self.session.add(
                    SkillRunSnapshot(
                        id=key,
                        user_id=self.user_id,
                        project_id=project_id,
                        payload=payload.model_dump(mode="json"),
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                await self.session.flush()
                if source is None and not set(selected_ids).issubset(
                    {skill.installation_id for skill in payload.skills}
                ):
                    raise BusinessException(SkillErrorCode.DISABLED)
        except IntegrityError as error:
            if error.orig is None or error.orig.args[:1] != (1062,):
                raise
            # 事务早先的读视图可能看不到已提交快照，重复插入后读取原内容
            try:
                payload = await self.snapshot(identity)
            except BusinessException:
                raise error
            if source is None and set(selected_ids) != {
                skill.installation_id for skill in payload.skills if skill.selected
            }:
                raise BusinessException(SkillErrorCode.SNAPSHOT_CONFLICT) from error
        return payload
