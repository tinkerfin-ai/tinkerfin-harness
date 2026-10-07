"""独占 MySQL 验证技能回执与安装内容在用户行锁下共同提交"""

import asyncio
import secrets
from datetime import datetime

import httpx
import pytest
from langgraph.store.memory import InMemoryStore
from skill_fakes import MemorySkillSource
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_mysql_schema_alignment import _create_database, _drop_database
from test_skills_library import add_users, skill_files

from tinkerfin import TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, SkillErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.infrastructure.database import Base, Database
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.entity import (
    SkillInstallation,
    SkillOperationReceipt,
    SkillRunSnapshot,
)
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import (
    SkillArchiveReader,
    SkillFile,
    parse_package,
)
from tinkerfin_studio.skills.repository import SkillOrigin, SkillRepository
from tinkerfin_studio.skills.schemas import (
    InstallSkillRequest,
    SkillCommandRequest,
    SkillEnabledRequest,
    SkillSnapshotPayload,
    UpdateSkillRequest,
)
from tinkerfin_studio.skills.sources import SkillSources


@pytest.mark.studio_mysql_integration
async def test_concurrent_skill_commands_and_snapshot_capture(
    mysql_admin_url: str, attachments, notifications
):
    class PairedSource(MemorySkillSource):
        def __init__(self):
            super().__init__({"first": parse_package(skill_files())})
            self.arrivals = 0
            self.both = asyncio.Event()
            self.proceed = asyncio.Event()
            self.pair = True

        async def package(self, skill_id: str, revision: str):
            if self.pair:
                self.arrivals += 1
                if self.arrivals == 2:
                    self.both.set()
                await self.proceed.wait()
            return await super().package(skill_id, revision)

    admin_url = make_url(mysql_admin_url)
    name = f"tinkerfin_schema_{secrets.token_hex(8)}_runtime"
    admin = create_async_engine(admin_url)
    created = False
    try:
        await _create_database(admin, name)
        created = True
        async with Database(
            admin_url.set(database=name).render_as_string(hide_password=False),
            pool_size=6,
            max_overflow=0,
        ) as database:
            async with database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with database.session() as session:
                await add_users(session)
                session.add_all(
                    [
                        Project(
                            id=f"project-{user_id}",
                            user_id=user_id,
                            name="项目",
                            created_at=datetime(2030, 1, 1),
                            updated_at=datetime(2030, 1, 1),
                        )
                        for user_id in (1, 2)
                    ]
                )
                await session.commit()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(404))
            ) as client:
                reader = SkillArchiveReader()
                source = PairedSource()
                content = SkillContentStore(TinkerFin(store=InMemoryStore()))
                library = SkillLibrary(
                    database,
                    content,
                    sources=SkillSources([source]),
                    github=GitHubSkillImporter(client, reader),
                    archives=reader,
                    attachments=attachments,
                    notifications=notifications,
                )
                request = InstallSkillRequest(
                    request_id="same-command",
                    source_id="catalog",
                    skill_id="author/reports",
                    revision="first",
                )
                commands = [
                    asyncio.create_task(library.install(1, request)) for _ in range(2)
                ]
                try:
                    await source.both.wait()
                    source.proceed.set()
                    results = await asyncio.gather(*commands)
                finally:
                    for task in commands:
                        task.cancel()
                    await asyncio.gather(*commands, return_exceptions=True)
                assert results[0] == results[1]
                assert len(await library.list(1)) == 1
                async with database.session() as session:
                    assert (
                        await session.scalar(
                            select(func.count()).select_from(SkillOperationReceipt)
                        )
                        == 1
                    )
                item = results[0].installation
                source.pair = False
                source.packages["downloaded"] = parse_package(
                    (*skill_files(), SkillFile("downloaded", b"older"))
                )
                source.revision = "downloaded"
                source.started.clear()
                source.release = asyncio.Event()
                work = asyncio.create_task(
                    library.update(1, item.id, UpdateSkillRequest(request_id="stale"))
                )
                snapshot_task = None
                try:
                    await source.started.wait()
                    replacement = parse_package(
                        (*skill_files(), SkillFile("committed", b"authoritative"))
                    )
                    await content.save(1, replacement)
                    async with database.session() as session, session.begin():
                        repository = SkillRepository(session, 1)
                        await repository.lock_owner()
                        await repository.replace(
                            item.id,
                            source.packages["first"].digest,
                            replacement,
                            SkillOrigin(
                                "Catalog",
                                kind="catalog",
                                source_id="catalog",
                                external_id="author/reports",
                            ),
                        )
                        assert (await library.detail(1, item.id)).files == [
                            file.path for file in source.packages["first"].files
                        ]
                        entered = asyncio.Event()

                        async def capture():
                            async with (
                                database.session() as snapshot_session,
                                snapshot_session.begin(),
                            ):
                                # 会话登记在用户锁前已读取业务数据，捕获仍须看到锁前提交的更新
                                assert (
                                    await snapshot_session.scalar(
                                        select(SkillInstallation.digest).where(
                                            SkillInstallation.id == item.id
                                        )
                                    )
                                    == source.packages["first"].digest
                                )
                                entered.set()
                                return await SkillRepository(
                                    snapshot_session, 1
                                ).capture(
                                    RunIdentity(
                                        namespace="ns_1",
                                        thread_id="thread",
                                        run_id="captured",
                                    ),
                                    project_id="project-1",
                                )

                        snapshot_task = asyncio.create_task(capture())
                        await entered.wait()
                    snapshot = await snapshot_task
                    assert snapshot.skills[0].digest == replacement.digest
                    source.release.set()
                    with pytest.raises(BusinessException) as conflict:
                        await work
                    assert conflict.value.error_code == SkillErrorCode.UPDATE_CONFLICT
                    assert "committed" in (await library.detail(1, item.id)).files
                finally:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    if snapshot_task is not None:
                        snapshot_task.cancel()
                        await asyncio.gather(snapshot_task, return_exceptions=True)

                later_identity = RunIdentity(
                    namespace="ns_1", thread_id="thread", run_id="later"
                )
                async with database.session() as session, session.begin():
                    # 其他请求在本事务首次读取后提交的快照，仍可读取、重放及恢复
                    await session.scalar(select(SkillRunSnapshot.id).limit(1))
                    async with database.session() as other, other.begin():
                        later = await SkillRepository(other, 1).capture(
                            later_identity,
                            project_id="project-1",
                            selected_ids=(item.id,),
                        )
                    repository = SkillRepository(session, 1)
                    assert await repository.snapshot(later_identity) == later
                    await library.set_enabled(
                        1,
                        item.id,
                        SkillEnabledRequest(request_id="disable-replay", enabled=False),
                    )
                    assert (
                        await repository.capture(
                            later_identity,
                            project_id="project-1",
                            selected_ids=(item.id,),
                        )
                        == later
                    )
                    restored = await repository.capture(
                        RunIdentity(
                            namespace="ns_1", thread_id="thread", run_id="restored"
                        ),
                        project_id="project-1",
                        source=later_identity,
                    )
                    assert restored == later
                await library.set_enabled(
                    1,
                    item.id,
                    SkillEnabledRequest(request_id="enable-after-replay", enabled=True),
                )

                rollback_identity = RunIdentity(
                    namespace="ns_1", thread_id="thread", run_id="rolled-back"
                )
                with pytest.raises(RuntimeError, match="abort registration"):
                    async with database.session() as session, session.begin():
                        await SkillRepository(session, 1).capture(
                            rollback_identity, project_id="project-1"
                        )
                        raise RuntimeError("abort registration")
                async with database.session() as session:
                    with pytest.raises(BusinessException) as rolled_back:
                        await SkillRepository(session, 1).snapshot(rollback_identity)
                    assert (
                        rolled_back.value.error_code
                        == SkillErrorCode.CONTENT_UNAVAILABLE
                    )

                pending_identity = RunIdentity(
                    namespace="ns_1", thread_id="thread", run_id="pending-error"
                )
                async with database.session() as session:
                    await session.scalar(select(SkillRunSnapshot.id).limit(1))
                    async with database.session() as other, other.begin():
                        await SkillRepository(other, 1).capture(
                            pending_identity, project_id="project-1"
                        )
                    session.add(
                        User(
                            id=3,
                            username="invalid",
                            display_name="待写用户",
                            password_hash=None,
                            roles=[],
                            disabled=False,
                        )
                    )
                    with pytest.raises(IntegrityError):
                        await SkillRepository(session, 1).capture(
                            pending_identity, project_id="project-1"
                        )
                    await session.rollback()

                async with database.session() as session, session.begin():
                    assert await session.scalar(
                        select(SkillInstallation.enabled).where(
                            SkillInstallation.id == item.id
                        )
                    )
                    await library.set_enabled(
                        1,
                        item.id,
                        SkillEnabledRequest(request_id="disable", enabled=False),
                    )
                    disabled = await SkillRepository(session, 1).capture(
                        RunIdentity(
                            namespace="ns_1", thread_id="thread", run_id="disabled"
                        ),
                        project_id="project-1",
                    )
                    assert disabled.skills == ()
                    unavailable_identity = RunIdentity(
                        namespace="ns_1", thread_id="thread", run_id="unavailable"
                    )
                    with pytest.raises(BusinessException) as unavailable:
                        await SkillRepository(session, 1).capture(
                            unavailable_identity,
                            project_id="project-1",
                            selected_ids=(item.id,),
                        )
                    assert unavailable.value.error_code == SkillErrorCode.DISABLED
                    with pytest.raises(BusinessException) as unpublished:
                        await SkillRepository(session, 1).snapshot(unavailable_identity)
                    assert (
                        unpublished.value.error_code
                        == SkillErrorCode.CONTENT_UNAVAILABLE
                    )

                source.started.clear()
                source.release = asyncio.Event()
                work = asyncio.create_task(
                    library.update(
                        1,
                        item.id,
                        UpdateSkillRequest(request_id="removed-during-download"),
                    )
                )
                try:
                    await source.started.wait()
                    await library.uninstall(
                        1, item.id, SkillCommandRequest(request_id="remove")
                    )
                    source.release.set()
                    with pytest.raises(BusinessException) as missing:
                        await work
                    assert missing.value.error_code == SkillErrorCode.NOT_FOUND
                finally:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                assert await library.list(1) == []
                async with database.session() as session, session.begin():
                    repository = SkillRepository(session, 1)
                    resumed = await repository.capture(
                        RunIdentity(
                            namespace="ns_1", thread_id="thread", run_id="captured"
                        ),
                        project_id="project-1",
                    )
                    fresh = await repository.capture(
                        RunIdentity(
                            namespace="ns_1", thread_id="thread", run_id="fresh"
                        ),
                        project_id="project-1",
                    )
                assert resumed == snapshot
                assert fresh.skills == ()
                assert (
                    await content.load(1, resumed.skills[0].digest)
                ).digest == replacement.digest
    finally:
        if created:
            await _drop_database(admin, name)
        await admin.dispose()


@pytest.mark.studio_mysql_integration
async def test_two_users_capture_first_snapshot_without_gap_deadlock(
    mysql_admin_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(mysql_admin_url)
    name = f"tinkerfin_schema_{secrets.token_hex(8)}_runtime"
    admin = create_async_engine(url)
    created = False
    try:
        await _create_database(admin, name)
        created = True
        async with Database(
            url.set(database=name).render_as_string(hide_password=False),
            pool_size=4,
            max_overflow=0,
        ) as database:
            async with database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with database.session() as session:
                await add_users(session)
                session.add_all(
                    [
                        Project(
                            id=f"project-{user_id}",
                            user_id=user_id,
                            name="项目",
                            created_at=datetime(2030, 1, 1),
                            updated_at=datetime(2030, 1, 1),
                        )
                        for user_id in (1, 2)
                    ]
                )
                await session.commit()

            both = asyncio.Event()
            proceed = asyncio.Event()
            arrivals = 0
            original_get = AsyncSession.get

            async def get_after_read(
                session: AsyncSession,
                entity: type[SkillRunSnapshot],
                ident: str,
                *,
                populate_existing: bool = False,
            ) -> SkillRunSnapshot | None:
                nonlocal arrivals
                value = await original_get(
                    session, entity, ident, populate_existing=populate_existing
                )
                if entity is SkillRunSnapshot and value is None:
                    arrivals += 1
                    if arrivals == 2:
                        both.set()
                    await proceed.wait()
                return value

            monkeypatch.setattr(AsyncSession, "get", get_after_read)

            async def capture(user_id: int) -> SkillSnapshotPayload:
                async with database.session() as session, session.begin():
                    return await SkillRepository(session, user_id).capture(
                        RunIdentity(
                            namespace=f"ns_{user_id}",
                            thread_id="thread",
                            run_id="first",
                        ),
                        project_id=f"project-{user_id}",
                    )

            commands = [asyncio.create_task(capture(user_id)) for user_id in (1, 2)]
            barrier = asyncio.create_task(both.wait())
            try:
                done, _ = await asyncio.wait(
                    [*commands, barrier], return_when=asyncio.FIRST_COMPLETED
                )
                assert barrier in done, [
                    task.exception() for task in commands if task.done()
                ]
                # 两个缺失键的读取都已结束后才放行插入，不依赖固定等待或偶然调度
                proceed.set()
                results = await asyncio.gather(*commands, return_exceptions=True)
                assert all(
                    isinstance(result, SkillSnapshotPayload) for result in results
                ), results
                async with database.session() as session:
                    assert (
                        await session.scalar(
                            select(func.count()).select_from(SkillRunSnapshot)
                        )
                        == 2
                    )
            finally:
                proceed.set()
                for task in [*commands, barrier]:
                    task.cancel()
                await asyncio.gather(*commands, return_exceptions=True)
                await asyncio.gather(barrier, return_exceptions=True)
    finally:
        if created:
            await _drop_database(admin, name)
        await admin.dispose()
