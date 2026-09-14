"""沙箱事件日志的等级、字段与宿主输出约束"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tinkerfin_sandbox import (
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleEventType,
    OpenSandboxLifecycleReason,
)
from tinkerfin_studio.infrastructure.sandbox_events import SandboxEventLogger

_LOGGER = "tinkerfin_studio.infrastructure.sandbox_events"


@pytest.mark.parametrize("kind", tuple(OpenSandboxLifecycleEventType))
async def test_event_log_preserves_safe_fields_and_severity(
    caplog: pytest.LogCaptureFixture, kind: OpenSandboxLifecycleEventType
) -> None:
    """所有事件输出可关联的单条元数据，故障等级与宿主约定一致"""

    event = OpenSandboxLifecycleEvent(
        event_id="event-123",
        type=kind,
        owner_key=None if kind.value.startswith("warm_") else "users/17",
        occurred_at=datetime(2026, 9, 6, tzinfo=UTC),
        reason=OpenSandboxLifecycleReason.NOT_FOUND,
        workspace_may_have_changed=True,
        diagnostic_context={"sandbox_id": "PRIVATE_REMOTE_ID", "cause": "SECRET"},
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await SandboxEventLogger().on_sandbox_event(event)
    records = [record for record in caplog.records if record.name == _LOGGER]
    assert len(records) == 1
    record = records[0]
    expected_level = (
        logging.ERROR
        if kind is OpenSandboxLifecycleEventType.RECOVERY_FAILED
        else logging.WARNING
        if kind
        in {
            OpenSandboxLifecycleEventType.UNAVAILABLE,
            OpenSandboxLifecycleEventType.WARM_CAPACITY_DEGRADED,
        }
        else logging.INFO
    )
    assert record.levelno == expected_level
    payload = json.loads(record.getMessage().removeprefix("沙箱生命周期 "))
    assert payload == {
        "event": kind.value,
        "reason": "not_found",
        "event_id": "event-123",
        "owner_key": event.owner_key,
        "occurred_at": "2026-09-06T00:00:00+00:00",
        "workspace_may_have_changed": True,
    }
    assert "PRIVATE_REMOTE_ID" not in record.getMessage()
    assert "SECRET" not in record.getMessage()


async def test_event_log_bounds_and_escapes_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常身份不能注入额外日志行或无限扩大一条记录"""

    event = OpenSandboxLifecycleEvent(
        event_id="id\r\n\u2028" * 100,
        type=OpenSandboxLifecycleEventType.RECOVERED,
        owner_key="用户\r\n\u2028" * 1000,
        occurred_at=datetime(2026, 9, 6, tzinfo=UTC),
        reason=OpenSandboxLifecycleReason.CONNECTION_RESTORED,
        workspace_may_have_changed=False,
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await SandboxEventLogger().on_sandbox_event(event)
    record = next(record for record in caplog.records if record.name == _LOGGER)
    message = record.getMessage()
    assert len(message.splitlines()) == 1
    assert len(message.encode()) < 2000
    payload = json.loads(message.removeprefix("沙箱生命周期 "))
    assert payload["event_id"] == event.event_id[:64]
    assert event.owner_key is not None
    assert payload["owner_key"] == event.owner_key[:128]


@pytest.mark.parametrize(
    ("level", "file_enabled"),
    [("INFO", False), ("WARNING", True), ("CRITICAL", True)],
)
def test_sandbox_events_use_existing_host_outputs_once(
    tmp_path: Path, level: str, file_enabled: bool
) -> None:
    """默认与滚动文件输出各记录一次，事件服从宿主等级且导入不接管日志"""

    program = """
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from tinkerfin_sandbox import (
    OpenSandboxLifecycleEvent, OpenSandboxLifecycleEventType,
    OpenSandboxLifecycleReason,
)
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings
settings = Settings(
    _env_file=None, database_url="mysql+asyncmy://studio:secret@db:3306/studio",
    log_level=sys.argv[1], log_file_enabled=json.loads(sys.argv[2]),
    log_file_path=Path("runtime/studio.log").resolve(),
)
async def emit_events():
    from tinkerfin_studio.infrastructure.sandbox_events import SandboxEventLogger
    observer = SandboxEventLogger()
    for kind in (
        OpenSandboxLifecycleEventType.RECOVERED,
        OpenSandboxLifecycleEventType.UNAVAILABLE,
        OpenSandboxLifecycleEventType.RECOVERY_FAILED,
    ):
        await observer.on_sandbox_event(OpenSandboxLifecycleEvent(
            event_id='host-' + kind.value, type=kind, owner_key='users/1',
            occurred_at=datetime(2026,9,6,tzinfo=UTC),
            reason=OpenSandboxLifecycleReason.TIMEOUT,
            workspace_may_have_changed=False,
            diagnostic_context={'secret': 'PRIVATE_DIAGNOSTIC'},
        ))
async def main():
    async with setup_logging(settings):
        logging.getLogger('studio.host').error('host_before_import')
        await emit_events()
        logging.getLogger('studio.host').error('host_after_import')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, level, json.dumps(file_enabled)],
        cwd=tmp_path,
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
    outputs = [result.stdout]
    log_file = tmp_path / "runtime/studio.log"
    if file_enabled:
        outputs.append(log_file.read_text())
    else:
        assert not log_file.exists()
    threshold = getattr(logging, level)
    for output in outputs:
        for event, severity in (
            ("recovered", logging.INFO),
            ("unavailable", logging.WARNING),
            ("recovery_failed", logging.ERROR),
        ):
            assert output.count(f'"event":"{event}"') == int(severity >= threshold)
        assert output.count("host_before_import") == int(logging.ERROR >= threshold)
        assert output.count("host_after_import") == int(logging.ERROR >= threshold)
        assert "PRIVATE_DIAGNOSTIC" not in output
