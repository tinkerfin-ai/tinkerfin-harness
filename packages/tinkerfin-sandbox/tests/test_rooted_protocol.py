"""Atomic in-Sandbox protocol tests for Rooted filesystem operations."""

from pathlib import Path

from deepagents.backends import LocalShellBackend

from tinkerfin_sandbox.backends._rooted_protocol import (
    _build_rooted_command,
    _parse_rooted_response,
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
    assert response.result["preview_has_truncation_marker"] is False
    assert (workspace / "captures" / "capped.txt").stat().st_size == 100
