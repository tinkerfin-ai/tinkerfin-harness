"""把本轮授权的本机资源交给框架按需准备"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from deepagents.backends.protocol import BackendProtocol

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_notifications import Notifications, NotificationScope
from tinkerfin_remote import RemoteTools, WorkspaceRejected
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.devices.profiles import DeviceProfileBinding, DeviceProfiles
from tinkerfin_studio.devices.service import DeviceService
from tinkerfin_studio.infrastructure.database import Database


class ComputerWorkspace:
    """仅绑定业务身份，工具发现、审批恢复和工作区生命周期由框架负责"""

    def __init__(
        self,
        *,
        database: Database,
        remote_tools: RemoteTools,
        notifications: Notifications,
        user_id: int,
        project_id: str,
        access_mode: AccessMode,
    ) -> None:
        self._database = database
        self._remote_tools = remote_tools
        self._notifications = notifications
        self._user_id = user_id
        self._project_id = project_id
        self._access_mode: AccessMode = access_mode

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncIterator[PreparedWorkspace[object, BackendProtocol]]:
        """提交运行的设备绑定后借用远程资源，断线或恢复均不替换已绑定目标"""
        devices = DeviceService(self._database)
        # 先订阅再读取，避免查询与设备连接之间漏掉唤醒；等待期间不占数据库事务
        # 已固定设备被替换时结束本次操作，不把原审批转交给新设备
        async with self._notifications.subscribe(
            scope=NotificationScope(f"ns_{self._user_id}"),
            topics=("studio.devices.changed",),
        ) as changes:
            binding: DeviceProfileBinding | None = None
            while binding is None:
                try:
                    async with self._database.session() as session:
                        binding = await DeviceProfiles(session, self._user_id).capture(
                            identity=identity,
                            project_id=self._project_id,
                            access_mode=self._access_mode,
                        )
                        await session.commit()
                except BusinessException as error:
                    if error.error_code != DeviceErrorCode.NOT_FOUND:
                        raise
                    await anext(changes)
            while True:
                visible = await devices.list_devices(self._user_id)
                device = next(
                    (item for item in visible if item.device_id == binding.device_id),
                    None,
                )
                if device is None:
                    raise WorkspaceRejected("本次操作绑定的设备已撤销，请重新发起操作")
                if device.online:
                    break
                await anext(changes)
        workspace = self._remote_tools.endpoint(
            namespace=identity.namespace,
            target=binding.device_id,
        ).workspace(authorization=binding.profile_id)
        async with workspace.prepare(identity) as prepared:
            yield prepared
