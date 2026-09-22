import os
from pathlib import Path

import pytest

from tinkerfin_studio.config.settings import Settings, load_settings


def _clear_settings_environment(monkeypatch) -> None:
    for name in tuple(os.environ):
        if name.lower() in Settings.model_fields or name.upper().startswith(
            ("MYSQL_", "REDIS_RUNTIME_", "OPEN_SANDBOX_", "S3_STORAGE_")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_STORAGE_BUCKET", "test-attachments")
    monkeypatch.setenv("S3_STORAGE_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_STORAGE_SECRET_KEY", "test-secret")


def test_native_settings_source_priority(tmp_path, monkeypatch):
    """构造参数优先于环境变量，环境变量优先于文件，缺省项使用默认值"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BUSINESS_DATABASE_URL=mysql+asyncmy://u:p@db/business\n"
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\n"
        "LOG_LEVEL=WARNING\n"
    )
    assert load_settings(env_file=env_file).log_level == "WARNING"
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # Pyright 未识别 BaseSettings 的环境输入和 _env_file 参数
    settings = Settings(_env_file=env_file, log_level="ERROR")  # type: ignore[reportCallIssue]
    assert settings.log_level == "ERROR"
    assert settings.database_pool_size == 5
    assert load_settings(env_file=env_file).log_level == "DEBUG"


@pytest.mark.parametrize("bucket", [None, "", "ABucket", "a", "a..b", "127.0.0.1"])
def test_s3_storage_bucket_is_required_and_validated(monkeypatch, bucket):
    _clear_settings_environment(monkeypatch)
    monkeypatch.delenv("S3_STORAGE_BUCKET")
    values = {
        "s3_storage_access_key": "test-access",
        "s3_storage_secret_key": "test-secret",
        "business_database_url": "mysql+asyncmy://u:p@db/studio",
        "components_database_url": "mysql+asyncmy://u:p@db/components",
    }
    if bucket is not None:
        values["s3_storage_bucket"] = bucket
    with pytest.raises(ValueError, match="s3_storage_bucket"):
        Settings.model_validate(values)


def test_attachment_settings_separate_internal_and_public_endpoints(monkeypatch):
    _clear_settings_environment(monkeypatch)
    settings = Settings.model_validate(
        {
            "s3_storage_access_key": "test-access",
            "s3_storage_secret_key": "test-secret",
            "business_database_url": "mysql+asyncmy://u:p@db/studio",
            "components_database_url": "mysql+asyncmy://u:p@db/components",
            "s3_storage_bucket": "chosen-bucket",
            "s3_storage_endpoint": "http://minio:9000/",
            "s3_storage_public_endpoint": "https://files.example.com",
        }
    )
    assert settings.s3_storage.bucket == "chosen-bucket"
    assert settings.s3_storage.endpoint == "http://minio:9000"
    assert settings.s3_storage.public_endpoint == "https://files.example.com"
    assert "test-secret" not in repr(settings)


def test_load_settings_groups_external_resource_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """配置文件应生成可直接交给资源层的精确设置"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "REDIS_RUNTIME_HOST=redis-runtime.internal",
                "REDIS_RUNTIME_PORT=6381",
                "REDIS_RUNTIME_PASSWORD=runtime-secret",
                "REDIS_RUNTIME_DB=3",
                "REDIS_RUNTIME_CHECKPOINT_DB=0",
                "OPEN_SANDBOX_DOMAIN=127.0.0.1:8091",
                "OPEN_SANDBOX_PROTOCOL=http",
                "OPEN_SANDBOX_CPU=2",
                "OPEN_SANDBOX_MEMORY_MIB=2048",
                "OPEN_SANDBOX_WARM_POOL_SIZE=3",
                "AUTH_TOKEN_EXPIRE_SECONDS=86400",
                "TAVILY_API_KEY=tavily-secret",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.business_database.url == (
        "mysql+asyncmy://studio:secret@db:3306/studio"
    )
    assert settings.redis_runtime.host == "redis-runtime.internal"
    assert settings.redis_runtime.port == 6381
    assert settings.redis_runtime.database == 3
    assert settings.redis_runtime.checkpoint_database == 0
    assert settings.sandbox.domain == "127.0.0.1:8091"
    assert settings.sandbox.cpu == 2
    assert settings.sandbox.memory_mib == 2048
    assert settings.sandbox.warm_pool_size == 3
    assert settings.auth_token_expire_seconds == 86400
    assert settings.database_connection_budget == 20
    assert settings.database_management_connection_reserve == 10
    assert settings.tavily_api_key is not None
    assert "runtime-secret" not in repr(settings)
    assert "tavily-secret" not in repr(settings)
    monkeypatch.setenv("OPEN_SANDBOX_CPU", "0.5")
    monkeypatch.setenv("OPEN_SANDBOX_MEMORY_MIB", "512")
    overridden = load_settings(env_file=env_file).sandbox
    assert overridden.cpu == 0.5
    assert overridden.memory_mib == 512
    monkeypatch.setenv("OPEN_SANDBOX_CPU", "nan")
    with pytest.raises(ValueError, match="open_sandbox_cpu"):
        load_settings(env_file=env_file)
    monkeypatch.setenv("OPEN_SANDBOX_CPU", "0")
    with pytest.raises(ValueError, match="open_sandbox_cpu"):
        load_settings(env_file=env_file)
    monkeypatch.setenv("OPEN_SANDBOX_CPU", "1")
    monkeypatch.setenv("OPEN_SANDBOX_MEMORY_MIB", "0")
    with pytest.raises(ValueError, match="open_sandbox_memory_mib"):
        load_settings(env_file=env_file)


def test_shared_configuration_uses_published_ports_and_explicit_addresses(
    tmp_path, monkeypatch
):
    """共享配置默认连接本机发布端口，明确指定的外部地址优先"""
    from sqlalchemy.engine import make_url

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MYSQL_BUSINESS_PASSWORD=business-secret\nMYSQL_COMPONENTS_PASSWORD=components-secret\n"
        "MYSQL_PUBLISHED_PORT=23306\nREDIS_RUNTIME_PUBLISHED_PORT=26381\n"
        "OPEN_SANDBOX_PUBLISHED_PORT=28091\nS3_STORAGE_PUBLISHED_PORT=29000\n"
    )
    local = load_settings(env_file=env_file)
    assert make_url(local.business_database_url).port == 23306
    assert make_url(local.components_database_url).port == 23306
    assert local.redis_runtime.port == 26381
    assert local.sandbox.domain == "127.0.0.1:28091"
    assert local.s3_storage.endpoint == "http://127.0.0.1:29000"

    for key, value in {
        "MYSQL_HOST": "db.example.com",
        "MYSQL_PORT": "3307",
        "REDIS_RUNTIME_HOST": "redis.example.com",
        "REDIS_RUNTIME_PORT": "6380",
        "OPEN_SANDBOX_DOMAIN": "sandbox.example.com:8090",
        "S3_STORAGE_ENDPOINT": "https://storage.example.com",
    }.items():
        monkeypatch.setenv(key, value)
    external = load_settings(env_file=env_file)
    url = make_url(external.business_database_url)
    assert (url.host, url.port) == ("db.example.com", 3307)
    assert (external.redis_runtime.host, external.redis_runtime.port) == (
        "redis.example.com",
        6380,
    )
    assert external.sandbox.domain == "sandbox.example.com:8090"
    assert external.s3_storage.endpoint == "https://storage.example.com"


def test_load_settings_rejects_non_async_mysql_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """请求链数据库必须使用 asyncmy，避免异步接口落入阻塞驱动"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+pymysql://studio:secret@db:3306/studio\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"mysql\+asyncmy"):
        load_settings(env_file=env_file)


def test_database_budget_must_cover_both_pools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """总连接预算必须覆盖业务和组件两个连接池"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "DATABASE_POOL_SIZE=5",
                "DATABASE_MAX_OVERFLOW=5",
                "DATABASE_CONNECTION_BUDGET=19",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"pool_size \+ max_overflow"):
        load_settings(env_file=env_file)


@pytest.mark.parametrize("configured", ["logs/studio.log", "../logs/app.log", None])
def test_logging_settings_load_dotenv_and_resolve_paths(
    tmp_path, monkeypatch, configured
):
    """日志配置读取同一份环境文件，路径不随工作目录变化"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    content = (
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
        "LOG_LEVEL=DEBUG\nLOG_FILE_ENABLED=true\n"
        "LOG_FILE_MAX_BYTES=2048\nLOG_FILE_BACKUP_COUNT=2\n"
    )
    if configured:
        content += f"LOG_FILE_PATH={configured}\n"
    env_file.write_text(content)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for cwd in (tmp_path, elsewhere):
        monkeypatch.chdir(cwd)
        settings = load_settings(env_file=env_file)
        assert settings.log_level == "DEBUG"
        assert settings.log_file_enabled
        assert settings.log_file_max_bytes == 2048
        assert settings.log_file_backup_count == 2
        assert (
            settings.log_file_path
            == (tmp_path / (configured or "server/logs/studio.log")).resolve()
        )
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("LOG_FILE_ENABLED", "false")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "volume" / "app.log"))
    settings = load_settings(env_file=env_file)
    assert settings.log_level == "ERROR"
    assert not settings.log_file_enabled
    assert settings.log_file_path == tmp_path / "volume" / "app.log"


def test_logging_defaults_and_validation(tmp_path, monkeypatch):
    """文件日志默认关闭，滚动阈值为 50 MiB，错误配置阻止启动"""
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv(
        "BUSINESS_DATABASE_URL", "mysql+asyncmy://studio:secret@db:3306/studio"
    )
    monkeypatch.setenv("COMPONENTS_DATABASE_URL", "mysql+asyncmy://u:p@db/components")
    settings = load_settings(env_file=None)
    assert settings.log_level == "INFO"
    assert not settings.log_file_enabled
    assert settings.log_file_max_bytes == 50 * 1024 * 1024
    assert settings.log_file_backup_count == 3
    assert settings.log_file_path.is_absolute()
    monkeypatch.chdir(tmp_path)
    assert load_settings(env_file=None).log_file_path == settings.log_file_path
    for key, value in (
        ("LOG_LEVEL", "INVALID"),
        ("LOG_FILE_MAX_BYTES", "0"),
        ("LOG_FILE_BACKUP_COUNT", "0"),
    ):
        monkeypatch.setenv(key, value)
        with pytest.raises(ValueError):
            load_settings(env_file=None)
        monkeypatch.delenv(key)


@pytest.mark.parametrize("use_file", [False, True])
def test_load_settings_requires_database_from_selected_source(
    tmp_path, monkeypatch, use_file
):
    """缺失的必填配置不能从另一个本地环境文件补入"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=WARNING\n")
    with pytest.raises(ValueError, match="MYSQL_BUSINESS_PASSWORD"):
        load_settings(env_file=env_file if use_file else None)


def test_selected_env_file_uses_defaults_for_omitted_settings(tmp_path, monkeypatch):
    """指定配置未填写的日志字段使用默认值，不混入其他环境文件"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
    )
    settings = load_settings(env_file=env_file)
    assert not settings.log_file_enabled
    assert settings.log_level == "INFO"
    assert settings.s3_storage_bucket == "test-attachments"
    assert settings.sandbox.cpu == 1
    assert settings.sandbox.memory_mib == 1024
    assert settings.sandbox.warm_pool_size == 0


def test_dotenv_sources_are_isolated_from_an_existing_default_file(tmp_path):
    """隔离安装目录内即使存在默认配置，选定文件和禁用文件读取仍保持独立"""
    import os
    import shutil
    import subprocess
    import sys

    import tinkerfin_studio.config.settings as settings_module

    module_file = tmp_path / "server/src/isolated/config/settings.py"
    module_file.parent.mkdir(parents=True)
    shutil.copyfile(settings_module.__file__, module_file)
    (tmp_path / ".env").write_text(
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://default:unused@isolated/studio\n"
        "LOG_FILE_ENABLED=true\n"
    )
    selected = tmp_path / "selected.env"
    selected.write_text(
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://selected:unused@isolated/studio\n"
    )
    program = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('isolated_settings', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.load_settings().log_file_enabled
assert not module.load_settings(env_file=sys.argv[2]).log_file_enabled
try:
    module.load_settings(env_file=None)
except ValueError as error:
    assert 'MYSQL_BUSINESS_PASSWORD' in str(error)
else:
    raise AssertionError('disabled dotenv read inherited the default database')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(module_file), str(selected)],
        env={
            "PATH": os.defpath,
            "S3_STORAGE_BUCKET": "test-attachments",
            "S3_STORAGE_ACCESS_KEY": "test-access",
            "S3_STORAGE_SECRET_KEY": "test-secret",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "missing", ["business_database_url", "components_database_url"]
)
@pytest.mark.parametrize("empty", [False, True])
def test_each_database_is_required(monkeypatch, missing, empty):
    _clear_settings_environment(monkeypatch)
    values = {
        "s3_storage_bucket": "test-attachments",
        "s3_storage_access_key": "test-access",
        "s3_storage_secret_key": "test-secret",
        "business_database_url": "mysql+asyncmy://u:p@db/business",
        "components_database_url": "mysql+asyncmy://u:p@db/components",
    }
    if empty:
        values[missing] = ""
    else:
        values.pop(missing)
    with pytest.raises(ValueError, match=missing.upper()):
        Settings.model_validate(values)


def test_databases_cannot_share_a_schema(monkeypatch):
    _clear_settings_environment(monkeypatch)
    with pytest.raises(ValueError, match="不同数据库"):
        Settings.model_validate(
            {
                "s3_storage_bucket": "test-attachments",
                "s3_storage_access_key": "test-access",
                "s3_storage_secret_key": "test-secret",
                "business_database_url": "mysql+asyncmy://business:p@db/studio",
                "components_database_url": "mysql+asyncmy://components:p@db:3306/studio",
            }
        )
