"""Atomic in-Sandbox protocol tests for Rooted filesystem operations."""

import json
import shlex
import subprocess
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest
from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from tinkerfin_sandbox.backends import _rooted_protocol
from tinkerfin_sandbox.backends._rooted_protocol import (
    _build_rooted_command,
    _build_rooted_transfer_command,
    _parse_rooted_response,
    _parse_rooted_transfer_handshake,
    _RootedError,
    _RootedTransferHandshake,
)


def test_rooted_helper_resource_remains_byte_stable() -> None:
    resource = (
        files("tinkerfin_sandbox.backends")
        .joinpath("_rooted_helper.py.txt")
        .read_bytes()
        .decode("utf-8")
        .strip()
    )
    assert resource == _rooted_protocol._ROOTED_HELPER_SCRIPT
    assert sha256(resource.encode()).hexdigest() == (
        "962d62c0d2837a9a96ab2a5d4bea109120b4428b80ca2038d57284ac59f2873b"
    )


def _local_backend(*, cwd: Path | str = "/") -> LocalShellBackend:
    return LocalShellBackend(
        root_dir=cwd,
        virtual_mode=False,
        max_output_bytes=2_000_000,
        env={
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
            "PYTHONSAFEPATH": "1",
        },
        inherit_env=True,
    )


def _probe(*, backend: LocalShellBackend, root: Path, path: str):
    request = _build_rooted_command(
        root=str(root),
        operation="probe",
        arguments={"path": path},
    )
    return _parse_rooted_response(
        backend.execute(request.command),
        request=request,
    )


def test_rooted_helper_allows_stable_internal_link(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "inside-link").symlink_to("inside.txt")

    response = _probe(
        backend=_local_backend(),
        root=workspace,
        path="/inside-link",
    )

    assert response.status == "ok"
    assert response.operation == "probe"
    assert response.error is None
    assert response.result == {"kind": "file"}


def test_rooted_helper_reads_requested_text_page(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "lines.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={
            "path": "/lines.txt",
            "offset": 1,
            "limit": 2,
            "binary": False,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "read"
    assert response.error is None
    assert response.result == {
        "encoding": "utf-8",
        "content": "two\nthree",
        "total_lines": 3,
        "start_line": 2,
        "end_line": 3,
        "next_offset": None,
        "no_lines_requested": False,
    }


def test_rooted_helper_rejects_binary_preview_over_limit(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "large.bin").write_bytes(b"x" * (500 * 1024 + 1))
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={
            "path": "/large.bin",
            "offset": 0,
            "limit": 2000,
            "binary": True,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "binary_too_large"
    assert response.error.message == (
        "Binary file exceeds maximum preview size of 512000 bytes"
    )
    assert response.result is None


def test_rooted_helper_caps_oversized_text_page(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "long.txt").write_text("x" * (600 * 1024) + "\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={
            "path": "/long.txt",
            "offset": 0,
            "limit": 1,
            "binary": False,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "read"
    assert len(response.result["content"].encode("utf-8")) == 500 * 1024
    assert response.result["content"].endswith(
        "\n\n[Output was truncated due to size limits. "
        "This paginated read result exceeded the sandbox stdout limit. "
        "Continue reading with a larger offset or smaller limit to inspect "
        "the rest of the file.]"
    )
    assert response.result["total_lines"] == 1
    assert response.result["start_line"] == 1
    assert response.result["end_line"] == 1
    assert response.result["next_offset"] is None


def test_rooted_helper_returns_empty_file_reminder_before_zero_limit(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "empty.txt").write_bytes(b"")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/empty.txt", "offset": 0, "limit": 0, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "read"
    assert response.result == {
        "encoding": "utf-8",
        "content": "System reminder: File exists but has empty contents",
        "total_lines": None,
        "start_line": None,
        "end_line": None,
        "next_offset": None,
        "no_lines_requested": False,
    }


def test_rooted_helper_returns_uninspected_zero_line_window(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "content.txt").write_text("content\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/content.txt", "offset": -4, "limit": -1, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "read"
    assert response.result["content"] == ""
    assert response.result["start_line"] is None
    assert response.result["end_line"] is None
    assert response.result["next_offset"] is None
    assert response.result["no_lines_requested"] is True


def test_rooted_helper_auto_detects_binary_content(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "unknown.data").write_bytes(b"a\x00\xffb")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/unknown.data", "offset": 0, "limit": 2, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "read"
    assert response.result["encoding"] == "base64"
    assert response.result["content"] == "YQD/Yg=="
    assert response.result["start_line"] is None
    assert response.result["next_offset"] is None


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/missing.txt", "not_found"),
        ("/directory", "not_a_file"),
    ],
)
def test_rooted_helper_reports_read_target_errors(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "directory").mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": path, "offset": 0, "limit": 2, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == expected_code
    assert response.result is None


def test_rooted_helper_reports_line_offset_past_eof(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "one-line.txt").write_text("only\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/one-line.txt", "offset": 3, "limit": 1, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "offset_exceeds_file_length"
    assert response.error.message == "Line offset 3 exceeds file length (1 lines)"


def test_rooted_helper_reads_stable_internal_link_and_rejects_external_link(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "inside-link").symlink_to("inside.txt")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside)

    inside_request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/inside-link", "offset": 0, "limit": 1, "binary": False},
    )
    outside_request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/outside-link", "offset": 0, "limit": 1, "binary": False},
    )

    inside_response = _parse_rooted_response(
        _local_backend().execute(inside_request.command),
        request=inside_request,
    )
    outside_response = _parse_rooted_response(
        _local_backend().execute(outside_request.command),
        request=outside_request,
    )

    assert inside_response.status == "ok"
    assert inside_response.operation == "read"
    assert inside_response.result["content"] == "inside"
    assert outside_response.status == "error"
    assert outside_response.error.code == "invalid_path"
    assert "outside sentinel" not in outside_response.error.message


def test_rooted_helper_edits_crlf_file_without_changing_line_endings(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "lines.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    request = _build_rooted_command(
        root=str(workspace),
        operation="edit",
        arguments={
            "path": "/lines.txt",
            "old": "two\n",
            "new": "TWO\n",
            "replace_all": False,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "edit"
    assert response.error is None
    assert response.result == {"count": 1}
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_rooted_helper_requires_replace_all_for_repeated_text(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "repeated.txt"
    target.write_text("old old\n", encoding="utf-8")
    one_request = _build_rooted_command(
        root=str(workspace),
        operation="edit",
        arguments={
            "path": "/repeated.txt",
            "old": "old",
            "new": "new",
            "replace_all": False,
        },
    )

    one_response = _parse_rooted_response(
        _local_backend().execute(one_request.command),
        request=one_request,
    )

    assert one_response.status == "error"
    assert one_response.error.code == "multiple_occurrences"
    assert target.read_text(encoding="utf-8") == "old old\n"

    all_request = _build_rooted_command(
        root=str(workspace),
        operation="edit",
        arguments={
            "path": "/repeated.txt",
            "old": "old",
            "new": "new",
            "replace_all": True,
        },
    )
    all_response = _parse_rooted_response(
        _local_backend().execute(all_request.command),
        request=all_request,
    )

    assert all_response.status == "ok"
    assert all_response.operation == "edit"
    assert all_response.result == {"count": 2}
    assert target.read_text(encoding="utf-8") == "new new\n"


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/missing.txt", "not_found"),
        ("/directory", "not_a_file"),
        ("/binary.txt", "not_text_file"),
    ],
)
def test_rooted_helper_reports_edit_target_errors(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "directory").mkdir()
    (workspace / "binary.txt").write_bytes(b"text\xff")
    request = _build_rooted_command(
        root=str(workspace),
        operation="edit",
        arguments={
            "path": path,
            "old": "text",
            "new": "updated",
            "replace_all": False,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == expected_code
    assert response.result is None


def test_rooted_helper_edits_internal_link_target_and_preserves_mode(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    link = workspace / "target-link"
    link.symlink_to("target.txt")
    request = _build_rooted_command(
        root=str(workspace),
        operation="edit",
        arguments={
            "path": "/target-link",
            "old": "old",
            "new": "new",
            "replace_all": False,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "edit"
    assert response.result == {"count": 1}
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o640


def test_rooted_helper_recursively_deletes_directory_without_following_links(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    tree = workspace / "tree"
    nested = tree / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    (tree / "outside-link").symlink_to(outside, target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="delete",
        arguments={"path": "/tree"},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "delete"
    assert response.error is None
    assert response.result == {"deleted": True}
    assert not tree.exists()
    assert sentinel.read_text(encoding="utf-8") == "outside sentinel"


def test_rooted_helper_deletes_internal_link_but_rejects_external_link(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    internal_link = workspace / "inside-link"
    internal_link.symlink_to("inside.txt")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    external_link = workspace / "outside-link"
    external_link.symlink_to(outside)

    internal_request = _build_rooted_command(
        root=str(workspace),
        operation="delete",
        arguments={"path": "/inside-link"},
    )
    external_request = _build_rooted_command(
        root=str(workspace),
        operation="delete",
        arguments={"path": "/outside-link"},
    )
    internal_response = _parse_rooted_response(
        _local_backend().execute(internal_request.command),
        request=internal_request,
    )
    external_response = _parse_rooted_response(
        _local_backend().execute(external_request.command),
        request=external_request,
    )

    assert internal_response.status == "ok"
    assert internal_response.operation == "delete"
    assert not internal_link.exists()
    assert inside.read_text(encoding="utf-8") == "inside"
    assert external_response.status == "error"
    assert external_response.error.code == "invalid_path"
    assert external_link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_rooted_helper_reports_missing_delete_target(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="delete",
        arguments={"path": "/missing"},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "not_found"


def test_rooted_helper_lists_entries_without_following_entry_links(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    directory = workspace / "docs"
    directory.mkdir(parents=True)
    (directory / "file.txt").write_text("content", encoding="utf-8")
    (directory / "nested").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "outside-link").symlink_to(outside, target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="list",
        arguments={"path": "/docs"},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "list"
    assert response.error is None
    assert sorted(response.result["entries"], key=lambda entry: entry["path"]) == [
        {"path": "/docs/file.txt", "is_dir": False},
        {"path": "/docs/nested", "is_dir": True},
        {"path": "/docs/outside-link", "is_dir": False},
    ]


def test_rooted_helper_lists_internal_directory_link_using_requested_path(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    actual = workspace / "actual"
    actual.mkdir(parents=True)
    (actual / "child.txt").write_text("child", encoding="utf-8")
    (workspace / "alias").symlink_to("actual", target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="list",
        arguments={"path": "/alias"},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "list"
    assert response.result == {
        "entries": [{"path": "/alias/child.txt", "is_dir": False}],
        "partial_error": None,
    }


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/missing", "not_found"),
        ("/file.txt", "not_directory"),
        ("/outside-link", "invalid_path"),
    ],
)
def test_rooted_helper_reports_list_target_errors(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="list",
        arguments={"path": path},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == expected_code


def test_rooted_helper_glob_returns_sorted_root_confined_matches(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    source = workspace / "src"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "a.py").write_text("a", encoding="utf-8")
    (source / ".hidden.py").write_text("hidden", encoding="utf-8")
    (nested / "b.py").write_text("b", encoding="utf-8")
    (workspace / "alias").symlink_to("src", target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("outside", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="glob",
        arguments={"path": "/", "pattern": "**/*.py"},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "glob"
    assert response.error is None
    assert response.result == {
        "matches": [
            {"path": "/alias/a.py", "is_dir": False},
            {"path": "/alias/nested/b.py", "is_dir": False},
            {"path": "/src/a.py", "is_dir": False},
            {"path": "/src/nested/b.py", "is_dir": False},
        ],
        "truncated": False,
        "partial_error": None,
    }


def test_rooted_helper_glob_preserves_empty_and_backslash_patterns(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    special = workspace / "dir\\name"
    special.mkdir(parents=True)
    (special / "match.py").write_text("match", encoding="utf-8")

    empty_request = _build_rooted_command(
        root=str(workspace),
        operation="glob",
        arguments={"path": "/", "pattern": ""},
    )
    backslash_request = _build_rooted_command(
        root=str(workspace),
        operation="glob",
        arguments={"path": "/", "pattern": "dir\\name/*.py"},
    )
    empty_response = _parse_rooted_response(
        _local_backend().execute(empty_request.command),
        request=empty_request,
    )
    backslash_response = _parse_rooted_response(
        _local_backend().execute(backslash_request.command),
        request=backslash_request,
    )

    assert empty_response.status == "ok"
    assert empty_response.operation == "glob"
    assert empty_response.result["matches"] == []
    assert backslash_response.status == "ok"
    assert backslash_response.operation == "glob"
    assert backslash_response.result["matches"] == [
        {"path": "/dir\\name/match.py", "is_dir": False}
    ]


def test_rooted_helper_grep_applies_relative_glob_and_total_match_cap(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    source = workspace / "src"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "a.py").write_text("needle one\nneedle two\n", encoding="utf-8")
    (nested / "b.py").write_text("needle three\n", encoding="utf-8")
    (workspace / "outside.py").write_text("needle outside\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="grep",
        arguments={
            "path": "/",
            "pattern": "needle",
            "glob": "src/**/*.py",
            "max_count": 2,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "grep"
    assert response.error is None
    assert response.result == {
        "matches": [
            {"path": "/src/a.py", "line": 1, "text": "needle one"},
            {"path": "/src/a.py", "line": 2, "text": "needle two"},
        ],
        "truncated": True,
        "partial_error": None,
    }


def test_rooted_helper_grep_is_literal_and_exact_cap_is_complete(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "content.txt").write_text("plain\nx.y\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="grep",
        arguments={
            "path": "/content.txt",
            "pattern": ".",
            "glob": "*.py",
            "max_count": 1,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "grep"
    assert response.result == {
        "matches": [{"path": "/content.txt", "line": 2, "text": "x.y"}],
        "truncated": False,
        "partial_error": None,
    }


def test_rooted_helper_grep_includes_hidden_files_for_basename_glob(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / ".hidden.py").write_text("needle hidden\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="grep",
        arguments={
            "path": "/",
            "pattern": "needle",
            "glob": "*.py",
            "max_count": None,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "grep"
    assert response.result == {
        "matches": [
            {
                "path": "/nested/.hidden.py",
                "line": 1,
                "text": "needle hidden",
            }
        ],
        "truncated": False,
        "partial_error": None,
    }


def test_rooted_helper_grep_treats_empty_glob_as_unfiltered(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "content.txt").write_text("needle\n", encoding="utf-8")
    request = _build_rooted_command(
        root=str(workspace),
        operation="grep",
        arguments={
            "path": "/",
            "pattern": "needle",
            "glob": "",
            "max_count": None,
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "grep"
    assert response.result["matches"] == [
        {"path": "/content.txt", "line": 1, "text": "needle"}
    ]


def test_rooted_helper_rejects_symlink_root(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    root_link = tmp_path / "workspace-link"
    root_link.symlink_to(workspace, target_is_directory=True)

    response = _probe(
        backend=_local_backend(),
        root=root_link,
        path="/inside.txt",
    )

    assert response.status == "error"
    assert response.error.code == "invalid_path"
    assert response.result is None


def test_rooted_helper_rejects_missing_root_as_invalid_path(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-workspace"
    request = _build_rooted_command(
        root=str(missing_root),
        operation="read",
        arguments={"path": "/file.txt", "offset": 0, "limit": 1, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "invalid_path"
    assert response.result is None


def test_rooted_helper_does_not_disguise_root_resource_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    original_script = _rooted_protocol._ROOTED_HELPER_SCRIPT
    statement = "        before = os.lstat(root)"
    assert original_script.count(statement) == 1
    monkeypatch.setattr(
        _rooted_protocol,
        "_ROOTED_HELPER_SCRIPT",
        original_script.replace(
            statement,
            '        raise OSError(errno.EMFILE, "descriptor table full")',
        ),
    )
    request = _build_rooted_command(
        root=str(workspace),
        operation="read",
        arguments={"path": "/inside.txt", "offset": 0, "limit": 1, "binary": False},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "operation_failed"
    assert response.result is None


def test_rooted_helper_rejects_root_with_symlinked_ancestor(tmp_path: Path) -> None:
    actual_parent = (tmp_path / "actual-parent").resolve()
    workspace = actual_parent / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    response = _probe(
        backend=_local_backend(),
        root=linked_parent / "workspace",
        path="/inside.txt",
    )

    assert response.status == "error"
    assert response.error.code == "invalid_path"
    assert response.result is None


def test_rooted_helper_rejects_external_leaf_link(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    (workspace / "external-link").symlink_to(outside)

    response = _probe(
        backend=_local_backend(),
        root=workspace,
        path="/external-link",
    )

    assert response.status == "error"
    assert response.error.code == "invalid_path"
    assert response.result is None
    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_rooted_helper_rejects_external_parent_link(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    (workspace / "external-parent").symlink_to(outside, target_is_directory=True)

    response = _probe(
        backend=_local_backend(),
        root=workspace,
        path="/external-parent/sentinel.txt",
    )

    assert response.status == "error"
    assert response.error.code == "invalid_path"
    assert response.result is None
    assert sentinel.read_text(encoding="utf-8") == "outside sentinel"


def test_rooted_helper_treats_shell_metacharacters_as_path_data(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    marker = tmp_path / "command-injection-marker"
    malicious_path = f"/missing'; touch {marker}; #"
    request = _build_rooted_command(
        root=str(workspace),
        operation="probe",
        arguments={"path": malicious_path},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "error"
    assert response.error.code == "not_found"
    assert not marker.exists()


def test_rooted_helper_ignores_workspace_json_module(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "json.py").write_text(
        "raise RuntimeError('workspace module imported')\n",
        encoding="utf-8",
    )

    response = _probe(
        backend=_local_backend(cwd=workspace),
        root=workspace,
        path="/inside.txt",
    )

    assert response.status == "ok"
    assert response.result == {"kind": "file"}


def test_rooted_response_rejects_malformed_json(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="probe",
        arguments={"path": "/inside.txt"},
    )

    with pytest.raises(ValueError, match="response malformed"):
        _parse_rooted_response(
            ExecuteResponse(output="not-json", exit_code=0),
            request=request,
        )


def test_rooted_response_rejects_mismatched_request_id(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="probe",
        arguments={"path": "/inside.txt"},
    )
    payload = {
        "request_id": "different-request",
        "operation": request.operation,
        "status": "ok",
        "error": None,
        "result": {"kind": "file"},
    }

    with pytest.raises(ValueError, match="request mismatch"):
        _parse_rooted_response(
            ExecuteResponse(output=json.dumps(payload), exit_code=0),
            request=request,
        )


def test_rooted_response_rejects_mismatched_operation(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="probe",
        arguments={"path": "/inside.txt"},
    )
    payload = {
        "request_id": request.request_id,
        "operation": "read",
        "status": "ok",
        "error": None,
        "result": {
            "encoding": "utf-8",
            "content": "content",
            "total_lines": 1,
            "start_line": 1,
            "end_line": 1,
            "next_offset": None,
            "no_lines_requested": False,
        },
    }

    with pytest.raises(ValueError, match="operation mismatch"):
        _parse_rooted_response(
            ExecuteResponse(output=json.dumps(payload), exit_code=0),
            request=request,
        )


def test_rooted_transfer_helper_emits_live_descriptor_handshake(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_transfer_command(
        root=str(workspace),
        path="/nested/target.bin",
        mode="upload",
        token="local-transfer-token",
        hold_seconds=5,
    )
    process = subprocess.Popen(
        shlex.split(request.command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        handshake_line = process.stdout.readline()
        handshake = _parse_rooted_transfer_handshake(
            handshake_line,
            request=request,
        )

        assert isinstance(handshake, _RootedTransferHandshake)
        assert handshake.token == "local-transfer-token"
        assert handshake.mode == "upload"
        assert handshake.pid == process.pid
        assert handshake.fd > 0
        assert (workspace / "nested" / "target.bin").is_file()
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/outside-link", "invalid_path"),
        ("/missing.bin", "not_found"),
        ("/missing/nested/file.bin", "not_found"),
    ],
)
def test_rooted_transfer_helper_emits_correlated_target_error(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (workspace / "outside-link").symlink_to(outside)
    request = _build_rooted_transfer_command(
        root=str(workspace),
        path=path,
        mode="download",
        token="local-transfer-token",
        hold_seconds=1,
    )

    raw = _local_backend().execute(request.command)
    record = _parse_rooted_transfer_handshake(raw.output, request=request)

    assert isinstance(record, _RootedError)
    assert record.code == expected_code
    assert not (workspace / "missing").exists()


def test_rooted_helper_offload_returns_small_output_inline(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="offload",
        arguments={
            "path": "/captures/call-1.txt",
            "command": "printf 'inline output'; printf 'warning' >&2; exit 7",
            "max_inline_bytes": 100,
            "max_capture_bytes": 1024,
            "working_directory": str(workspace),
            "command_env": {},
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "offload"
    assert response.result == {
        "offloaded": False,
        "output": "inline outputwarning",
        "exit_code": 7,
        "truncated": False,
    }
    assert not (workspace / "captures" / "call-1.txt").exists()


def test_rooted_helper_offload_publishes_large_output_with_preview(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    command = "for i in $(seq 1 20); do printf 'line-%02d-output\\n' \"$i\"; done"
    expected = "".join(f"line-{index:02d}-output\n" for index in range(1, 21))
    request = _build_rooted_command(
        root=str(workspace),
        operation="offload",
        arguments={
            "path": "/captures/call-1.txt",
            "command": command,
            "max_inline_bytes": 20,
            "max_capture_bytes": 4096,
            "working_directory": str(workspace),
            "command_env": {},
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "offload"
    assert response.result["offloaded"] is True
    assert response.result["exit_code"] == 0
    assert response.result["truncated"] is False
    assert "line-01-output" in response.result["output"]
    assert "lines truncated" in response.result["output"]
    assert "line-20-output" in response.result["output"]
    assert (workspace / "captures" / "call-1.txt").read_text() == expected


def test_rooted_helper_offload_enforces_hard_capture_cap(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    request = _build_rooted_command(
        root=str(workspace),
        operation="offload",
        arguments={
            "path": "/captures/capped.txt",
            "command": "python3 -c \"print('x' * 2000, end='')\"",
            "max_inline_bytes": 10,
            "max_capture_bytes": 100,
            "working_directory": str(workspace),
            "command_env": {},
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "offload"
    assert response.result["offloaded"] is True
    assert response.result["truncated"] is True
    assert (workspace / "captures" / "capped.txt").stat().st_size == 100


def test_rooted_helper_offload_unsafe_capture_executes_command_once(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside)
    marker = workspace / "count.txt"
    request = _build_rooted_command(
        root=str(workspace),
        operation="offload",
        arguments={
            "path": "/outside-link",
            "command": "printf x >> count.txt; printf 'large output'",
            "max_inline_bytes": 1,
            "max_capture_bytes": 1024,
            "working_directory": str(workspace),
            "command_env": {},
        },
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "offload"
    assert response.result == {
        "offloaded": False,
        "output": "large output",
        "exit_code": 0,
        "truncated": False,
    }
    assert marker.read_text(encoding="utf-8") == "x"
    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_rooted_helper_reset_clears_children_and_keeps_root(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    request = _build_rooted_command(
        root=str(workspace),
        operation="reset",
        arguments={},
    )

    response = _parse_rooted_response(
        _local_backend().execute(request.command),
        request=request,
    )

    assert response.status == "ok"
    assert response.operation == "reset"
    assert response.result == {"reset": True}
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []
    assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
