"""设备凭证与短期授权记录，不保存本机凭证明文"""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


class Device(Base):
    """已与用户绑定的本机；撤销后保留身份以拒绝凭证重放"""

    __tablename__ = "devices"
    __table_args__ = (
        Index("ix_devices_owner", "user_id", "revoked_at", "device_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "用户本机设备与凭证摘要"},
    )

    device_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="服务端分配的设备 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="设备所属用户 ID"
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="本机显示名称"
    )
    credential_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="本机随机凭证的 SHA-256 摘要，非可用凭证"
    )
    capabilities: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="本机报告的当前可用能力，不包含目录路径或凭证"
    )
    connection_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="当前设备连接 UUID，用于排除已替换的连接"
    )
    paired_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 配对完成时间"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 最近一次已认证连接通信时间"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 凭证撤销时间，空表示仍可认证"
    )


class DeviceAuthorization(Base):
    """客户端发起、浏览器批准的短期申请，两端使用不同的随机能力"""

    __tablename__ = "device_authorizations"
    __table_args__ = (
        UniqueConstraint("slot", name="uq_device_authorizations_slot"),
        UniqueConstraint("device_id", name="uq_device_authorizations_device"),
        CheckConstraint(
            "slot >= 0 AND slot < 1024",
            name="ck_device_authorizations_slot",
        ),
        Index("ix_device_authorizations_expiry", "expires_at"),
        {**MYSQL_TABLE_OPTIONS, "comment": "本机发起的浏览器授权申请与双端能力摘要"},
    )

    authorization_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="授权申请 UUID"
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="批准申请的用户 ID，批准前为空"
    )
    device_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="申请固定的设备 UUID"
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="客户端发起授权时的设备名称"
    )
    credential_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="客户端 Keychain 凭证的 SHA-256 摘要"
    )
    browser_token_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="浏览器批准能力的 SHA-256 摘要，不可用于设备认证",
    )
    slot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="短期记录独占的容量槽，范围 0 至 1023，清理后释放",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 发起时间"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 授权有效期截止时间"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 首次批准时间"
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 未批准申请的取消时间"
    )


class DeviceExecutionProfile(Base):
    """云端运行固定的本机工作区和权限，审批恢复继续使用原授权"""

    __tablename__ = "device_execution_profiles"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_thread_id",
            "source_run_id",
            name="uq_device_profiles_source",
        ),
        Index("ix_device_profiles_target", "user_id", "device_id", "profile_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "云端会话固定的本机工作区与权限快照"},
    )

    profile_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="执行配置 UUID"
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="授权用户 ID")
    device_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="固定执行设备 UUID"
    )
    source_thread_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="首次授权的云端会话 ID"
    )
    source_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="首次授权的云端运行 ID"
    )
    workspace_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="本机已授权工作区标识"
    )
    workspace_label: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="授权时的工作区显示名称"
    )
    access_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="本机确认选择：full、write_approval"
    )
    skills: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        JSON, nullable=False, comment="会话已授权技能的身份和内容摘要"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 授权时间"
    )
