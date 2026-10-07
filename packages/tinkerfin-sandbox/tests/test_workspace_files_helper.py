"""Descriptor-confined file-query wire behavior against local fixture files."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue, TypeAdapter

_JSON = TypeAdapter(dict[str, JsonValue])
_HELPER = (
    files("tinkerfin_sandbox.backends")
    .joinpath("_workspace_files_helper.py.txt")
    .read_text()
)


def _query(
    root: Path,
    operation: str,
    path: str = "/",
    *,
    failure: Literal["admission", "observation"] | None = None,
    change: Literal["replace_parent", "sibling_edit"] | None = None,
    **limits: JsonValue,
) -> dict[str, JsonValue]:
    # The external runtime owns root observation. The fixture supplies that
    # provider boundary; the shipped reader performs actual descriptor traversal.
    provider = f"""
import os, sys, types
from contextlib import contextmanager
module = types.ModuleType("registry")
class RegistryError(Exception):
    reason = "stale"
def validate():
    if {failure!r} == "observation":
        raise RegistryError("observation ended")
class ProjectRegistry:
    @staticmethod
    @contextmanager
    def open_existing(root, project):
        if {failure!r} == "admission":
            raise RegistryError("workspace deleted")
        descriptor = os.open({str(root)!r}, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield types.SimpleNamespace(files=descriptor, project=project,
                incarnation="fixture", validate=validate)
        finally:
            os.close(descriptor)
module.ProjectRegistry = ProjectRegistry
module.RegistryError = RegistryError
sys.modules["registry"] = module
"""
    if change is not None:
        provider += f"""
from pathlib import Path
changed = False
def mutate():
    global changed
    if changed:
        return
    changed = True
    root = Path({str(root)!r})
    if {change!r} == "replace_parent":
        (root / "selected").rename(root / "detached")
        (root / "replacement").rename(root / "selected")
    else:
        (root / "selected" / "unrelated.txt").write_text("unrelated edit")
read, scan, info = os.read, os.scandir, os.stat
def changed_read(*args, **kwargs):
    result = read(*args, **kwargs)
    mutate()
    return result
def changed_scan(*args, **kwargs):
    result = scan(*args, **kwargs)
    mutate()
    return result
def changed_info(path, *args, **kwargs):
    result = info(path, *args, **kwargs)
    if path == "note.txt":
        mutate()
    return result
if {operation!r} == "text":
    os.read = changed_read
elif {operation!r} == "list":
    os.scandir = changed_scan
else:
    os.stat = changed_info
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            provider + _HELPER,
            json.dumps(
                {"project": "fixture", "operation": operation, "path": path, **limits}
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return _JSON.validate_json(result.stdout)


def test_directory_pages_are_bounded_and_names_are_literal(tmp_path: Path) -> None:
    (tmp_path / "z-directory").mkdir()
    for name in ["$(touch escaped).txt", "a.txt", "中文.txt"]:
        (tmp_path / name).write_text(name)
    first = _query(tmp_path, "list", limit=2, cursor=None)
    entries = first["entries"]
    assert isinstance(entries, list) and len(entries) == 2
    assert isinstance(entries[0], dict) and entries[0]["name"] == "z-directory"
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    second = _query(tmp_path, "list", limit=2, cursor=cursor)
    assert second["next_cursor"] is None
    assert not (tmp_path / "escaped").exists()
    (tmp_path / "new.txt").write_text("new")
    assert _query(tmp_path, "list", limit=2, cursor=cursor) == {
        "kind": "error",
        "code": "changed",
    }


def test_deleted_admission_is_absent_but_invalidated_reads_are_changed(
    tmp_path: Path,
) -> None:
    assert _query(tmp_path, "list", limit=2, failure="admission") == {
        "kind": "error",
        "code": "not_initialized",
    }
    assert _query(tmp_path, "list", limit=2, failure="observation") == {
        "kind": "error",
        "code": "changed",
    }


def test_text_prefix_preserves_utf8_and_line_limits(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("中文\nsecond\nthird", encoding="utf-8")
    byte_bound = _query(tmp_path, "text", "/note.txt", max_bytes=4, max_lines=200)
    assert byte_bound["text"] == "中" and byte_bound["truncated"] is True
    line_bound = _query(tmp_path, "text", "/note.txt", max_bytes=100, max_lines=1)
    assert line_bound["text"] == "中文\n" and line_bound["truncated"] is True
    full = _query(tmp_path, "text", "/note.txt", max_bytes=100, max_lines=200)
    assert full["text"] == "中文\nsecond\nthird" and full["truncated"] is False


def test_links_and_binary_entries_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("private")
    (root / "link").symlink_to(tmp_path)
    (root / "binary").write_bytes(b"\x00\xff")
    (root / "symbol").symlink_to(tmp_path / "secret.txt")
    denied = _query(root, "text", "/link/secret.txt", max_bytes=100, max_lines=200)
    assert denied["kind"] == "error"
    for name in ["binary", "symbol"]:
        assert _query(root, "text", f"/{name}", max_bytes=100, max_lines=200) == {
            "kind": "error",
            "code": "not_text",
        }
    assert _query(root, "text", "/../secret.txt", max_bytes=100, max_lines=200) == {
        "kind": "error",
        "code": "invalid_path",
    }


@pytest.mark.parametrize("operation", ["text", "stat", "list"])
@pytest.mark.parametrize("change", ["replace_parent", "sibling_edit"])
def test_path_identity_rejects_replaced_ancestors_but_allows_sibling_edits(
    tmp_path: Path,
    operation: str,
    change: Literal["replace_parent", "sibling_edit"],
) -> None:
    for name, text in [("selected", "old tree"), ("replacement", "current tree")]:
        directory = tmp_path / name / "nested"
        directory.mkdir(parents=True)
        (directory / "note.txt").write_text(text)
    path = "/selected/nested" + ("" if operation == "list" else "/note.txt")
    result = _query(
        tmp_path,
        operation,
        path,
        change=change,
        max_bytes=100,
        max_lines=200,
        limit=2,
    )
    if change == "replace_parent":
        assert result == {"kind": "error", "code": "changed"}
    else:
        assert (
            result["kind"]
            == {"text": "text", "stat": "info", "list": "directory"}[operation]
        )
