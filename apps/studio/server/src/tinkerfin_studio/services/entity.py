"""个人搜索与图片生成服务的持久化实体"""

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


class ServiceConfig(Base):
    """每位用户对每种能力至多保存一份服务配置"""

    __tablename__ = "service_configs"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "capability", name="uq_service_configs_owner_capability"
        ),
        Index("ix_service_configs_owner_enabled", "user_id", "enabled"),
        {**MYSQL_TABLE_OPTIONS, "comment": "搜索与图片生成服务配置"},
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="服务配置 ID")
    user_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="配置所属用户 ID"
    )
    capability: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="服务类型：web_search 网页搜索、image_generation 图片生成",
    )
    provider_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="服务提供方：tavily、openai、fal、custom"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用服务"
    )
    config: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="服务请求与结果配置"
    )
    api_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="服务密钥（明文）",
    )
    test_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="最近测试状态：success 成功、failed 失败",
    )
    test_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="最近测试结果码"
    )
    test_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近测试的配置摘要"
    )
    tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="UTC 最近测试时间"
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
