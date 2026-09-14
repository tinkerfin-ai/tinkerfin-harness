from pathlib import Path

import pytest

from tinkerfin_studio.config.settings import Settings, load_settings


def _clear_settings_environment(monkeypatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    monkeypatch.setenv("S3_STORAGE_BUCKET", "test-attachments")
    monkeypatch.setenv("S3_STORAGE_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_STORAGE_SECRET_KEY", "test-secret")


@pytest.mark.parametrize("bucket", [None, "", "ABucket", "a", "a..b", "127.0.0.1"])
def test_s3_storage_bucket_is_required_and_validated(monkeypatch, bucket):
    _clear_settings_environment(monkeypatch)
    monkeypatch.delenv("S3_STORAGE_BUCKET")
    values = {"database_url": "mysql+asyncmy://u:p@db/studio"}
    if bucket is not None:
        values["s3_storage_bucket"] = bucket
    with pytest.raises(ValueError, match="s3_storage_bucket"):
        Settings.model_validate(values)


def test_attachment_settings_separate_internal_and_public_endpoints(monkeypatch):
    _clear_settings_environment(monkeypatch)
    settings = Settings.model_validate(
        {
            "database_url": "mysql+asyncmy://u:p@db/studio",
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
                "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
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

    assert settings.database.url == ("mysql+asyncmy://studio:secret@db:3306/studio")
    assert settings.redis_runtime.host == "redis-runtime.internal"
    assert settings.redis_runtime.port == 6381
    assert settings.redis_runtime.database == 3
    assert settings.redis_runtime.checkpoint_database == 0
    assert settings.sandbox.domain == "127.0.0.1:8091"
    assert settings.sandbox.cpu == 2
    assert settings.sandbox.memory_mib == 2048
    assert settings.sandbox.warm_pool_size == 3
    assert settings.auth_token_expire_seconds == 86400
    assert settings.database.connection_budget == 20
    assert settings.database.management_connection_reserve == 10
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


def test_load_settings_rejects_non_async_mysql_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """请求链数据库必须使用 asyncmy，避免异步接口落入阻塞驱动"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=mysql+pymysql://studio:secret@db:3306/studio\n",
        encoding="utf-8",
    )

    try:
        load_settings(env_file=env_file)
    except ValueError as error:
        assert "mysql+asyncmy" in str(error)
    else:  # pragma: no cover - 失败分支用于给断言提供清晰原因
        raise AssertionError("同步 MySQL URL 不应通过配置校验")


def test_secret_files_override_plain_environment_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """容器 Secrets 文件必须优先于普通环境变量且不进入配置 repr"""

    _clear_settings_environment(monkeypatch)
    database_url_file = tmp_path / "database_url"
    redis_runtime_password_file = tmp_path / "redis_runtime_password"
    sandbox_key_file = tmp_path / "sandbox_key"
    tavily_key_file = tmp_path / "tavily_key"
    database_url_file.write_text(
        "mysql+asyncmy://studio:file-secret@mysql:3306/tinkerfin\n",
        encoding="utf-8",
    )
    redis_runtime_password_file.write_text("runtime-file-secret\n", encoding="utf-8")
    sandbox_key_file.write_text("sandbox-file-secret\n", encoding="utf-8")
    tavily_key_file.write_text("tavily-file-secret\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:plain@db:3306/tinkerfin",
                f"DATABASE_URL_FILE={database_url_file}",
                "REDIS_RUNTIME_PASSWORD=plain-runtime",
                f"REDIS_RUNTIME_PASSWORD_FILE={redis_runtime_password_file}",
                "OPEN_SANDBOX_API_KEY=plain-sandbox",
                f"OPEN_SANDBOX_API_KEY_FILE={sandbox_key_file}",
                "TAVILY_API_KEY=plain-tavily",
                f"TAVILY_API_KEY_FILE={tavily_key_file}",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.database.url == (
        "mysql+asyncmy://studio:file-secret@mysql:3306/tinkerfin"
    )
    assert settings.redis_runtime.password is not None
    assert settings.redis_runtime.password.get_secret_value() == "runtime-file-secret"
    assert settings.sandbox.api_key is not None
    assert settings.sandbox.api_key.get_secret_value() == "sandbox-file-secret"
    assert settings.tavily_api_key is not None
    assert settings.tavily_api_key.get_secret_value() == "tavily-file-secret"
    assert "file-secret" not in repr(settings)


def test_secret_file_rejects_missing_or_blank_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """配置的 Secret 文件缺失或为空时必须拒绝启动"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL_FILE=" + str(tmp_path / "missing"),
                "REDIS_RUNTIME_PASSWORD_FILE=" + str(tmp_path / "blank"),
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "blank").write_text("\n", encoding="utf-8")

    try:
        load_settings(env_file=env_file)
    except ValueError as error:
        assert "DATABASE_URL_FILE" in str(error)
    else:  # pragma: no cover - 失败分支用于提供清晰原因
        raise AssertionError("缺失的 Secret 文件不应通过配置校验")


def test_database_budget_must_cover_the_shared_pool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """总连接预算必须覆盖所有业务共用的数据库连接池"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "DATABASE_POOL_SIZE=10",
                "DATABASE_MAX_OVERFLOW=10",
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
        "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
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
            == (tmp_path / (configured or "logs/studio.log")).resolve()
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
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://studio:secret@db:3306/studio")
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
    with pytest.raises(ValueError, match="database_url"):
        load_settings(env_file=env_file if use_file else None)


def test_selected_env_file_uses_defaults_for_omitted_settings(tmp_path, monkeypatch):
    """指定配置未填写的日志字段使用默认值，不混入其他环境文件"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n")
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

    module_file = tmp_path / "src/isolated/config/settings.py"
    module_file.parent.mkdir(parents=True)
    shutil.copyfile(settings_module.__file__, module_file)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=mysql+asyncmy://default:unused@isolated/studio\n"
        "LOG_FILE_ENABLED=true\n"
    )
    selected = tmp_path / "selected.env"
    selected.write_text(
        "DATABASE_URL=mysql+asyncmy://selected:unused@isolated/studio\n"
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
    assert 'database_url' in str(error)
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
