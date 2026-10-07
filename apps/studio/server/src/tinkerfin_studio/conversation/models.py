"""会话归属、Run 注册与恢复认领 ORM 实体"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base

TitleSource = Literal["default", "generated", "user", "unknown"]
TitleGenerationStatus = Literal["idle", "running", "succeeded", "failed", "skipped"]

_PRIMARY_KEY = BigInteger().with_variant(Integer, "sqlite")


class ConversationThread(Base):
    """用户归属、产品控制与 Trace 列表摘要"""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_conversation_threads_thread"),
        Index(
            "ix_conversation_threads_project_history",
            "user_id",
            "project_id",
            "archived",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_conversation_threads_user_updated",
            "user_id",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_conversation_threads_user_pinned_updated",
            "user_id",
            "deleted_at",
            "pinned",
            "updated_at",
            "id",
        ),
        Index(
            "ix_conversation_threads_user_status_updated",
            "user_id",
            "status",
            "updated_at",
            "id",
        ),
        {**MYSQL_TABLE_OPTIONS, "comment": "用户会话"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="会话主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户 ID"
    )
    project_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="所属项目 ID"
    )
    archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="是否归档",
    )
    thread_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="会话 ID"
    )
    title: Mapped[str] = mapped_column(String(32), nullable=False, comment="会话标题")
    title_source: Mapped[TitleSource] = mapped_column(
        String(16),
        nullable=False,
        default="default",
        server_default="default",
        comment="标题来源：default 默认、generated 自动生成、user 手动、unknown 未知",
    )
    title_generation_status: Mapped[TitleGenerationStatus] = mapped_column(
        String(16),
        nullable=False,
        default="idle",
        server_default="idle",
        comment="标题生成状态：idle 未开始、running 生成中、succeeded 成功、failed 失败、skipped 跳过",
    )
    title_seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="标题更新序号",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="idle",
        comment="会话状态：idle、running、waiting_approval、error、deleting",
    )
    last_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="最近主运行 ID"
    )
    last_access_mode: Mapped[AccessMode] = mapped_column(
        String(32),
        nullable=False,
        default="full",
        server_default="full",
        comment="最近主运行文件审批模式：full、write_approval",
    )
    last_model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近主运行模型 ID"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="用户与助手消息总数"
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="工具调用数"
    )
    has_pending_interrupt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否有待处理交互"
    )
    pending_interaction_kind: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="待处理交互类型"
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否置顶"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 最近会话活动时间"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 删除时间"
    )


class ConversationRunRegistration(Base):
    """会话运行记录"""

    __tablename__ = "conversation_run_registrations"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id",
            "run_id",
            name="uq_conversation_run_registrations_thread_run",
        ),
        Index(
            "ix_conversation_run_registrations_thread_started",
            "conversation_thread_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_conversation_run_registrations_thread_status",
            "conversation_thread_id",
            "status",
            "updated_at",
        ),
        {**MYSQL_TABLE_OPTIONS, "comment": "会话运行记录"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="运行记录 ID"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键"
    )
    run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="主运行 ID"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="分支或恢复的来源运行 ID"
    )
    access_mode: Mapped[AccessMode] = mapped_column(
        String(32),
        nullable=False,
        default="full",
        server_default="full",
        comment="文件审批模式：full、write_approval",
    )
    model_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="主运行模型 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="preparing",
        comment="运行状态：preparing、starting、running、waiting、succeeded、failed、cancelled、abandoned",
    )
    input_json: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="运行请求内容"
    )
    service_bindings: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"web_search": None, "image_generation": None},
        comment="搜索与图片生成服务 ID 及配置摘要",
    )
    preparation_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=lambda: uuid4().hex,
        comment="运行准备标识",
    )
    terminal_outcome: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="运行终态结果"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="运行终态错误码"
    )
    trace_generation: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, comment="Trace 数据代次标识"
    )
    trace_as_of_seq: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="会话摘要对应的 Trace 事件序号"
    )
    trace_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime().with_variant(DATETIME(fsp=6), "mysql"),
        nullable=True,
        comment="UTC Trace 观测时间（微秒精度）",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 请求注册时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 运行结束时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 最近状态更新时间"
    )


class ConversationInterruptClaim(Base):
    """不复制 interrupt payload 的恢复请求原子认领与结算"""

    __tablename__ = "conversation_interrupt_claims"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id",
            "interrupt_id",
            name="uq_conversation_interrupt_claims_thread_interrupt",
        ),
        Index(
            "ix_conversation_interrupt_claims_run_status",
            "conversation_thread_id",
            "claimed_run_id",
            "status",
        ),
        {
            **MYSQL_TABLE_OPTIONS,
            "comment": "会话交互恢复记录",
        },
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="认领主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键"
    )
    interrupt_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="交互中断 ID",
    )
    source_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="交互来源运行 ID"
    )
    claimed_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="交互恢复运行 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="claimed",
        comment="处理状态：claimed 已认领、resolved 已恢复、cancelled 已取消",
    )
    resolution_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="交互恢复或取消凭据",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 认领时间"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 交互恢复时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="UTC 最近状态更新时间"
    )


__all__ = [
    "ConversationInterruptClaim",
    "ConversationRunRegistration",
    "ConversationThread",
]
