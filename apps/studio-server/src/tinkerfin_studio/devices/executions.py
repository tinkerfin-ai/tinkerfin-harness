"""核验本机工具使用的云端运行、项目和技能授权"""

from dataclasses import dataclass

from pydantic import TypeAdapter
from sqlalchemy import select

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.devices.models import DeviceExecutionProfile
from tinkerfin_studio.devices.schemas import LocalExecutionConfiguration
from tinkerfin_studio.devices.service import DevicePrincipal, DeviceService
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.skills.schemas import SkillReference

_skills = TypeAdapter(tuple[SkillReference, ...])


@dataclass(frozen=True, slots=True)
class AuthorizedDeviceExecution:
    """已复核主体与云端运行，本机只能取得其工作区配置"""

    principal: DevicePrincipal
    identity: RunIdentity
    configuration: LocalExecutionConfiguration


class DeviceExecutions:
    """统一核验本机配置、技能和附件请求，不下发模型或会话上下文"""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._devices = DeviceService(database)

    async def authorize(
        self,
        token: str | None,
        authorization: str,
        *,
        thread_id: str,
        run_id: str,
    ) -> AuthorizedDeviceExecution:
        """拒绝跨设备、跨会话、已终止运行或已撤销的工作区授权"""
        principal = await self._devices.authenticate(token)
        identity = RunIdentity(
            namespace=principal.namespace, thread_id=thread_id, run_id=run_id
        )
        async with self._database.session() as session:
            profile = await session.scalar(
                select(DeviceExecutionProfile).where(
                    DeviceExecutionProfile.profile_id == authorization,
                    DeviceExecutionProfile.user_id == principal.user_id,
                )
            )
            if (
                profile is None
                or profile.device_id != principal.device_id
                or profile.source_thread_id != thread_id
            ):
                raise BusinessException(DeviceErrorCode.EXECUTION_UNAVAILABLE)
            source = await session.scalar(
                select(ConversationRunRegistration)
                .join(
                    ConversationThread,
                    ConversationThread.id
                    == ConversationRunRegistration.conversation_thread_id,
                )
                .where(
                    ConversationThread.user_id == principal.user_id,
                    ConversationThread.thread_id == thread_id,
                    ConversationThread.project_id == profile.workspace_id,
                    ConversationThread.deleted_at.is_(None),
                    ConversationThread.archived.is_(False),
                    ConversationThread.status != "deleting",
                    ConversationRunRegistration.run_id == run_id,
                    ConversationRunRegistration.local_profile_id == authorization,
                    ConversationRunRegistration.status.in_(
                        ("preparing", "starting", "running")
                    ),
                )
            )
            if source is None:
                raise BusinessException(DeviceErrorCode.EXECUTION_UNAVAILABLE)
            configuration = LocalExecutionConfiguration(
                authorization=profile.profile_id,
                workspace_id=profile.workspace_id,
                skills=_skills.validate_python(profile.skills),
            )
        await self._devices.require_workspace(
            principal.user_id, principal.device_id, configuration.workspace_id
        )
        return AuthorizedDeviceExecution(principal, identity, configuration)
