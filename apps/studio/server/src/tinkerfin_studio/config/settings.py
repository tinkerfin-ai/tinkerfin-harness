"""应用外部资源配置与读取入口"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from tinkerfin_studio.models.transport import normalize_model_origin

_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_DEFAULT_LOG_FILE_PATH = Path("logs/studio.log")
_SECRET_FILE_TARGETS = {
    "s3_storage_access_key_file": "s3_storage_access_key",
    "s3_storage_secret_key_file": "s3_storage_secret_key",
    "database_url_file": "database_url",
    "redis_runtime_password_file": "redis_runtime_password",
    "open_sandbox_api_key_file": "open_sandbox_api_key",
    "tavily_api_key_file": "tavily_api_key",
}


def _apply_secret_files(values: dict[str, str], directory: Path) -> None:
    """用容器 Secret 文件覆盖对应的普通配置值"""

    for file_key, target_key in _SECRET_FILE_TARGETS.items():
        raw_path = values.pop(file_key, None)
        if not raw_path:
            continue
        path = directory / raw_path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"{file_key.upper()} 无法读取: {path}") from error
        if not value:
            raise ValueError(f"{file_key.upper()} 内容不能为空")
        values[target_key] = value


class DatabaseSettings(BaseModel):
    """异步 SQLAlchemy 数据库连接配置"""

    model_config = ConfigDict(frozen=True)

    url: str = Field(
        min_length=1, repr=False, description="包含凭据的异步数据库连接地址"
    )
    echo: bool = Field(default=False, description="是否输出 SQL 日志")
    pool_size: int = Field(default=10, ge=1, description="连接池常驻连接数")
    max_overflow: int = Field(default=10, ge=0, description="连接池临时连接上限")
    pool_recycle: int = Field(
        default=3600, ge=-1, description="连接回收秒数，-1 表示关闭"
    )
    connection_budget: int = Field(
        ge=2, description="Studio 允许使用的 MySQL 总连接预算"
    )
    management_connection_reserve: int = Field(
        ge=1, description="为数据库管理与故障处理保留的连接数"
    )


class RedisConnectionSettings(BaseModel):
    """Studio Redis 连接配置"""

    model_config = ConfigDict(frozen=True)

    host: str = Field(min_length=1, description="Redis 主机")
    port: int = Field(ge=1, le=65535, description="Redis 端口")
    password: SecretStr | None = Field(
        default=None, repr=False, description="Redis 密码"
    )
    database: int = Field(ge=0, description="普通 Redis 逻辑库")
    max_connections: int = Field(default=80, ge=8, description="连接池最大连接数")
    socket_timeout_seconds: float = Field(
        default=10.0, gt=0, description="单次 Redis 命令超时秒数"
    )


class RedisRuntimeSettings(RedisConnectionSettings):
    """认证、checkpointer 与 Messaging 共用的 Redis 配置"""

    checkpoint_database: int = Field(
        ge=0, le=0, description="支持 RediSearch 的 checkpoint 逻辑库"
    )


class SandboxSettings(BaseModel):
    """OpenSandbox 控制面和持久化分配配置"""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(min_length=1, description="OpenSandbox 控制面域名与端口")
    protocol: Literal["http", "https"] = Field(description="OpenSandbox 连接协议")
    api_key: SecretStr | None = Field(
        default=None, repr=False, description="OpenSandbox API 密钥"
    )
    cpu: float = Field(
        default=1, gt=0, allow_inf_nan=False, description="每个新建沙箱的 CPU 核数上限"
    )
    memory_mib: int = Field(
        default=1024, gt=0, description="每个新建沙箱的内存上限，单位为 MiB"
    )
    warm_pool_size: int = Field(ge=0, description="全局预热 Sandbox 数量")
    workspace_root: str = Field(
        default="/workspace", description="Agent 文件系统虚拟根目录"
    )
    state_namespace: str = Field(
        min_length=1, max_length=64, description="Sandbox State 数据库命名空间"
    )


class S3StorageSettings(BaseModel):
    """应用存储桶及后端、浏览器各自可达的地址"""

    model_config = ConfigDict(frozen=True)
    bucket: str
    endpoint: str
    public_endpoint: str
    access_key: SecretStr
    secret_key: SecretStr


class Settings(BaseSettings):
    """Studio 服务进程配置"""

    model_config = SettingsConfigDict(extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file_enabled: bool = False
    log_file_path: Path = Field(
        default=_DEFAULT_LOG_FILE_PATH,
        description="日志文件路径；相对路径以配置文件所在目录为基准",
    )
    log_file_max_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1024, description="文件日志滚动阈值，单位为字节"
    )
    log_file_backup_count: int = Field(default=3, ge=1)

    @field_validator("log_file_path")
    @classmethod
    def resolve_log_file_path(cls, value: Path) -> Path:
        """固定日志位置，避免启动工作目录改变输出文件"""
        return (_DEFAULT_ENV_FILE.parent / value).resolve()

    s3_storage_bucket: str = Field(
        min_length=3, max_length=63, description="用户指定的应用存储桶，无默认值"
    )
    s3_storage_endpoint: str = "http://127.0.0.1:9000"
    s3_storage_public_endpoint: str = "http://127.0.0.1:9000"
    s3_storage_access_key: SecretStr
    s3_storage_secret_key: SecretStr

    @field_validator("s3_storage_bucket")
    @classmethod
    def validate_s3_storage_bucket(cls, value: str) -> str:
        """校验 MinIO 桶名，不修改用户输入或推导默认值"""
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value)
            or ".." in value
            or ".-" in value
            or "-." in value
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
        ):
            raise ValueError("S3_STORAGE_BUCKET 必须是合法的 MinIO 桶名")
        return value

    @field_validator("s3_storage_endpoint", "s3_storage_public_endpoint")
    @classmethod
    def validate_s3_storage_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("附件存储地址必须是 HTTP/HTTPS 服务地址，不含路径或凭据")
        return value.rstrip("/")

    @field_validator("s3_storage_access_key", "s3_storage_secret_key")
    @classmethod
    def validate_attachment_credential(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("附件存储凭据不能为空")
        return value

    @property
    def s3_storage(self) -> S3StorageSettings:
        """提供附件存储连接配置，密钥不进入公开接口"""
        return S3StorageSettings(
            bucket=self.s3_storage_bucket,
            endpoint=self.s3_storage_endpoint,
            public_endpoint=self.s3_storage_public_endpoint,
            access_key=self.s3_storage_access_key,
            secret_key=self.s3_storage_secret_key,
        )

    model_allowed_origins: tuple[str, ...] = Field(
        default=(),
        description="管理员允许访问的 HTTP、本机或内网模型服务来源，精确匹配协议、主机和端口",
    )

    @field_validator("model_allowed_origins", mode="before")
    @classmethod
    def validate_model_allowed_origins(cls, value: object) -> tuple[str, ...]:
        """从环境 JSON 数组或配置序列读取允许的模型服务来源"""
        adapter = TypeAdapter(tuple[str, ...])
        origins = (
            adapter.validate_json(value)
            if isinstance(value, str)
            else adapter.validate_python(value)
        )
        return tuple(dict.fromkeys(normalize_model_origin(item) for item in origins))

    database_url: str = Field(
        min_length=1, repr=False, description="异步 MySQL 连接地址"
    )
    database_echo: bool = Field(default=False, description="是否输出 SQL 日志")
    database_pool_size: int = Field(default=10, ge=1, description="数据库常驻连接数")
    database_max_overflow: int = Field(default=10, ge=0, description="数据库临时连接数")
    database_pool_recycle: int = Field(
        default=3600, ge=-1, description="数据库连接回收秒数"
    )
    database_connection_budget: int = Field(
        default=20, ge=1, description="Studio 进程的 MySQL 总连接预算"
    )
    database_management_connection_reserve: int = Field(
        default=10, ge=1, description="数据库管理与故障处理保留连接数"
    )

    redis_runtime_host: str = Field(
        default="127.0.0.1", min_length=1, description="Redis Runtime 主机"
    )
    redis_runtime_port: int = Field(
        default=6379, ge=1, le=65535, description="Redis Runtime 端口"
    )
    redis_runtime_password: SecretStr | None = Field(
        default=None, repr=False, description="Redis Runtime 密码"
    )
    redis_runtime_db: int = Field(default=0, ge=0, description="Redis Runtime 逻辑库")
    redis_runtime_checkpoint_db: int = Field(
        default=0, ge=0, le=0, description="checkpoint Redis 逻辑库"
    )
    redis_runtime_max_connections: int = Field(
        default=80, ge=8, description="Redis Runtime 最大连接数"
    )
    redis_runtime_socket_timeout_seconds: float = Field(
        default=10.0, gt=0, description="Redis Runtime 命令超时秒数"
    )
    open_sandbox_domain: str = Field(
        default="127.0.0.1:8091", min_length=1, description="OpenSandbox 域名与端口"
    )
    open_sandbox_protocol: Literal["http", "https"] = Field(
        default="http", description="OpenSandbox 连接协议"
    )
    open_sandbox_api_key: SecretStr | None = Field(
        default=None, repr=False, description="OpenSandbox API 密钥"
    )
    open_sandbox_cpu: float = Field(
        default=1, gt=0, allow_inf_nan=False, description="每个新建沙箱的 CPU 核数上限"
    )
    open_sandbox_memory_mib: int = Field(
        default=1024, gt=0, description="每个新建沙箱的内存上限，单位为 MiB"
    )
    open_sandbox_warm_pool_size: int = Field(
        default=0, ge=0, description="全局预热沙箱数量；0 表示首次使用时创建"
    )
    open_sandbox_workspace_root: str = Field(
        default="/workspace", min_length=1, description="Agent 文件系统虚拟根目录"
    )
    open_sandbox_state_namespace: str = Field(
        default="tinkerfin-studio",
        min_length=1,
        max_length=64,
        description="Sandbox State 命名空间",
    )

    auth_token_expire_seconds: int = Field(
        default=86400, ge=60, description="访问令牌有效秒数"
    )
    tavily_api_key: SecretStr | None = Field(
        default=None, repr=False, description="Tavily API 密钥"
    )

    @model_validator(mode="after")
    def validate_database_contract(self) -> Settings:
        """拒绝阻塞 Driver 与超出显式总连接预算的池配置"""

        if make_url(self.database_url).drivername != "mysql+asyncmy":
            raise ValueError("DATABASE_URL 必须使用 mysql+asyncmy 驱动")
        required_connections = self.database_pool_size + self.database_max_overflow
        if required_connections > self.database_connection_budget:
            raise ValueError(
                "DATABASE_CONNECTION_BUDGET 必须覆盖 pool_size + max_overflow"
            )
        return self

    @property
    def database(self) -> DatabaseSettings:
        """返回数据库资源使用的分组配置"""

        return DatabaseSettings(
            url=self.database_url,
            echo=self.database_echo,
            pool_size=self.database_pool_size,
            max_overflow=self.database_max_overflow,
            pool_recycle=self.database_pool_recycle,
            connection_budget=self.database_connection_budget,
            management_connection_reserve=self.database_management_connection_reserve,
        )

    @property
    def redis_runtime(self) -> RedisRuntimeSettings:
        """返回认证、Checkpointer 与 Messaging 共用的 Redis 配置"""

        return RedisRuntimeSettings(
            host=self.redis_runtime_host,
            port=self.redis_runtime_port,
            password=self.redis_runtime_password,
            database=self.redis_runtime_db,
            checkpoint_database=self.redis_runtime_checkpoint_db,
            max_connections=self.redis_runtime_max_connections,
            socket_timeout_seconds=self.redis_runtime_socket_timeout_seconds,
        )

    @property
    def sandbox(self) -> SandboxSettings:
        """返回 OpenSandbox 资源使用的分组配置"""

        return SandboxSettings(
            domain=self.open_sandbox_domain,
            protocol=self.open_sandbox_protocol,
            api_key=self.open_sandbox_api_key,
            cpu=self.open_sandbox_cpu,
            memory_mib=self.open_sandbox_memory_mib,
            warm_pool_size=self.open_sandbox_warm_pool_size,
            workspace_root=self.open_sandbox_workspace_root,
            state_namespace=self.open_sandbox_state_namespace,
        )


@lru_cache
def get_settings() -> Settings:
    """读取并缓存真实运行配置"""

    return load_settings()


def load_settings(*, env_file: str | Path | None = _DEFAULT_ENV_FILE) -> Settings:
    """读取配置，相对路径以指定配置文件或应用默认配置目录为基准"""

    values: dict[str, str] = {}
    config_directory = (
        Path(env_file).resolve().parent
        if env_file is not None
        else _DEFAULT_ENV_FILE.parent
    )
    if env_file is not None:
        values.update(
            {
                key.lower(): value
                for key, value in dotenv_values(env_file).items()
                if value is not None
            }
        )
    values.update(
        {
            key.lower(): value
            for key, value in os.environ.items()
            if key.lower() in Settings.model_fields
            or key.lower() in _SECRET_FILE_TARGETS
        }
    )
    _apply_secret_files(values, config_directory)
    log_path = Path(values.get("log_file_path", str(_DEFAULT_LOG_FILE_PATH)))
    values["log_file_path"] = str((config_directory / log_path).resolve())
    return Settings.model_validate(values)
