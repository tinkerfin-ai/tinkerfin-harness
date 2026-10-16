"""项目所有权校验与名称事务"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import BusinessException, ProjectErrorCode
from tinkerfin_studio.projects.models import Project


class ProjectRepository:
    """所有查询同时限定用户和项目，不接受客户端提供的资源路径"""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._session = session
        self._user_id = user_id

    async def require(self, project_id: str, *, lock: bool = False) -> Project:
        statement = select(Project).where(
            Project.id == project_id, Project.user_id == self._user_id
        )
        if lock:
            statement = statement.with_for_update()
        item = await self._session.scalar(statement)
        if item is None or item.id != project_id:
            raise BusinessException(ProjectErrorCode.NOT_FOUND)
        return item

    async def list(self) -> list[Project]:
        return list(
            await self._session.scalars(
                select(Project)
                .where(Project.user_id == self._user_id)
                .order_by(Project.created_at, Project.id)
            )
        )

    async def create(self, name: str) -> Project:
        now = datetime.now(UTC).replace(tzinfo=None)
        item = Project(
            id=str(uuid4()),
            user_id=self._user_id,
            name=name,
            created_at=now,
            updated_at=now,
        )
        self._session.add(item)
        await self._save()
        return item

    async def rename(self, project_id: str, name: str) -> Project:
        item = await self.require(project_id, lock=True)
        item.name = name
        item.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self._save()
        return item

    async def _save(self) -> None:
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise BusinessException(ProjectErrorCode.NAME_CONFLICT) from error
