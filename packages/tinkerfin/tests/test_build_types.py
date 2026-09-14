"""Packaged public APIs and workspace annotations retain their types."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from tinkerfin import AgentRuntime, TinkerFin

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]


def test_runtime_is_reexported_as_the_actual_execution_type() -> None:
    import tinkerfin.runtime as implementation

    assert AgentRuntime is implementation.AgentRuntime
    assert not issubclass(AgentRuntime, TinkerFin)
    assert not issubclass(TinkerFin, AgentRuntime)


def test_removed_and_third_party_exports_are_not_advertised(tmp_path: Path) -> None:
    import tinkerfin
    import tinkerfin.deep_agent as graph_module

    for name in (
        "DeepAgentState",
        "create_deep_agent",
        "DeepAgentDefinition",
        "DeepAgentRuntime",
    ):
        assert not hasattr(tinkerfin, name)
    assert not hasattr(graph_module, "create_deep_agent")
    fixture = tmp_path / "removed_exports.py"
    fixture.write_text(
        "from tinkerfin import DeepAgentState, create_deep_agent, DeepAgentDefinition\n"
        "from tinkerfin.deep_agent import create_deep_agent as module_factory\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("pyright")),
            "--pythonpath",
            sys.executable,
            "--outputjson",
            str(fixture),
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert report["summary"]["errorCount"] == 4
    assert all(
        item["rule"] == "reportAttributeAccessIssue"
        for item in report["generalDiagnostics"]
    )


def test_workspace_build_types_are_complete_for_strict_callers(tmp_path: Path) -> None:
    fixture = tmp_path / "workspace_contract.py"
    shutil.copy2(_PACKAGE_ROOT / "tests/typecheck_runtime_workspace.py", fixture)
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": [fixture.name],
                "pythonVersion": "3.11",
                "typeCheckingMode": "strict",
                "reportMissingTypeStubs": "none",
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("pyright")),
            "--pythonpath",
            sys.executable,
            "--project",
            str(config),
            "--outputjson",
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["summary"]["filesAnalyzed"] == 1
    assert completed.returncode == 0 and report["summary"]["errorCount"] == 0, report
