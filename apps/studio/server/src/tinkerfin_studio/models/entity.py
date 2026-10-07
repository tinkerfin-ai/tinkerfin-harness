"""Agent 模型配置 ORM 实体"""

from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ModelConnection(Base):
    """一个用户的模型服务连接，同连接下模型共享地址与认证"""

    __tablename__ = "model_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "connection_id", name="uq_model_connections_owner_connection"
        ),
        {**MYSQL_TABLE_OPTIONS, "comment": "模型服务连接配置"},
    )
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="连接记录 ID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="连接所属用户 ID"
    )
    connection_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="连接 ID"
    )
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="连接显示名称"
    )
    provider_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="模型服务提供方标识，custom 表示自定义"
    )
    api_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="接口类型：openai_chat_completions、ollama",
    )
    base_url: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="模型服务基础地址"
    )
    auth_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="认证方式：api_key 密钥认证、none 无需认证"
    )
    api_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="服务密钥（明文）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=_utcnow, comment="UTC 创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        comment="UTC 更新时间",
    )


class AgentModel(Base):
    """用户可选模型，地址与认证由所属连接提供"""

    __tablename__ = "agent_models"
    __table_args__ = (
        UniqueConstraint("user_id", "model_id", name="uq_agent_models_owner_model"),
        Index(
            "ix_agent_models_enabled_order", "user_id", "enabled", "sort_order", "id"
        ),
        Index("ix_agent_models_connection", "user_id", "connection_id"),
        Index("ix_agent_models_default", "user_id", "is_default", "enabled"),
        {**MYSQL_TABLE_OPTIONS, "comment": "模型配置"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="模型配置 ID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="模型配置所属用户 ID"
    )
    model_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型 ID")
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="模型显示名称"
    )
    connection_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="所属连接 ID"
    )
    chat_options: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, default=dict, comment="聊天生成参数"
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="服务提供方模型名称"
    )
    image_support: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment="图片输入能力：supported 支持、unsupported 不支持、unknown 未知",
    )
    reasoning_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否启用模型推理",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用模型"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否为默认模型",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="模型排序值（升序）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=_utcnow, comment="UTC 创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        comment="UTC 更新时间",
    )
