"""Cross-package diagnostic logging ownership and safety contracts."""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COMPONENT_ROOTS = {
    "agui",
    "automation",
    "contracts",
    "gateway",
    "langgraph_store",
    "messaging",
    "native_stream",
    "notifications",
    "runtime",
    "sandbox",
    "tracing",
}


def test_package_logger_calls_are_safe_by_construction() -> None:
    """Reject divergent roots, traceback logging, raw arguments, and unsafe extras."""

    for source in sorted((_ROOT / "packages").glob("*/src/**/*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "logging"
                and function.attr == "getLogger"
            ):
                assert len(node.args) == 1
                name = ast.literal_eval(node.args[0])
                assert isinstance(name, str)
                components = name.split(".")
                assert components[0] == "tinkerfin"
                assert len(components) >= 2
                assert components[1] in _COMPONENT_ROOTS
                continue
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "logger"
            ):
                continue
            assert function.attr != "exception"
            assert len(node.args) == 1
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            assert "exc_info" not in keywords
            extra = keywords.get("extra")
            if extra is None:
                continue
            assert isinstance(extra, ast.Dict)
            keys: list[object] = []
            for key in extra.keys:
                assert key is not None
                keys.append(ast.literal_eval(key))
            assert all(
                isinstance(key, str) and key.startswith("tinkerfin_") for key in keys
            )


def test_package_imports_do_not_change_process_logging_state() -> None:
    """Import logging components in a fresh process without global side effects."""

    script = """
import json
import logging

root = logging.getLogger()
before = {
    "level": root.level,
    "handlers": [id(value) for value in root.handlers],
    "filters": [id(value) for value in root.filters],
    "factory": id(logging.getLogRecordFactory()),
    "disable": logging.Logger.manager.disable,
}
import tinkerfin_messaging._producer_runtime
import tinkerfin_sandbox.lifecycle._manager_resources
import tinkerfin_langgraph_store.sqlalchemy
after = {
    "level": root.level,
    "handlers": [id(value) for value in root.handlers],
    "filters": [id(value) for value in root.filters],
    "factory": id(logging.getLogRecordFactory()),
    "disable": logging.Logger.manager.disable,
}
assert before == after
expected = (
    "tinkerfin.messaging.producer",
    "tinkerfin.sandbox.lifecycle",
    "tinkerfin.langgraph_store.store",
)
for name in expected:
    logger = logging.getLogger(name)
    assert logger.level == logging.NOTSET
    assert logger.handlers == []
    assert logger.propagate is True
for forbidden in (
    "tinkerfin_messaging",
    "tinkerfin_sandbox",
    "tinkerfin_agui_adapter",
    "tinkerfin_langgraph_store",
):
    assert not any(
        name == forbidden or name.startswith(forbidden + ".")
        for name in logging.Logger.manager.loggerDict
    )
print(json.dumps(before, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert isinstance(json.loads(completed.stdout), dict)


def test_parent_component_override_and_complete_silence() -> None:
    """Let the host control all component records through the canonical hierarchy."""

    names = (
        "tinkerfin",
        "tinkerfin.messaging",
        "tinkerfin.messaging.producer",
        "tinkerfin.sandbox",
        "tinkerfin.sandbox.lifecycle",
        "tinkerfin.langgraph_store",
        "tinkerfin.langgraph_store.store",
    )
    loggers = {name: logging.getLogger(name) for name in names}
    snapshots = {
        name: (logger.level, list(logger.handlers), logger.propagate, logger.disabled)
        for name, logger in loggers.items()
    }
    root = logging.getLogger()
    root_snapshot = (root.level, list(root.handlers), root.propagate, root.disabled)
    captured: list[logging.LogRecord] = []
    escaped_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def __init__(self, target: list[logging.LogRecord]) -> None:
            super().__init__()
            self._target = target

        def emit(self, record: logging.LogRecord) -> None:
            self._target.append(record)

    handler = _Capture(captured)
    escaped = _Capture(escaped_records)
    try:
        root.handlers = [escaped]
        root.setLevel(logging.DEBUG)
        root.disabled = False
        parent = loggers["tinkerfin"]
        component = loggers["tinkerfin.messaging"]
        child = loggers["tinkerfin.messaging.producer"]
        parent.handlers = [handler]
        parent.setLevel(logging.WARNING)
        parent.propagate = False
        parent.disabled = False
        for name in names[1:]:
            loggers[name].handlers = []
            loggers[name].setLevel(logging.NOTSET)
            loggers[name].propagate = True
            loggers[name].disabled = False
        component.setLevel(logging.DEBUG)

        child.debug("component probe")
        loggers["tinkerfin.sandbox.lifecycle"].warning("sandbox probe")
        loggers["tinkerfin.langgraph_store.store"].warning("mysql probe")
        assert [record.getMessage() for record in captured] == [
            "component probe",
            "sandbox probe",
            "mysql probe",
        ]
        assert escaped_records == []

        captured.clear()
        parent.setLevel(logging.CRITICAL + 1)
        for name in names[1:]:
            loggers[name].setLevel(logging.NOTSET)
            loggers[name].handlers = []
            loggers[name].propagate = True
        child.error("messaging silence probe")
        loggers["tinkerfin.sandbox.lifecycle"].error("sandbox silence probe")
        loggers["tinkerfin.langgraph_store.store"].error("mysql silence probe")
        assert captured == []
        assert escaped_records == []
    finally:
        for name, logger in loggers.items():
            level, handlers, propagate, disabled = snapshots[name]
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.disabled = disabled
        root.setLevel(root_snapshot[0])
        root.handlers = root_snapshot[1]
        root.propagate = root_snapshot[2]
        root.disabled = root_snapshot[3]
