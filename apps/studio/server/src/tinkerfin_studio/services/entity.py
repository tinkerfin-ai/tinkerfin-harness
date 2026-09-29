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
        {**MYSQL_TABLE_OPTIONS, "comment": "用户搜索与图片生成服务配置"},
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="稳定服务配置 ID；清除后重建分配新值"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="配置所属用户 ID"
    )
    capability: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="web_search 网页搜索或 image_generation 图片生成",
    )
    provider_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="tavily、openai、fal 或 custom 接入方式"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否允许新运行使用本服务"
    )
    config: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="经能力类型校验的请求及结果配置，不含凭证"
    )
    api_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="服务密钥原文，不进入响应、日志或运行绑定",
    )
    test_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="最近主动测试的 success 或 failed；配置改变时清空",
    )
    test_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="最近主动测试的安全结果码"
    )
    test_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近测试所针对的执行配置摘要"
    )
    tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="最近主动测试时间，UTC"
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
