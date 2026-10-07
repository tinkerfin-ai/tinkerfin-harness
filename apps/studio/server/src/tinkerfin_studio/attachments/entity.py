"""Studio 附件的归属和存储记录"""

from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


class AttachmentFile(Base):
    """记录附件原件、使用归属和可访问状态"""

    __tablename__ = "conversation_attachments"
    __table_args__ = (
        Index("ix_conversation_attachments_owner", "user_id", "thread_id"),
        Index("ix_conversation_attachments_cleanup", "thread_id", "created_at"),
        {**MYSQL_TABLE_OPTIONS, "comment": "会话附件"},
    )
    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="附件 ID",
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="附件所属用户 ID"
    )
    project_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="附件所属项目 ID"
    )
    thread_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="所属会话 ID，草稿为空"
    )
    message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="首次使用或生成附件的消息 ID"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="文件名")
    mime_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="文件媒体类型，待确认上传为空"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="文件大小（字节），待确认上传为声明值"
    )
    sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="文件内容 SHA-256 校验值"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="uploading",
        comment="附件状态：uploading、processing、ready、deleting",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="user",
        comment="附件来源：user 用户上传、tool 工具生成",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        comment="UTC 创建时间",
    )


class AttachmentCollection(Base):
    """保存任务配置或运行结果的独立附件归属，配置快照不可改写"""

    __tablename__ = "attachment_collections"
    __table_args__ = (
        Index("ix_attachment_collections_owner_task", "user_id", "task_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "自动化任务附件集合"},
    )
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="附件集合 ID",
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属用户 ID")
    project_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="集合所属项目 ID"
    )
    purpose: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="集合用途：input 任务输入、execution 运行附件",
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="关联自动化任务 ID"
    )
    configuration: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, default=dict, comment="任务或运行输入快照"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        comment="UTC 创建时间",
    )


class AttachmentReference(Base):
    """一个附件可由多个任务配置和运行保留"""

    __tablename__ = "attachment_references"
    __table_args__ = (
        Index("ix_attachment_references_file", "attachment_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "附件集合与文件关联"},
    )
    collection_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="附件集合 ID"
    )
    attachment_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="附件 ID"
    )
