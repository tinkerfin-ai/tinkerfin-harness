"""设备授权、凭证核验与连接替换，所有写入使用短事务"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_sqlalchemy import SqlTransaction
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.devices.models import (
    Device,
    DeviceAuthorization,
)
from tinkerfin_studio.devices.schemas import (
    AuthorizationCreated,
    AuthorizationPreview,
    AuthorizationStart,
    AuthorizationState,
    AuthorizationStatus,
    DeviceCapabilities,
    DeviceCapabilityUpdate,
    DevicePresence,
    DeviceProject,
    DeviceView,
    PairedDevice,
)
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.projects.repository import ProjectRepository


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DevicePrincipal:
    """已认证设备的归属，不包含可再次用于认证的凭证"""

    device_id: str
    user_id: int
    name: str

    @property
    def namespace(self) -> str:
        return f"ns_{self.user_id}"


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    """一条已认证连接的身份，旧连接不能更新新连接的在线状态"""

    principal: DevicePrincipal
    connection_id: str


@dataclass(frozen=True, slots=True)
class DeviceApproval:
    """已提交的授权结果及需要停止接收操作的旧设备"""

    preview: AuthorizationPreview
    revoked_device_ids: tuple[str, ...]


class DeviceService:
    """管理已登录用户授权的本机接入，数据库资源由应用借入

    申请有效期为十分钟，两端能力分离；浏览器批准按用户串行提交。
    同一申请只生效一次，重放不能撤销后来已连接的设备。
    设备连接和任务运行分开：离线不等于已停止，撤销也不证明离线任务已取消。
    """

    def __init__(
        self, database: Database, *, now: Callable[[], datetime] = _now
    ) -> None:
        self._database = database
        self._now = now

    def _time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("设备业务时钟必须包含时区")
        return value.astimezone(UTC).replace(tzinfo=None)

    @asynccontextmanager
    async def _session(self, *, read: bool = False) -> AsyncGenerator[AsyncSession]:
        async with SqlTransaction(self._database.engine, read_only=read) as connection:
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="rollback_only",
            ) as session:
                yield session
                if not read:
                    await session.flush()

    async def create_authorization(
        self, request: AuthorizationStart
    ) -> AuthorizationCreated:
        """创建短期申请；唯一容量槽将全部短期申请记录限制在 1024 个以内"""
        now = self._time()
        try:
            async with self._session() as session:
                await session.execute(
                    delete(DeviceAuthorization).where(
                        (
                            DeviceAuthorization.approved_at.is_(None)
                            & (DeviceAuthorization.expires_at <= now)
                        )
                        | (DeviceAuthorization.approved_at <= now - timedelta(days=1))
                    )
                )
                occupied = set(
                    await session.scalars(
                        select(DeviceAuthorization.slot).where(
                            DeviceAuthorization.slot.is_not(None)
                        )
                    )
                )
                slot = next(
                    (value for value in range(1024) if value not in occupied), None
                )
                if slot is None:
                    raise BusinessException(DeviceErrorCode.LIMIT_REACHED)
                browser_token = secrets.token_hex(32)
                authorization = DeviceAuthorization(
                    authorization_id=str(uuid4()),
                    user_id=None,
                    device_id=str(uuid4()),
                    name=request.name,
                    credential_digest=_digest(request.credential.get_secret_value()),
                    browser_token_digest=_digest(browser_token),
                    slot=slot,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    approved_at=None,
                    cancelled_at=None,
                )
                session.add(authorization)
                await session.flush()
                return AuthorizationCreated(
                    authorization_id=authorization.authorization_id,
                    browser_token=browser_token,
                    expires_at=authorization.expires_at.replace(tzinfo=UTC),
                )
        except IntegrityError as error:
            # 并发创建争用同一容量槽时拒绝本次申请，不超额写入或隐式重试
            raise BusinessException(DeviceErrorCode.LIMIT_REACHED) from error

    async def _authorization(
        self,
        session: AsyncSession,
        authorization_id: str,
        proof: str,
        *,
        browser: bool,
        lock: bool = False,
    ) -> DeviceAuthorization:
        query = select(DeviceAuthorization).where(
            DeviceAuthorization.authorization_id == authorization_id
        )
        record = await session.scalar(query.with_for_update() if lock else query)
        expected = (
            None
            if record is None
            else record.browser_token_digest
            if browser
            else record.credential_digest
        )
        if (
            record is None
            or expected is None
            or not hmac.compare_digest(expected, _digest(proof))
        ):
            raise BusinessException(DeviceErrorCode.NOT_FOUND)
        return record

    async def _authorization_status(
        self, session: AsyncSession, record: DeviceAuthorization
    ) -> AuthorizationStatus:
        state: AuthorizationState
        paired = None
        deadline = (
            record.expires_at
            if record.approved_at is None
            else record.approved_at + timedelta(days=1)
        )
        if deadline <= self._time():
            state = "expired"
        elif record.cancelled_at is not None:
            state = "cancelled"
        elif record.approved_at is not None:
            device = await session.get(Device, record.device_id)
            owner = (
                await session.get(User, record.user_id)
                if record.user_id is not None
                else None
            )
            if (
                device is None
                or device.revoked_at is not None
                or owner is None
                or owner.disabled
            ):
                state = "superseded"
            else:
                state = "authorized"
                paired = self._paired(device)
        else:
            state = "waiting"
        return AuthorizationStatus(
            authorization_id=record.authorization_id, state=state, device=paired
        )

    async def authorization_status(
        self, authorization_id: str, credential: str
    ) -> AuthorizationStatus:
        """客户端证明只用于查询自己的申请，不代替网页登录授权"""
        async with self._session(read=True) as session:
            record = await self._authorization(
                session, authorization_id, credential, browser=False
            )
            return await self._authorization_status(session, record)

    async def preview_authorization(
        self, user_id: int, authorization_id: str, browser_token: str
    ) -> AuthorizationPreview:
        """批准页显示服务端记录；已经绑定的申请不向其他账号开放"""
        async with self._session(read=True) as session:
            record = await self._authorization(
                session, authorization_id, browser_token, browser=True
            )
            await self._browser_owner(session, user_id, record)
            return self._preview(
                record, (await self._authorization_status(session, record)).state
            )

    @staticmethod
    def _preview(
        record: DeviceAuthorization, state: AuthorizationState
    ) -> AuthorizationPreview:
        return AuthorizationPreview(
            authorization_id=record.authorization_id,
            name=record.name,
            expires_at=record.expires_at.replace(tzinfo=UTC),
            state=state,
        )

    @staticmethod
    async def _browser_owner(
        session: AsyncSession, user_id: int, record: DeviceAuthorization
    ) -> None:
        user = await session.get(User, user_id)
        if user is None or user.disabled:
            raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
        if record.user_id is not None and record.user_id != user_id:
            raise BusinessException(DeviceErrorCode.AUTHORIZATION_CONFLICT)

    async def approve_authorization(
        self,
        user_id: int,
        authorization_id: str,
        browser_token: str,
        expected_user_id: int,
    ) -> DeviceApproval:
        """同账号批准按提交顺序替换设备；重复批准仅返回原申请的实际状态"""
        if user_id != expected_user_id:
            raise BusinessException(DeviceErrorCode.AUTHORIZATION_CONFLICT)
        now = self._time()
        async with self._session() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.disabled:
                raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
            record = await self._authorization(
                session, authorization_id, browser_token, browser=True, lock=True
            )
            await self._browser_owner(session, user_id, record)
            status = await self._authorization_status(session, record)
            if status.state != "waiting":
                return DeviceApproval(
                    self._preview(record, status.state),
                    await self._revoked_device_ids(session, user_id),
                )
            # 用户行锁覆盖旧设备撤销和新设备创建，跨进程也只会留下一个有效身份
            await session.execute(
                update(Device)
                .where(Device.user_id == user_id, Device.revoked_at.is_(None))
                .values(revoked_at=now, connection_id=None)
            )
            session.add(
                Device(
                    device_id=record.device_id,
                    user_id=user_id,
                    name=record.name,
                    credential_digest=record.credential_digest,
                    capabilities={},
                    connection_id=None,
                    paired_at=now,
                    last_seen_at=None,
                    revoked_at=None,
                )
            )
            record.user_id = user_id
            record.approved_at = now
            return DeviceApproval(
                self._preview(record, "authorized"),
                await self._revoked_device_ids(session, user_id),
            )

    @staticmethod
    async def _revoked_device_ids(
        session: AsyncSession, user_id: int
    ) -> tuple[str, ...]:
        # 返回已撤销设备，使批准后的通知失败也能在重试时完成停止请求
        return tuple(
            await session.scalars(
                select(Device.device_id).where(
                    Device.user_id == user_id, Device.revoked_at.is_not(None)
                )
            )
        )

    async def cancel_authorization(
        self, authorization_id: str, credential: str
    ) -> AuthorizationStatus:
        """取消未批准申请；批准先提交时返回成功身份，由客户端完成保存"""
        async with self._session() as session:
            record = await self._authorization(
                session, authorization_id, credential, browser=False, lock=True
            )
            status = await self._authorization_status(session, record)
            if status.state == "waiting":
                record.cancelled_at = self._time()
                return AuthorizationStatus(
                    authorization_id=authorization_id, state="cancelled"
                )
            return status

    async def deny_authorization(
        self, authorization_id: str, browser_token: str
    ) -> AuthorizationPreview:
        """浏览器取消其持有入口的待批准申请，不撤销已经提交的设备"""
        async with self._session() as session:
            record = await self._authorization(
                session, authorization_id, browser_token, browser=True, lock=True
            )
            status = await self._authorization_status(session, record)
            if status.state == "waiting":
                record.cancelled_at = self._time()
                return self._preview(record, "cancelled")
            return self._preview(record, status.state)

    async def rename(self, token: str | None, name: str) -> PairedDevice:
        """已认证设备更新自己的显示名称，不影响身份及授权目录"""
        async with self._session() as session:
            device = await self._authenticated(session, token, lock=True)
            device.name = name
            return self._paired(device)

    @staticmethod
    def _paired(device: Device) -> PairedDevice:
        return PairedDevice(
            device_id=device.device_id,
            name=device.name,
            namespace=f"ns_{device.user_id}",
        )

    @staticmethod
    def _principal(device: Device) -> DevicePrincipal:
        return DevicePrincipal(device.device_id, device.user_id, device.name)

    async def _credential_device(
        self, session: AsyncSession, token: str | None, *, lock: bool = False
    ) -> Device:
        if token is None or re.fullmatch(r"[0-9a-f-]{36}\.[0-9a-f]{64}", token) is None:
            raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
        device_id, credential = token.split(".", 1)
        query = select(Device).where(Device.device_id == device_id)
        device = await session.scalar(query.with_for_update() if lock else query)
        if device is None or not hmac.compare_digest(
            device.credential_digest, _digest(credential)
        ):
            raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
        return device

    async def _authenticated(
        self, session: AsyncSession, token: str | None, *, lock: bool = False
    ) -> Device:
        device = await self._credential_device(session, token, lock=lock)
        if device.revoked_at is not None:
            raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
        user = await session.get(User, device.user_id)
        if user is None or user.disabled:
            raise BusinessException(DeviceErrorCode.UNAUTHORIZED)
        return device

    async def authenticate(self, token: str | None) -> DevicePrincipal:
        """核验设备凭证、撤销状态与用户是否仍可使用账户"""
        async with self._session(read=True) as session:
            return self._principal(await self._authenticated(session, token))

    async def connect(self, token: str, presence: DevicePresence) -> DeviceConnection:
        """替换设备当前连接并保存本机此时可用的能力"""
        async with self._session() as session:
            device = await self._authenticated(session, token, lock=True)
            connection_id = str(uuid4())
            device.connection_id = connection_id
            device.last_seen_at = self._time()
            device.capabilities = dict(presence.capabilities.model_dump(mode="json"))
            return DeviceConnection(self._principal(device), connection_id)

    async def update_capabilities(
        self, token: str | None, report: DeviceCapabilityUpdate
    ) -> DevicePrincipal:
        """只更新当前连接的能力，迟到报告不能覆盖新连接或续延在线状态"""
        async with self._session() as session:
            device = await self._authenticated(session, token, lock=True)
            if device.connection_id != report.connection_id:
                raise BusinessException(DeviceErrorCode.CONNECTION_REPLACED)
            device.capabilities = dict(report.capabilities.model_dump(mode="json"))
            return self._principal(device)

    async def touch(self, token: str, connection: DeviceConnection) -> None:
        """每次任务通信前复核凭证和连接归属，随后更新在线时间"""
        async with self._session() as session:
            device = await self._authenticated(session, token, lock=True)
            if (
                device.connection_id != connection.connection_id
                or device.device_id != connection.principal.device_id
            ):
                raise BusinessException(DeviceErrorCode.CONNECTION_REPLACED)
            device.last_seen_at = self._time()

    async def disconnect(self, connection: DeviceConnection) -> None:
        """只清除本条连接的在线身份，不影响后来建立的连接"""
        async with self._session() as session:
            device = await session.scalar(
                select(Device)
                .where(
                    Device.device_id == connection.principal.device_id,
                    Device.user_id == connection.principal.user_id,
                )
                .with_for_update()
            )
            if device is not None and device.connection_id == connection.connection_id:
                device.connection_id = None

    async def list_devices(self, user_id: int) -> list[DeviceView]:
        """返回本人当前有效设备，不公开历史凭证或授权能力"""
        async with self._session(read=True) as session:
            devices = await session.scalars(
                select(Device)
                .where(Device.user_id == user_id, Device.revoked_at.is_(None))
                .order_by(Device.paired_at.desc(), Device.device_id)
                .limit(64)
            )
            return [self._view(device) for device in devices]

    def _view(self, device: Device) -> DeviceView:
        online = (
            device.revoked_at is None
            and device.connection_id is not None
            and device.last_seen_at is not None
            and device.last_seen_at > self._time() - timedelta(seconds=30)
        )
        return DeviceView(
            device_id=device.device_id,
            name=device.name,
            online=online,
            revoked=device.revoked_at is not None,
            paired_at=device.paired_at.replace(tzinfo=UTC),
            last_seen_at=None
            if device.last_seen_at is None
            else device.last_seen_at.replace(tzinfo=UTC),
            capabilities=DeviceCapabilities.model_validate(device.capabilities),
        )

    async def require_workspace(
        self, user_id: int, device_id: str, workspace_id: str
    ) -> DeviceProject:
        """核验有效设备与项目属于同一用户，项目目录由本机自动准备"""
        async with self._session(read=True) as session:
            device = await session.scalar(
                select(Device).where(
                    Device.device_id == device_id,
                    Device.user_id == user_id,
                    Device.revoked_at.is_(None),
                )
            )
            if device is None:
                raise BusinessException(DeviceErrorCode.NOT_FOUND)
            project = await ProjectRepository(session, user_id).require(workspace_id)
            return DeviceProject(project_id=project.id, name=project.name)

    async def revoke(self, user_id: int, device_id: str) -> None:
        """使本人设备凭证失效；不把离线执行推断为已经停止"""
        async with self._session() as session:
            device = await session.scalar(
                select(Device)
                .where(Device.device_id == device_id, Device.user_id == user_id)
                .with_for_update()
            )
            if device is None:
                raise BusinessException(DeviceErrorCode.NOT_FOUND)
            if device.revoked_at is None:
                device.revoked_at = self._time()
                device.connection_id = None

    async def revoke_current(self, token: str | None) -> DevicePrincipal:
        """凭设备密钥撤销自身，重复请求只确认撤销结果，不恢复任何访问权"""
        async with self._session() as session:
            # 撤销提交后响应可能丢失；保留凭证摘要只用于确认同一设备的重试
            device = await self._credential_device(session, token, lock=True)
            if device.revoked_at is None:
                device.revoked_at = self._time()
            device.connection_id = None
            return self._principal(device)
