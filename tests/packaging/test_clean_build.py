"""Exercise the repository build command against owned, contaminated projects."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.packaging_e2e


def _workspace(tmp_path: Path, *projects: str) -> Path:
    root = tmp_path / "workspace"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_ROOT / "scripts/build_wheels.py", root / "scripts/build_wheels.py")
    for name in projects:
        source = _ROOT / "packages" / name
        target = root / "packages" / name
        target.mkdir(parents=True)
        for filename in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(source / filename, target / filename)
        shutil.copytree(
            source / "src",
            target / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".DS_Store"),
        )
    return root


def _build(root: Path, *projects: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(root / "scripts/build_wheels.py"),
            "--offline",
            "--out-dir",
            str(root / "dist"),
            *(f"packages/{project}" for project in projects),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_clean_build_keeps_local_source_changes_and_preserves_artifacts(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, "tinkerfin-contracts")
    project = root / "packages/tinkerfin-contracts"
    package = project / "src/tinkerfin_contracts"
    source = package / "local_module.py"
    source.write_text("CURRENT = True\n", encoding="utf-8")
    (package / "local_module.pyi").write_text("CURRENT: bool\n", encoding="utf-8")
    (package / "secrets").mkdir()
    (package / "secrets/__init__.py").write_text("LOCAL = True\n", encoding="utf-8")
    stale = project / "build/lib/tinkerfin_contracts"
    stale.mkdir(parents=True)
    stale_source = stale / "local_module.py"
    stale_source.write_text("CURRENT = False\n", encoding="utf-8")
    future = source.stat().st_mtime + 3600
    os.utime(stale_source, (future, future))
    (stale / "deleted_module.py").write_text("DELETED = True\n", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cached_module.py").write_text("CACHED = True\n", encoding="utf-8")
    metadata = project / "src/tinkerfin_contracts.egg-info"
    metadata.mkdir()
    (metadata / "SOURCES.txt").write_text("deleted_module.py\n", encoding="utf-8")
    before = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

    result = _build(root, "tinkerfin-contracts")

    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next((root / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert (
            archive.read("tinkerfin_contracts/local_module.py") == source.read_bytes()
        )
        assert (
            archive.read("tinkerfin_contracts/local_module.pyi") == b"CURRENT: bool\n"
        )
        assert (
            archive.read("tinkerfin_contracts/secrets/__init__.py") == b"LOCAL = True\n"
        )
        assert "tinkerfin_contracts/deleted_module.py" not in archive.namelist()
        assert not any("__pycache__" in name for name in archive.namelist())
    assert {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    } == before
    assert stale_source.stat().st_mtime == future


def test_payload_mismatch_prevents_publication_of_all_selected_wheels(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, "tinkerfin-contracts", "tinkerfin-native-stream")
    project = root / "packages/tinkerfin-native-stream"
    configuration = project / "pyproject.toml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            'where = ["src"]', 'where = ["src"]\nexclude = ["unshipped"]'
        ),
        encoding="utf-8",
    )
    omitted = project / "src/unshipped"
    omitted.mkdir()
    (omitted / "__init__.py").write_text("SOURCE = True\n", encoding="utf-8")

    result = _build(root, "tinkerfin-contracts", "tinkerfin-native-stream")

    assert result.returncode != 0
    assert "wheel/source mismatch" in result.stderr
    assert "unshipped/__init__.py" in result.stderr
    assert not tuple((root / "dist").glob("*.whl"))


def test_existing_output_wheels_are_preserved_and_rejected(tmp_path: Path) -> None:
    root = _workspace(tmp_path, "tinkerfin-contracts")
    output = root / "dist"
    output.mkdir()
    existing = output / "existing.whl"
    existing.write_bytes(b"owned by caller")

    result = _build(root, "tinkerfin-contracts")

    assert result.returncode != 0
    assert "Output already contains wheels" in result.stderr
    assert existing.read_bytes() == b"owned by caller"
    assert tuple(output.iterdir()) == (existing,)
