import os

import pytest

from tinkerfin_studio.config.settings import Settings


def _clear_settings_environment(monkeypatch) -> None:
    for name in tuple(os.environ):
        if name.lower() in Settings.model_fields or name.upper().startswith(
            ("MYSQL_", "REDIS_RUNTIME_", "OPEN_SANDBOX_", "S3_STORAGE_")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_STORAGE_BUCKET", "test-attachments")
    monkeypatch.setenv("S3_STORAGE_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_STORAGE_SECRET_KEY", "test-secret")


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
