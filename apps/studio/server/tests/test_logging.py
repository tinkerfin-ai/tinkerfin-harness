"""Studio 全局日志初始化测试"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("file_enabled", (False, True))
def test_logging_outputs_and_cleanup(tmp_path: Path, file_enabled: bool) -> None:
    """控制台始终输出，可选文件写入后随生命周期退出刷新关闭"""
    program = """
import asyncio
import logging
import sys
from pathlib import Path
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import load_settings
async def main():
    async with setup_logging(load_settings(env_file=Path('config.env'))):
        logging.getLogger('studio.test').warning('双通道日志')
asyncio.run(main())
"""
    (tmp_path / "config.env").write_text(
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://u:p@db/components\nBUSINESS_DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
        f"LOG_FILE_ENABLED={str(file_enabled).lower()}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
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
    assert result.stdout.count("双通道日志") == 1
    if file_enabled:
        assert (tmp_path / "server/logs/studio.log").read_text().count(
            "双通道日志"
        ) == 1
    else:
        assert not (tmp_path / "server/logs").exists()


@pytest.mark.parametrize(
    ("echo", "file_enabled", "level"),
    [
        (False, False, "DEBUG"),
        (False, True, "DEBUG"),
        (True, False, "ERROR"),
        (True, True, "ERROR"),
    ],
)
def test_database_sql_uses_host_handlers_once_per_output(
    tmp_path: Path, echo: bool, file_enabled: bool, level: str
) -> None:
    """SQL 开关独立控制可见性，每个宿主输出各记录一次且不影响执行"""

    program = """
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings
from tinkerfin_studio.infrastructure.database import Database

settings = Settings(
    s3_storage_bucket="test-attachments", s3_storage_access_key="test-access", s3_storage_secret_key="test-secret",
    business_database_url="mysql+asyncmy://studio:secret@db:3306/studio", components_database_url="mysql+asyncmy://u:p@db/components",
    log_level=sys.argv[3],
    log_file_enabled=json.loads(sys.argv[2]),
    log_file_path=Path("runtime/sql.log").resolve(),
)
executed = []
values = []
failures = []

def record_statement(connection, cursor, statement, parameters, context, many):
    executed.append(statement)

async def main():
    for _ in range(2):
        async with setup_logging(settings), Database(
            "sqlite+aiosqlite:///isolated.db", echo=json.loads(sys.argv[1])
        ) as database:
            event.listen(
                database.engine.sync_engine, "before_cursor_execute", record_statement
            )
            async with database.engine.connect() as connection:
                values.append(await connection.scalar(
                    text("SELECT :value AS logged_value"), {"value": 271828}
                ))
                try:
                    await connection.execute(text("SELECT * FROM missing_logging_table"))
                except SQLAlchemyError as error:
                    failures.append(type(error).__name__)
                    logging.getLogger("studio.test").error("query_failure_delivered")

asyncio.run(main())
print(json.dumps({"executed": executed, "values": values, "failures": failures}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            json.dumps(echo),
            json.dumps(file_enabled),
            level,
        ],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "S3_STORAGE_BUCKET": "test-attachments",
            "S3_STORAGE_ACCESS_KEY": "test-access",
            "S3_STORAGE_SECRET_KEY": "test-secret",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    statements = ("SELECT ? AS logged_value", "SELECT * FROM missing_logging_table")
    assert observed["values"] == [271828, 271828]
    assert observed["failures"] == ["OperationalError", "OperationalError"]
    assert observed["executed"] == list(statements) * 2
    outputs = [result.stdout]
    log_file = tmp_path / "runtime/sql.log"
    if file_enabled:
        outputs.append(log_file.read_text(encoding="utf-8"))
    else:
        assert not log_file.exists()
    for output in outputs:
        lines = output.splitlines()
        for statement in statements:
            sql_lines = [line for line in lines if line.endswith(statement)]
            assert len(sql_lines) == (2 if echo else 0)
            assert all("[INFO] [sqlalchemy.engine.Engine" in line for line in sql_lines)
        parameter_lines = [line for line in lines if line.endswith("(271828,)")]
        assert parameter_lines == []
        assert (
            "SQL parameters hidden" in output
            if echo
            else "SQL parameters hidden" not in output
        )
        assert sum(line.endswith("query_failure_delivered") for line in lines) == 2


def test_application_startup_failure_is_logged_before_cleanup(tmp_path):
    """应用资源初始化失败时，文件保留启动日志与原始错误原因"""
    program = r"""
import asyncio
import logging
import threading
from pathlib import Path
from fastapi import FastAPI
import tinkerfin_studio.resources as resources
from tinkerfin_studio.config.settings import Settings

initial_threads = set(threading.enumerate())
settings = Settings(
    s3_storage_bucket="test-attachments", s3_storage_access_key="test-access", s3_storage_secret_key="test-secret",
    business_database_url="mysql+asyncmy://studio:secret@db:3306/studio", components_database_url="mysql+asyncmy://u:p@db/components",
    log_file_enabled=True, log_file_path=Path('studio.log').resolve(),
)
resources.get_settings = lambda: settings
def fail_database(*args, **kwargs):
    raise RuntimeError("isolated_startup_failure")
resources.Database = fail_database
async def main():
    try:
        async with resources.build_lifespan()(FastAPI()):
            raise AssertionError("initialization must fail")
    except RuntimeError as error:
        assert str(error) == "isolated_startup_failure"
asyncio.run(main())
for thread in set(threading.enumerate()) - initial_threads:
    thread.join()
assert set(threading.enumerate()) <= initial_threads
logging.warning('console-after-cleanup')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
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
    content = (tmp_path / "studio.log").read_text()
    assert "服务器已加载" in content
    assert "服务生命周期失败" in content
    assert "RuntimeError: isolated_startup_failure" in content
    assert result.stdout.count("console-after-cleanup") == 1
    assert "console-after-cleanup" not in content


def test_file_logging_rejects_unwritable_destination(tmp_path):
    """无效的文件目录阻止初始化，且不遗留监听线程"""
    program = r"""
import asyncio
import threading
from pathlib import Path
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings

initial_threads = set(threading.enumerate())
blocker = Path('not-a-directory').resolve()
blocker.write_text('occupied')
settings = Settings(
    s3_storage_bucket="test-attachments", s3_storage_access_key="test-access", s3_storage_secret_key="test-secret",
    business_database_url="mysql+asyncmy://studio:secret@db:3306/studio", components_database_url="mysql+asyncmy://u:p@db/components",
    log_file_enabled=True, log_file_path=blocker / 'studio.log',
)
async def main():
    try:
        async with setup_logging(settings):
            raise AssertionError('unwritable directory must fail')
    except OSError:
        pass
asyncio.run(main())
for thread in set(threading.enumerate()) - initial_threads:
    thread.join()
assert set(threading.enumerate()) <= initial_threads
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
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


@pytest.mark.parametrize("phase", ("opening", "draining"))
@pytest.mark.parametrize("close_fails", (False, True))
def test_cancellation_finishes_logging_cleanup_even_when_close_fails(
    tmp_path, phase, close_fails
):
    """初始化和排空期间的重复取消均完成清理，关闭错误不能覆盖取消"""
    program = r"""
import asyncio
import logging
import sys
import threading
from pathlib import Path
from concurrent_log_handler import ConcurrentRotatingFileHandler
import tinkerfin_studio.config.logging as config
from tinkerfin_studio.config.settings import Settings

initial_threads = set(threading.enumerate())
phase, close_fails = sys.argv[1], sys.argv[2] == 'true'
release, finish_close, closed = threading.Event(), threading.Event(), threading.Event()

class CheckedHandler(ConcurrentRotatingFileHandler):
    failed = False
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if phase == 'opening':
            loop.call_soon_threadsafe(entered.set)
            release.wait()
    def emit(self, record):
        if phase == 'draining':
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        super().emit(record)
    def close(self):
        if not closed.is_set():
            loop.call_soon_threadsafe(closing.set)
            finish_close.wait()
        super().close()
        closed.set()
        if close_fails and not self.failed:
            self.failed = True
            raise OSError('isolated-close-failure')

config.ConcurrentRotatingFileHandler = CheckedHandler
settings = Settings(
    s3_storage_bucket="test-attachments", s3_storage_access_key="test-access", s3_storage_secret_key="test-secret",
    business_database_url='mysql+asyncmy://u:p@db/studio', components_database_url="mysql+asyncmy://u:p@db/components",
    log_file_enabled=True, log_file_path=Path('studio.log').resolve(),
)
async def run():
    async with config.setup_logging(settings):
        logging.warning('accepted-record')
        await asyncio.Event().wait()

async def main():
    global loop, entered, closing
    loop = asyncio.get_running_loop()
    entered, closing = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(run())
    try:
        await entered.wait()
        task.cancel()
        release.set()
        await closing.wait()
        assert not task.done()
        task.cancel()
    finally:
        release.set()
        finish_close.set()
    try:
        await task
        raise AssertionError('cancellation was lost')
    except asyncio.CancelledError as error:
        if close_fails:
            assert isinstance(error.__cause__, OSError)
            assert str(error.__cause__) == 'isolated-close-failure'
    assert closed.is_set()
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
asyncio.run(main())
for thread in set(threading.enumerate()) - initial_threads:
    thread.join()
assert set(threading.enumerate()) <= initial_threads
if phase == 'draining':
    assert Path('studio.log').read_text().count('accepted-record') == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", program, phase, str(close_fails).lower()],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
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
    assert "never retrieved" not in result.stderr
