"""用户 ORM 实体"""

from sqlalchemy import JSON, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import MYSQL_TABLE_OPTIONS, Base


class User(Base):
    """可登录 Studio 的系统用户"""

    __tablename__ = "users"
    __table_args__ = {**MYSQL_TABLE_OPTIONS, "comment": "TinkerFin Studio 登录用户"}

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="用户主键"
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="登录用户名"
    )
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="展示名称"
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, comment="头像 HTTPS URL"
    )
    password_hash: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="带算法、参数和独立盐值的密码哈希"
    )
    roles: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, comment="用户角色列表"
    )
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否禁止登录"
    )
