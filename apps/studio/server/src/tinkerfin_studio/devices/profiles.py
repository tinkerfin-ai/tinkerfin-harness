"""固定云端会话使用的本机工作区与权限快照"""

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
from tinkerfin_studio.devices.schemas import LocalTargetSelection
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload

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
        selection: LocalTargetSelection | None,
        access_mode: AccessMode,
        skills: SkillSnapshotPayload,
        inherited: str | None = None,
    ) -> DeviceProfileBinding | None:
        """固定会话项目与执行位置；新轮次使用当前设备，审批恢复保留原授权"""
        if identity.namespace != f"ns_{self._user_id}":
            raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
        if inherited is not None:
            row = await self.require(inherited)
            if (
                row.source_thread_id != identity.thread_id
                or row.access_mode != access_mode
            ):
                raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
            if selection is not None and (
                row.device_id != selection.device_id
                or row.workspace_id != selection.workspace_id
            ):
                raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
            return self.binding(row)
        prior = await self._session.scalar(
            select(ConversationRunRegistration)
            .join(
                ConversationThread,
                ConversationThread.id
                == ConversationRunRegistration.conversation_thread_id,
            )
            .where(
                ConversationThread.user_id == self._user_id,
                ConversationThread.thread_id == identity.thread_id,
                ConversationRunRegistration.run_id != identity.run_id,
                ConversationRunRegistration.status != "rejected",
            )
            .order_by(ConversationRunRegistration.id)
            .limit(1)
        )
        if prior is not None:
            if prior.local_profile_id is None:
                if selection is not None:
                    raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
            else:
                original = await self.require(prior.local_profile_id)
                if (
                    selection is not None
                    and original.workspace_id != selection.workspace_id
                ):
                    raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
                if selection is None:
                    device_id = await self._session.scalar(
                        select(Device.device_id).where(
                            Device.user_id == self._user_id,
                            Device.revoked_at.is_(None),
                        )
                    )
                    if device_id is None:
                        raise BusinessException(DeviceErrorCode.NOT_FOUND)
                    selection = LocalTargetSelection(
                        device_id=device_id, workspace_id=original.workspace_id
                    )
        if selection is None:
            return None
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
        existing = await self._session.get(DeviceExecutionProfile, profile_id)
        if existing is not None:
            if (
                existing.user_id != self._user_id
                or existing.device_id != selection.device_id
                or existing.workspace_id != selection.workspace_id
                or existing.access_mode != access_mode
            ):
                raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)
            return self.binding(existing)
        device = await self._session.scalar(
            select(Device)
            .where(
                Device.device_id == selection.device_id,
                Device.user_id == self._user_id,
                Device.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if device is None:
            raise BusinessException(DeviceErrorCode.NOT_FOUND)
        thread = await self._session.scalar(
            select(ConversationThread).where(
                ConversationThread.user_id == self._user_id,
                ConversationThread.thread_id == identity.thread_id,
            )
        )
        if thread is None or selection.workspace_id != thread.project_id:
            raise BusinessException(DeviceErrorCode.WORKSPACE_UNAVAILABLE)
        project = await ProjectRepository(self._session, self._user_id).require(
            thread.project_id
        )
        row = DeviceExecutionProfile(
            profile_id=profile_id,
            user_id=self._user_id,
            device_id=selection.device_id,
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
        await self._session.flush()
        return self.binding(row)
