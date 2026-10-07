"""用户命名项目的持久化身份"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


class Project(Base):
    """项目名称可修改，稳定身份用于文件、记忆和任务归属"""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
        Index("ix_projects_user_created", "user_id", "created_at", "id"),
        {**MYSQL_TABLE_OPTIONS, "comment": "用户项目"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="项目 ID")
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户 ID"
    )
    name: Mapped[str] = mapped_column(
        String(64).with_variant(String(64, collation="utf8mb4_bin"), "mysql"),
        nullable=False,
        comment="项目名称",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="UTC 更新时间"
    )
