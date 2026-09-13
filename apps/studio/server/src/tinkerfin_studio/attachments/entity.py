"""Studio 附件的归属和存储记录"""

from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import Base


class AttachmentFile(Base):
    """记录附件原件、使用归属和可访问状态"""

    __tablename__ = "conversation_attachments"
    __table_args__ = (
        Index("ix_conversation_attachments_owner", "user_id", "thread_id"),
        Index("ix_conversation_attachments_cleanup", "thread_id", "created_at"),
        {"comment": "会话上传和生成附件的持久化引用"},
    )
    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="服务端随机附件 ID，同时作为不透明存储标识",
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="附件所属用户 ID"
    )
    thread_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="附件绑定的会话 ID；空值表示未发送草稿"
    )
    message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="首次使用或生成附件的权威消息 ID"
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="用户可见文件名，不用于存储路径"
    )
    mime_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="经服务端验证的文件媒体类型"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="原件大小，单位为字节"
    )
    sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="原件完整内容的 SHA-256 校验值"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="uploading",
        comment="uploading、ready 或 deleting；仅 ready 可用",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="user",
        comment="user 表示用户上传，tool 表示工具生成",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        comment="UTC 创建时间，用于未发送附件的清理",
    )


class AttachmentCollection(Base):
    """保存任务配置或运行结果的独立附件归属，配置快照不可改写"""

    __tablename__ = "attachment_collections"
    __table_args__ = (
        Index("ix_attachment_collections_owner_task", "user_id", "task_id"),
        {"comment": "自动化任务配置和运行附件的持久归属"},
    )
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="集合稳定ID，任务配置由请求ID推导，运行使用执行ID",
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属用户ID")
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="input为不可变任务配置，execution为运行附件"
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="关联自动化任务ID，删除任务后仍保留历史引用"
    )
    configuration: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, default=dict, comment="不含凭据的不可变任务或运行输入快照"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        comment="UTC创建时间",
    )


class AttachmentReference(Base):
    """一个附件可由多个任务配置和运行保留"""

    __tablename__ = "attachment_references"
    __table_args__ = (
        Index("ix_attachment_references_file", "attachment_id"),
        {"comment": "自动化附件集合与文件引用，由服务校验归属"},
    )
    collection_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="附件集合ID"
    )
    attachment_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="附件文件ID"
    )
