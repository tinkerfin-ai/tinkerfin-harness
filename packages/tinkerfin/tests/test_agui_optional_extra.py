"""Core and AG-UI extra installation boundaries."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def test_core_and_plan_import_without_agui_and_entrypoint_error_is_precise() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = r"""
import importlib.abc
import sys


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname == "ag_ui"
            or fullname.startswith("ag_ui.")
            or fullname == "tinkerfin_agui_adapter"
            or fullname.startswith("tinkerfin_agui_adapter.")
        ):
            error = ModuleNotFoundError(f"blocked {fullname}")
            error.name = fullname.split(".")[0]
            raise error
        return None


sys.meta_path.insert(0, Blocker())
import tinkerfin

configured = tinkerfin.TinkerFin().with_namespace("test").with_plan(default_mode="plan")
definition = configured.build(model="provider:model", tools=[])
assert definition
assert not any(
    name == "ag_ui"
    or name.startswith("ag_ui.")
    or name == "tinkerfin_agui_adapter"
    or name.startswith("tinkerfin_agui_adapter.")
    for name in sys.modules
)

for operation in (
    lambda: tinkerfin.AgUiResumeBinding,
    lambda: definition.agui.history(None),
    lambda: definition.open_agui_run(thread_id="thread-core", run_id="run-core", input={"messages": []}),
):
    try:
        operation()
    except ModuleNotFoundError as error:
        assert 'pip install "tinkerfin[agui]"' in str(error)
    else:
        raise AssertionError("AG-UI entrypoint unexpectedly loaded without its extra")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("blocked", ["tinkerfin_tracing", "tinkerfin_agui_adapter"])
def test_history_optional_imports_are_lazy_and_report_missing_extra(
    blocked: str,
) -> None:
    script = r"""
import importlib.abc
import sys

blocked = sys.argv[1]
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == blocked or fullname.startswith(blocked + "."):
            raise ModuleNotFoundError("blocked " + fullname, name=blocked)
        return None

sys.meta_path.insert(0, Blocker())
from tinkerfin import TinkerFin
import tinkerfin.agui as agui
runtime = TinkerFin().with_namespace("test").build(model="provider:model")
assert blocked not in sys.modules
if blocked == "tinkerfin_tracing":
    assert runtime.agui
    assert blocked not in sys.modules
for operation in (lambda: agui.AgUiHistory, lambda: runtime.agui.history(None)):
    try:
        operation()
    except ModuleNotFoundError as error:
        command = 'pip install "tinkerfin[agui,tracing]"' if blocked == "tinkerfin_tracing" else 'pip install "tinkerfin[agui]"'
        assert command in str(error), str(error)
        assert error.name == blocked
    else:
        raise AssertionError("History loaded without its dependency")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, blocked],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_distribution_metadata_declares_current_optional_dependency_graph() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    tinkerfin_project = tomllib.loads(
        (repository_root / "packages/tinkerfin/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    messaging_project = tomllib.loads(
        (repository_root / "packages/tinkerfin-messaging/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    studio_project = tomllib.loads(
        (repository_root / "apps/studio/server/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    tracing_project = tomllib.loads(
        (repository_root / "packages/tinkerfin-tracing/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]

    core_dependencies = tuple(tinkerfin_project["dependencies"])
    assert not any(value.startswith("ag-ui-protocol") for value in core_dependencies)
    assert not any(
        value.startswith("tinkerfin-agui-adapter") for value in core_dependencies
    )
    assert tinkerfin_project["optional-dependencies"]["agui"] == [
        "ag-ui-protocol==0.1.19",
        "tinkerfin-agui-adapter==0.1.0",
    ]
    assert messaging_project["optional-dependencies"]["native"] == [
        "tinkerfin-native-stream==0.1.0"
    ]
    assert tinkerfin_project["optional-dependencies"]["tracing"] == [
        "tinkerfin-tracing==0.1.0"
    ]
    tracing_dependencies = [
        *tracing_project["dependencies"],
        *(
            dependency
            for dependencies in tracing_project["optional-dependencies"].values()
            for dependency in dependencies
        ),
    ]
    assert not any(
        dependency.startswith(
            ("tinkerfin-agui-adapter", "ag-ui-protocol", "tinkerfin==")
        )
        for dependency in tracing_dependencies
    )
    assert "tinkerfin[agui,redis,tracing]==0.1.0" in studio_project["dependencies"]
