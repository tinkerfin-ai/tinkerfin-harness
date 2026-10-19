"""按账号读取会话已固定的工具执行位置"""

from __future__ import annotations

from sqlalchemy import select

from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
)
from tinkerfin_studio.conversation.models import ConversationRunRegistration
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices.models import Device
from tinkerfin_studio.devices.profiles import DeviceProfiles
from tinkerfin_studio.devices.schemas import (
    LocalTargetSelection,
)
from tinkerfin_studio.infrastructure.database import Database


class DeviceSelections:
    """只读取本人会话的执行位置"""

    def __init__(
        self,
        database: Database,
        *,
        user_id: int,
    ) -> None:
        self._database = database
        self._user_id = user_id

    async def selection(
        self, thread_id: str, run_id: str | None
    ) -> LocalTargetSelection | None:
        """重试复用原运行目标；下一轮本机对话选择账号当前有效设备"""
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            thread = await repository.get_accessible_thread(
                user_id=self._user_id, thread_id=thread_id
            )
            if thread is None or thread.status == "deleting":
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            run = (
                await repository.get_run(thread_pk=thread.id, run_id=run_id)
                if run_id
                else await session.scalar(
                    select(ConversationRunRegistration)
                    .where(
                        ConversationRunRegistration.conversation_thread_id == thread.id,
                        ConversationRunRegistration.status != "rejected",
                    )
                    .order_by(ConversationRunRegistration.id)
                    .limit(1)
                )
            )
            if run is None:
                raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
            if run.local_profile_id is None:
                return None
            profile = await DeviceProfiles(session, self._user_id).require(
                run.local_profile_id
            )
            current_device = (
                await session.scalar(
                    select(Device.device_id).where(
                        Device.user_id == self._user_id,
                        Device.revoked_at.is_(None),
                    )
                )
                if run_id is None
                else None
            )
            return LocalTargetSelection(
                device_id=current_device or profile.device_id,
                workspace_id=profile.workspace_id,
            )
