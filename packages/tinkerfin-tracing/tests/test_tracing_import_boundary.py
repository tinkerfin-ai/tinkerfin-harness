"""Distribution and import boundaries for the protocol-neutral tracing package."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

import pytest


def test_tracing_import_loads_no_agent_protocol_or_infrastructure_packages() -> None:
    repository = Path(__file__).resolve().parents[3]
    script = """
import json
import sys
import tinkerfin_tracing

blocked = (
    'ag_ui', 'deepagents', 'langchain', 'langgraph', 'redis', 'sqlalchemy',
    'tinkerfin', 'tinkerfin_agui_adapter', 'tinkerfin_messaging',
    'tinkerfin_sandbox', 'tinkerfin_studio',
)
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)
)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_distribution_requires_only_protocol_neutral_packages() -> None:
    try:
        metadata = distribution("tinkerfin-tracing")
    except PackageNotFoundError:
        pytest.fail("tinkerfin-tracing distribution is not installed")

    requirements = set(metadata.requires or ())
    assert {item for item in requirements if "extra ==" not in item} == {
        "pydantic<3,>=2.12",
        "tinkerfin-contracts==0.1.0",
        "tinkerfin-notifications==0.1.0",
    }
    assert 'sqlalchemy[asyncio]==2.0.52; extra == "sqlalchemy"' in requirements
    assert 'tinkerfin-sqlalchemy==0.1.0; extra == "sqlalchemy"' in requirements
    assert not any(
        item.startswith(("aiosqlite", "asyncmy", "asyncpg")) for item in requirements
    )
    assert files("tinkerfin_tracing").joinpath("py.typed").is_file()


def test_missing_sql_extra_reports_the_sqlalchemy_install_choice() -> None:
    repository = Path(__file__).resolve().parents[3]
    script = """
import importlib.abc
import sys

class BlockSqlAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sqlalchemy' or fullname.startswith('sqlalchemy.'):
            raise ModuleNotFoundError("blocked for test", name=fullname)
        return None

sys.meta_path.insert(0, BlockSqlAlchemy())
import tinkerfin_tracing
try:
    tinkerfin_tracing.SqlAlchemyTraceStore
except ImportError as error:
    print(str(error))
else:
    raise AssertionError('SQL symbol unexpectedly loaded')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "tinkerfin-tracing[sqlalchemy]" in completed.stdout
