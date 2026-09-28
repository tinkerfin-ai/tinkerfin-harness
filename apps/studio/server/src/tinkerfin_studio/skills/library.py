"""页面与对话共用的技能查询、内容准备及幂等管理事务"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter

from tinkerfin_notifications import Notifications
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.entity import SkillImportDraft, SkillInstallation
from tinkerfin_studio.skills.packages import SkillArchiveReader, SkillPackage
from tinkerfin_studio.skills.repository import SkillOrigin, SkillRepository
from tinkerfin_studio.skills.schemas import (
    ConfirmImportRequest,
    ImportCandidate,
    ImportPreview,
    InstalledSkill,
    InstallSkillRequest,
    RemoteSkillDetail,
    RemoteSkillPage,
    SkillChangeResult,
    SkillCommandRequest,
    SkillDetail,
    SkillEnabledRequest,
    SkillImportResult,
    SkillRemovalResult,
    SkillReplacement,
    SkillSourceInfo,
    SkillSourceKind,
    UpdateSkillRequest,
)
from tinkerfin_studio.skills.sources import SkillSources
from tinkerfin_studio.skills.updates import (
    CatalogSkillUpdate,
    GitHubSkillUpdate,
    SkillUpdateStrategy,
    SkillUpdateTarget,
    ZipSkillUpdate,
)

_SOURCE_KIND = TypeAdapter(SkillSourceKind)
_Result = TypeVar("_Result", bound=BaseModel)


def installed_skill(item: SkillInstallation) -> InstalledSkill:
    """投影安装信息，内容摘要和内部存储不发给页面"""
    return InstalledSkill(
        id=item.id,
        name=item.name,
        description=item.description,
        source_kind=_SOURCE_KIND.validate_python(item.source_kind),
        source_id=item.source_id,
        source_name=item.source_name,
        external_id=item.external_id,
        enabled=item.enabled,
        author=item.author,
        topics=item.topics,
        file_count=item.file_count,
        byte_size=item.byte_size,
        created_at=item.created_at.replace(tzinfo=UTC),
        updated_at=item.updated_at.replace(tzinfo=UTC),
    )


def _fingerprint(
    action: str, request: SkillCommandRequest, target: str | None = None
) -> str:
    payload = {
        "action": action,
        "target": target,
        "request": request.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class SkillLibrary:
    """借用应用资源，拥有每次管理操作的短事务；不保留用户或请求会话

    文件按内容摘要先完整保存，再由业务事务发布安装引用与操作回执。
    失败不会发布不完整安装；未引用内容继续沿用现有保留策略。
    运行快照捕获仍由运行注册事务负责，不在本服务中独立提交。
    """

    def __init__(
        self,
        database: Database,
        content: SkillContentStore,
        *,
        sources: SkillSources,
        github: GitHubSkillImporter,
        archives: SkillArchiveReader,
        attachments: AttachmentService,
        notifications: Notifications,
    ) -> None:
        self.content = content
        self._database = database
        self._sources = sources
        self._github = github
        self._archives = archives
        self._attachments = attachments
        self._notifications = notifications
        self._updates: dict[SkillSourceKind, SkillUpdateStrategy] = {
            "catalog": CatalogSkillUpdate(sources),
            "github": GitHubSkillUpdate(github),
            "zip": ZipSkillUpdate(),
        }

    def sources(self) -> list[SkillSourceInfo]:
        return self._sources.list()

    async def search(
        self, source_id: str, *, query: str = "", cursor: str | None = None
    ) -> RemoteSkillPage:
        return await self._sources.get(source_id).browse(
            query=query.strip(), cursor=cursor
        )

    async def remote_detail(
        self, source_id: str, skill_id: str, revision: str | None = None
    ) -> RemoteSkillDetail:
        source = self._sources.get(source_id)
        card = await source.lookup(skill_id)
        resolved = revision or card.revision
        if resolved is None:
            raise BusinessException(
                SkillErrorCode.NOT_FOUND, message="该技能尚未发布可安装内容"
            )
        return RemoteSkillDetail(
            skill=card.model_copy(update={"revision": resolved}),
            detail=await source.detail(card.id, resolved),
        )

    async def list(self, user_id: int, *, query: str = "") -> list[InstalledSkill]:
        async with self._database.session() as session:
            items = [
                installed_skill(item)
                for item in await SkillRepository(session, user_id).list()
            ]
        normalized = query.strip().casefold()
        return [
            item
            for item in items
            if not normalized
            or normalized
            in f"{item.name} {item.description} {item.author or ''}".casefold()
        ]

    async def detail(self, user_id: int, installation_id: str) -> SkillDetail:
        async with self._database.session() as session:
            item = await SkillRepository(session, user_id).get(installation_id)
        package = await self.content.load(user_id, item.digest)
        return SkillDetail(
            name=item.name,
            description=item.description,
            markdown=package.markdown,
            files=[file.path for file in package.files],
            author=item.author,
            source_url=item.source_url,
            topics=item.topics,
        )

    async def _receipt(
        self,
        user_id: int,
        request_id: str,
        fingerprint: str,
        result_type: type[_Result],
    ) -> _Result | None:
        async with self._database.session() as session:
            value = await SkillRepository(session, user_id).receipt(
                request_id, fingerprint
            )
        return None if value is None else result_type.model_validate(value)

    async def _commit(
        self,
        user_id: int,
        request_id: str,
        fingerprint: str,
        result_type: type[_Result],
        change: Callable[[SkillRepository], Awaitable[_Result]],
    ) -> _Result:
        async with self._database.session() as session, session.begin():
            repository = SkillRepository(session, user_id)
            await repository.lock_owner()
            previous = await repository.receipt(request_id, fingerprint)
            if previous is not None:
                return result_type.model_validate(previous)
            result = await change(repository)
            repository.save_receipt(
                request_id, fingerprint, result.model_dump(mode="json")
            )
        await notify_change(
            self._notifications,
            user_id=user_id,
            topic="studio.skills.changed",
            key=request_id,
        )
        return result

    async def install(
        self, user_id: int, request: InstallSkillRequest
    ) -> SkillChangeResult:
        """安装指定发行；重放已提交请求时不再次下载或重新安装"""
        fingerprint = _fingerprint("install", request)
        previous = await self._receipt(
            user_id, request.request_id, fingerprint, SkillChangeResult
        )
        if previous is not None:
            return previous
        source = self._sources.get(request.source_id)
        card = await source.lookup(request.skill_id)
        package = await source.package(card.id, request.revision)
        origin = SkillOrigin(
            name=source.info.name,
            kind="catalog",
            source_id=source.info.id,
            external_id=card.id,
            revision=request.revision,
            url=source.page_url(card.id, request.revision),
            author=card.author,
            topics=tuple(card.topics),
        )
        await self.content.save(user_id, package)

        async def change(repository: SkillRepository) -> SkillChangeResult:
            existing = await repository.find_name(package.name)
            item = await repository.install(package, origin)
            return SkillChangeResult(
                installation=installed_skill(item), changed=existing is None
            )

        return await self._commit(
            user_id, request.request_id, fingerprint, SkillChangeResult, change
        )

    async def set_enabled(
        self, user_id: int, installation_id: str, request: SkillEnabledRequest
    ) -> SkillChangeResult:
        fingerprint = _fingerprint("enabled", request, installation_id)

        async def change(repository: SkillRepository) -> SkillChangeResult:
            item = await repository.get(installation_id)
            changed = item.enabled != request.enabled
            if changed:
                item = await repository.set_enabled(installation_id, request.enabled)
            return SkillChangeResult(
                installation=installed_skill(item), changed=changed
            )

        return await self._commit(
            user_id, request.request_id, fingerprint, SkillChangeResult, change
        )

    async def uninstall(
        self, user_id: int, installation_id: str, request: SkillCommandRequest
    ) -> SkillRemovalResult:
        fingerprint = _fingerprint("uninstall", request, installation_id)

        async def change(repository: SkillRepository) -> SkillRemovalResult:
            await repository.uninstall(installation_id)
            return SkillRemovalResult(installation_id=installation_id)

        return await self._commit(
            user_id, request.request_id, fingerprint, SkillRemovalResult, change
        )

    async def update(
        self, user_id: int, installation_id: str, request: UpdateSkillRequest
    ) -> SkillChangeResult:
        """策略准备内容后原子切换安装引用，旧快照仍指向原摘要"""
        fingerprint = _fingerprint("update", request, installation_id)
        previous = await self._receipt(
            user_id, request.request_id, fingerprint, SkillChangeResult
        )
        if previous is not None:
            return previous
        async with self._database.session() as session:
            item = await SkillRepository(session, user_id).get(installation_id)
            kind = _SOURCE_KIND.validate_python(item.source_kind)
            target = SkillUpdateTarget(
                id=item.id,
                name=item.name,
                digest=item.digest,
                origin=SkillOrigin(
                    name=item.source_name,
                    kind=kind,
                    source_id=item.source_id,
                    external_id=item.external_id,
                    revision=item.source_revision,
                    url=item.source_url,
                    author=item.author,
                    topics=tuple(item.topics),
                ),
            )
        if request.replacement is not None and kind != "zip":
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        replacement = (
            await self._replacement(user_id, request.replacement)
            if request.replacement
            else None
        )
        prepared = await self._updates[kind].prepare(target, replacement)
        await self.content.save(user_id, prepared.package)

        async def change(repository: SkillRepository) -> SkillChangeResult:
            item, changed = await repository.replace(
                target.id, target.digest, prepared.package, prepared.origin
            )
            return SkillChangeResult(
                installation=installed_skill(item), changed=changed
            )

        return await self._commit(
            user_id, request.request_id, fingerprint, SkillChangeResult, change
        )

    async def _replacement(
        self, user_id: int, selection: SkillReplacement
    ) -> SkillPackage:
        async with self._database.session() as session:
            draft = await SkillRepository(session, user_id).draft(selection.draft_id)
            if draft.source != "zip" or selection.digest not in {
                ImportCandidate.model_validate(value).digest
                for value in draft.candidates
            }:
                raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        return await self.content.load(user_id, selection.digest)

    async def preview_github(self, user_id: int, url: str) -> ImportPreview:
        return await self._preview(
            user_id, await self._github.read(url), source="github", source_url=url
        )

    async def preview_zip(self, user_id: int, content: bytes) -> ImportPreview:
        return await self._preview(
            user_id, await self._archives.read(content), source="zip"
        )

    async def preview_attachment(
        self, user_id: int, thread_id: str, attachment_id: str
    ) -> ImportPreview:
        """通过附件服务校验用户与会话归属，只读取已发布的 ZIP 原件"""
        attachment, data = await self._attachments.read(
            attachment_id, user_id=user_id, thread_id=thread_id
        )
        if attachment.mime_type != "application/zip":
            raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
        return await self.preview_zip(user_id, data)

    async def _preview(
        self,
        user_id: int,
        packages: tuple[SkillPackage, ...],
        *,
        source: Literal["github", "zip"],
        source_url: str | None = None,
    ) -> ImportPreview:
        for package in packages:
            await self.content.save(user_id, package)
        candidates = [
            ImportCandidate(
                digest=package.digest,
                name=package.name,
                description=package.description,
                file_count=len(package.files),
                byte_size=package.byte_size,
            )
            for package in packages
        ]
        preview = ImportPreview(id=str(uuid4()), source=source, candidates=candidates)
        async with self._database.session() as session, session.begin():
            await SkillRepository(session, user_id).lock_owner()
            session.add(
                SkillImportDraft(
                    id=preview.id,
                    user_id=user_id,
                    source=source,
                    source_url=source_url,
                    candidates=[
                        candidate.model_dump(mode="json") for candidate in candidates
                    ],
                    selected_digests=None,
                    installation_ids=None,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        return preview

    async def confirm(
        self, user_id: int, draft_id: str, request: ConfirmImportRequest
    ) -> SkillImportResult:
        """安装预览固定的内容；批次整体提交，确认重放不恢复已卸载条目"""
        request = request.model_copy(update={"digests": sorted(request.digests)})
        fingerprint = _fingerprint("import", request, draft_id)
        previous = await self._receipt(
            user_id, request.request_id, fingerprint, SkillImportResult
        )
        if previous is not None:
            return previous
        async with self._database.session() as session:
            draft = await SkillRepository(session, user_id).draft(draft_id)
            allowed = {
                ImportCandidate.model_validate(candidate).digest
                for candidate in draft.candidates
            }
            if len(set(request.digests)) != len(request.digests) or not set(
                request.digests
            ).issubset(allowed):
                raise BusinessException(SkillErrorCode.INVALID_PACKAGE)
            origin = SkillOrigin(
                name="GitHub" if draft.source == "github" else "ZIP",
                kind=_SOURCE_KIND.validate_python(draft.source),
                url=draft.source_url,
            )
        packages = [
            await self.content.load(user_id, digest) for digest in request.digests
        ]

        async def change(repository: SkillRepository) -> SkillImportResult:
            draft = await repository.draft(draft_id)
            if draft.selected_digests is not None:
                if draft.selected_digests != request.digests:
                    raise BusinessException(SkillErrorCode.IMPORT_CONFLICT)
                assert draft.installation_ids is not None
                return SkillImportResult(installation_ids=draft.installation_ids)
            ids = [
                (await repository.install(package, origin)).id for package in packages
            ]
            draft.selected_digests = request.digests
            draft.installation_ids = ids
            return SkillImportResult(installation_ids=ids)

        return await self._commit(
            user_id, request.request_id, fingerprint, SkillImportResult, change
        )
