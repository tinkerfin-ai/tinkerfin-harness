"""技能安装、导入草稿和运行内容引用的业务持久化"""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


class SkillInstallation(Base):
    """用户安装关系；实际文件由持久 Store 保存"""

    __tablename__ = "skill_installations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", "name", name="uq_skill_installations_owner_name"
        ),
        Index("ix_skill_installations_owner_updated", "user_id", "updated_at"),
        {**MYSQL_TABLE_OPTIONS, "comment": "用户技能安装记录"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="技能安装 ID")
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="安装所属用户 ID"
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        default="",
        server_default="",
        comment="所属项目 ID，空字符串表示个人库",
    )
    name: Mapped[str] = mapped_column(
        String(64).with_variant(String(64, collation="utf8mb4_bin"), "mysql"),
        nullable=False,
        comment="技能名称",
    )
    description: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="技能描述"
    )
    digest: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="技能目录 SHA-256 内容摘要"
    )
    source_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="安装来源：catalog、github、zip"
    )
    source_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="技能来源 ID，个人导入为空"
    )
    source_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="安装来源名称"
    )
    external_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="来源中的发布者限定标识"
    )
    source_revision: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="来源发布内容标识"
    )
    source_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, comment="来源页面地址"
    )
    author: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="技能作者"
    )
    topics: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="技能分类列表"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用技能"
    )
    file_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="技能文件数"
    )
    byte_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="技能文件总大小（字节）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 安装时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 安装状态更新时间"
    )


class ProjectSkillSetting(Base):
    """项目对个人技能的启用选择"""

    __tablename__ = "project_skill_settings"
    __table_args__ = (
        Index("ix_project_skill_settings_owner", "user_id", "project_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "项目个人技能启用配置"},
    )
    project_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="项目 ID"
    )
    installation_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="技能安装 ID"
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属用户 ID")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否在项目中启用"
    )


class SkillImportDraft(Base):
    """确认时只使用预览固定的内容，不再次访问远程仓库"""

    __tablename__ = "skill_import_drafts"
    __table_args__ = (
        Index("ix_skill_import_drafts_owner", "user_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "技能导入预览与确认结果"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="导入预览 ID")
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="导入所属用户 ID"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="导入来源：github、zip"
    )
    source_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, comment="GitHub 导入地址，ZIP 导入为空"
    )
    candidates: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        JSON, nullable=False, comment="技能预览条目及内容摘要"
    )
    selected_digests: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="已确认的内容摘要列表，未确认为空"
    )
    confirmed_project_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="确认安装的项目 ID，个人库为空字符串，未确认为空值",
    )
    installation_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="已确认的技能安装 ID 列表"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 预览创建时间"
    )


class SkillRunSnapshot(Base):
    """新运行捕获的技能集合，供历史展示和恢复关联保留当时选择"""

    __tablename__ = "skill_run_snapshots"
    __table_args__ = (
        Index("ix_skill_run_snapshots_owner", "user_id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "运行技能快照"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="技能快照 ID")
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="技能内容所属用户 ID"
    )
    project_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="所属项目 ID"
    )
    payload: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="技能安装 ID、名称、内容摘要及手动选择标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 快照捕获时间"
    )


class SkillOperationReceipt(Base):
    """与安装变更一起提交的结果，恢复和请求重放不重复执行写操作"""

    __tablename__ = "skill_operation_receipts"
    __table_args__ = (
        Index("ix_skill_operation_receipts_owner_created", "user_id", "created_at"),
        {**MYSQL_TABLE_OPTIONS, "comment": "技能操作回执"},
    )
    user_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, comment="操作所属用户 ID"
    )
    request_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        comment="技能操作请求 ID",
    )
    fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范化操作参数的 SHA-256 摘要"
    )
    result: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="操作结果"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 操作提交时间"
    )
