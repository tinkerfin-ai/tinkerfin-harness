"""在首次本机需求时固定运行的设备、项目与确认选择"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.devices.models import Device, DeviceExecutionProfile
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.skills.repository import SkillRepository

_skills: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(
    list[dict[str, JsonValue]]
)


@dataclass(frozen=True, slots=True)
class DeviceProfileBinding:
    """会话已固定的本机执行身份，供框架工具绑定"""

    profile_id: str
    device_id: str
    workspace_id: str
    workspace_label: str


class DeviceProfiles:
    """借用会话登记事务，原子保存本机授权与主运行关联"""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._session = session
        self._user_id = user_id

    async def require(self, profile_id: str) -> DeviceExecutionProfile:
        """只读取本人已固定的配置"""
        row = await self._session.scalar(
            select(DeviceExecutionProfile).where(
                DeviceExecutionProfile.profile_id == profile_id,
                DeviceExecutionProfile.user_id == self._user_id,
            )
        )
        if row is None:
            raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
        return row

    @staticmethod
    def binding(row: DeviceExecutionProfile) -> DeviceProfileBinding:
        return DeviceProfileBinding(
            row.profile_id, row.device_id, row.workspace_id, row.workspace_label
        )

    async def capture(
        self,
        *,
        identity: RunIdentity,
        project_id: str,
        access_mode: AccessMode,
    ) -> DeviceProfileBinding:
        """首次本机调用原子绑定当前设备，恢复和重复调用保留原授权

        只为已登记且仍在运行的会话创建授权，调用方在提交后才打开本机工作区。
        绑定与运行记录共用事务，失败或取消回滚后不会留下孤立授权。
        """
        if identity.namespace != f"ns_{self._user_id}":
            raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
        run = await self._session.scalar(
            select(ConversationRunRegistration)
            .join(
                ConversationThread,
                ConversationThread.id
                == ConversationRunRegistration.conversation_thread_id,
            )
            .where(
                ConversationThread.user_id == self._user_id,
                ConversationThread.thread_id == identity.thread_id,
                ConversationThread.project_id == project_id,
                ConversationThread.deleted_at.is_(None),
                ConversationThread.archived.is_(False),
                ConversationThread.status != "deleting",
                ConversationRunRegistration.run_id == identity.run_id,
                ConversationRunRegistration.status.in_(
                    ("preparing", "starting", "running")
                ),
            )
            .with_for_update()
        )
        if run is None or run.access_mode != access_mode:
            raise BusinessException(DeviceErrorCode.EXECUTION_UNAVAILABLE)
        if run.local_profile_id is not None:
            row = await self.require(run.local_profile_id)
            if (
                row.source_thread_id != identity.thread_id
                or row.workspace_id != project_id
                or row.access_mode != access_mode
            ):
                raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
            return self.binding(row)
        device = await self._session.scalar(
            select(Device)
            .where(Device.user_id == self._user_id, Device.revoked_at.is_(None))
            .with_for_update()
        )
        if device is None:
            raise BusinessException(DeviceErrorCode.NOT_FOUND)
        project = await ProjectRepository(self._session, self._user_id).require(
            project_id
        )
        skills = await SkillRepository(self._session, self._user_id).snapshot(
            identity, project_id=project_id
        )
        profile_id = str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    (
                        "tinkerfin-local",
                        identity.namespace,
                        identity.thread_id,
                        identity.run_id,
                    )
                ),
            )
        )
        row = DeviceExecutionProfile(
            profile_id=profile_id,
            user_id=self._user_id,
            device_id=device.device_id,
            source_thread_id=identity.thread_id,
            source_run_id=identity.run_id,
            workspace_id=project.id,
            workspace_label=project.name,
            access_mode=access_mode,
            skills=_skills.validate_python(
                [item.model_dump(mode="json") for item in skills.skills]
            ),
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self._session.add(row)
        run.local_profile_id = profile_id
        await self._session.flush()
        return self.binding(row)
