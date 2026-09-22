"""应用外部资源配置与读取入口"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources.utils import parse_env_vars
from sqlalchemy.engine import URL, make_url

from tinkerfin_studio.models.transport import normalize_model_origin

_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"
_DEFAULT_LOG_FILE_PATH = Path("server/logs/studio.log")


class _LiteralDotEnvSource(DotEnvSettingsSource):
    """保留凭据中的 ${变量} 字面值，与部署配置保持一致"""

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        return parse_env_vars(
            dotenv_values(
                file_path, encoding=self.env_file_encoding or "utf-8", interpolate=False
            ),
            self.case_sensitive,
            self.env_ignore_empty,
            self.env_parse_none_str,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """已校验的数据库连接参数，回收时间以秒计，-1 表示关闭"""

    url: str = field(repr=False)
    echo: bool
    pool_size: int
    max_overflow: int
    pool_recycle: int


@dataclass(frozen=True, slots=True)
class RedisConnectionSettings:
    """已校验的 Redis 连接参数，命令超时以秒计"""

    host: str
    port: int
    password: SecretStr | None = field(repr=False)
    database: int
    max_connections: int
    socket_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class RedisRuntimeSettings(RedisConnectionSettings):
    """认证、检查点与消息服务共用的 Redis 配置，检查点固定使用逻辑库 0"""

    checkpoint_database: int


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """已校验的沙箱配置，CPU 为核数上限，内存单位为 MiB"""

    domain: str
    protocol: Literal["http", "https"]
    api_key: SecretStr | None = field(repr=False)
    cpu: float
    memory_mib: int
    warm_pool_size: int
    workspace_root: str
    state_namespace: str


@dataclass(frozen=True, slots=True)
class S3StorageSettings:
    """已校验的存储配置，区分后端连接与浏览器上传下载地址"""

    bucket: str
    endpoint: str
    public_endpoint: str
    access_key: SecretStr = field(repr=False)
    secret_key: SecretStr = field(repr=False)


class Settings(BaseSettings):
    """按构造参数、环境变量、指定配置文件、默认值的优先级读取并校验配置"""

    model_config = SettingsConfigDict(extra="ignore", env_file_encoding="utf-8")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """使用原生来源优先级，仅关闭配置文件中的变量插值"""
        assert isinstance(dotenv_settings, DotEnvSettingsSource)
        return (
            init_settings,
            env_settings,
            _LiteralDotEnvSource(
                settings_cls,
                env_file=dotenv_settings.env_file,
                env_file_encoding=dotenv_settings.env_file_encoding,
            ),
        )

    mysql_host: str = Field(default="127.0.0.1", min_length=1)
    mysql_port: int = Field(default=13306, ge=1, le=65535)
    mysql_business_database: str = "tinkerfin"
    mysql_components_database: str = "tinkerfin_components"
    mysql_business_user: str = "tinkerfin"
    mysql_components_user: str = "tinkerfin_components"
    mysql_business_password: SecretStr | None = Field(default=None, repr=False)
    mysql_components_password: SecretStr | None = Field(default=None, repr=False)

    mysql_published_port: int | None = Field(default=None, ge=1, le=65535)
    redis_runtime_published_port: int | None = Field(default=None, ge=1, le=65535)
    open_sandbox_published_port: int | None = Field(default=None, ge=1, le=65535)
    s3_storage_published_port: int | None = Field(default=None, ge=1, le=65535)

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

    business_database_url: str = Field(
        default="",
        repr=False,
        description="业务库的异步 MySQL 连接地址；未指定时由 MySQL 配置生成",
    )
    components_database_url: str = Field(
        default="",
        repr=False,
        description="组件库的异步 MySQL 连接地址；未指定时由 MySQL 配置生成",
    )
    database_echo: bool = Field(default=False, description="是否输出 SQL 日志")
    database_pool_size: int = Field(
        default=5, ge=1, description="每个数据库池的常驻连接数"
    )
    database_max_overflow: int = Field(
        default=5, ge=0, description="每个数据库池的临时连接数"
    )
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
    def resolve_service_connections(self) -> Settings:
        """显式地址优先，本机默认连接发布端口；缺省数据库地址由独立账号生成"""
        if (
            "mysql_port" not in self.model_fields_set
            and self.mysql_published_port is not None
        ):
            self.mysql_port = self.mysql_published_port
        if (
            "redis_runtime_port" not in self.model_fields_set
            and self.redis_runtime_published_port is not None
        ):
            self.redis_runtime_port = self.redis_runtime_published_port
        if (
            "open_sandbox_domain" not in self.model_fields_set
            and self.open_sandbox_published_port is not None
        ):
            self.open_sandbox_domain = f"127.0.0.1:{self.open_sandbox_published_port}"
        if (
            "s3_storage_endpoint" not in self.model_fields_set
            and self.s3_storage_published_port is not None
        ):
            self.s3_storage_endpoint = (
                f"http://127.0.0.1:{self.s3_storage_published_port}"
            )
        for kind, database, user, password in (
            (
                "business",
                self.mysql_business_database,
                self.mysql_business_user,
                self.mysql_business_password,
            ),
            (
                "components",
                self.mysql_components_database,
                self.mysql_components_user,
                self.mysql_components_password,
            ),
        ):
            key = f"{kind}_database_url"
            if key in self.model_fields_set:
                continue
            if password is None or not password.get_secret_value():
                raise ValueError(
                    f"请配置 MYSQL_{kind.upper()}_PASSWORD 或 {key.upper()}"
                )
            setattr(
                self,
                key,
                URL.create(
                    "mysql+asyncmy",
                    username=user,
                    password=password.get_secret_value(),
                    host=self.mysql_host,
                    port=self.mysql_port,
                    database=database,
                    query={"charset": "utf8mb4"},
                ).render_as_string(hide_password=False),
            )
        return self

    @model_validator(mode="after")
    def validate_database_contract(self) -> Settings:
        """拒绝阻塞 Driver 与超出显式总连接预算的池配置"""

        for name, value in (
            ("BUSINESS_DATABASE_URL", self.business_database_url),
            ("COMPONENTS_DATABASE_URL", self.components_database_url),
        ):
            if not value:
                raise ValueError(f"{name} 不能为空")
        business = make_url(self.business_database_url)
        components = make_url(self.components_database_url)
        for name, url in (
            ("BUSINESS_DATABASE_URL", business),
            ("COMPONENTS_DATABASE_URL", components),
        ):
            if url.drivername != "mysql+asyncmy" or not url.database:
                raise ValueError(f"{name} 必须使用 mysql+asyncmy 驱动并指定数据库")
        if (business.host, business.port or 3306, business.database) == (
            components.host,
            components.port or 3306,
            components.database,
        ):
            raise ValueError("业务库与组件库必须使用不同数据库")
        required_connections = 2 * (
            self.database_pool_size + self.database_max_overflow
        )
        if required_connections > self.database_connection_budget:
            raise ValueError(
                "DATABASE_CONNECTION_BUDGET 必须覆盖两个池的 pool_size + max_overflow"
            )
        return self

    @property
    def business_database(self) -> DatabaseSettings:
        """返回用户、模型和会话等业务数据的连接配置"""

        return DatabaseSettings(
            url=self.business_database_url,
            echo=self.database_echo,
            pool_size=self.database_pool_size,
            max_overflow=self.database_max_overflow,
            pool_recycle=self.database_pool_recycle,
        )

    @property
    def components_database(self) -> DatabaseSettings:
        """返回长期记忆、轨迹、沙箱分配和自动化数据的连接配置"""

        return DatabaseSettings(
            url=self.components_database_url,
            echo=self.database_echo,
            pool_size=self.database_pool_size,
            max_overflow=self.database_max_overflow,
            pool_recycle=self.database_pool_recycle,
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

    return load_settings(env_file=os.environ.get("STUDIO_ENV_FILE", _DEFAULT_ENV_FILE))


def load_settings(*, env_file: str | Path | None = _DEFAULT_ENV_FILE) -> Settings:
    """读取配置，相对路径以指定配置文件或应用默认配置目录为基准"""

    # Pyright 按模型字段推导构造参数，未识别 BaseSettings 的环境输入和 _env_file
    settings = Settings(_env_file=env_file)  # type: ignore[reportCallIssue]
    config_directory = (
        Path(env_file).resolve().parent
        if env_file is not None
        else _DEFAULT_ENV_FILE.parent
    )
    settings.log_file_path = (config_directory / settings.log_file_path).resolve()
    return settings
