"""Verify check selection and Git hook boundaries without changing user files."""

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import studio_checks as checks

SOURCE_ROOT = checks.ROOT_DIR


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Script Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    git(root, "config", "commit.gpgsign", "false")
    (root / "helper.txt").write_text("original\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    monkeypatch.setattr(checks, "ROOT_DIR", root)
    return root


def test_staged_paths_preserve_unicode_and_spaces(repository: Path) -> None:
    name = "中文 file.txt"
    (repository / name).write_text("content\n", encoding="utf-8")
    git(repository, "add", "--", name)
    assert checks._staged_files() == [name]


@pytest.mark.parametrize("untracked", [False, True])
def test_staged_checks_reject_unstaged_dependencies(
    repository: Path, untracked: bool
) -> None:
    (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "staged.txt")
    (repository / ("new-helper.txt" if untracked else "helper.txt")).write_text(
        "not staged\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="working tree"):
        checks.verify_staged()


@pytest.mark.parametrize("skip_browser", [True, False])
def test_web_check_browser_dependency_boundary(
    monkeypatch: pytest.MonkeyPatch, skip_browser: bool
) -> None:
    commands: list[tuple[str, ...]] = []

    def record(
        *command: str,
        cwd: Path = checks.ROOT_DIR,
        env: dict[str, str] | None = None,
    ) -> None:
        commands.append(command)
        if command[0] == "node":
            assert cwd == checks.WEB_DIR

    monkeypatch.setattr(checks, "run", record)
    monkeypatch.setattr(checks, "_free_port", lambda: "4173")
    checks.verify_web(skip_browser=skip_browser)
    assert any("build" in command for command in commands)
    assert any(
        "tests/packaging/document-preview-licenses.test.mjs" in command
        for command in commands
    )
    assert any("test:proxy" in command for command in commands) is not skip_browser
    assert any("playwright" in command for command in commands) is not skip_browser


def test_clean_staged_changes_and_deletions(repository: Path) -> None:
    (repository / "helper.txt").unlink()
    git(repository, "add", "-u")
    assert checks._staged_files() == ["helper.txt"]
    checks.verify_staged()


@pytest.mark.parametrize("dirty", ["clean", "staged", "unstaged", "untracked"])
def test_push_checks_head_content(
    repository: Path, monkeypatch: pytest.MonkeyPatch, dirty: str
) -> None:
    head = git(repository, "rev-parse", "HEAD")
    if dirty != "clean":
        (repository / ("new.txt" if dirty == "untracked" else "helper.txt")).write_text(
            "changed\n", encoding="utf-8"
        )
        if dirty == "staged":
            git(repository, "add", "helper.txt")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"),
    )
    checked: list[bool] = []
    monkeypatch.setattr(checks, "verify_all", lambda: checked.append(True))
    if dirty == "clean":
        checks.verify_pushed()
        assert checked == [True]
    else:
        with pytest.raises(SystemExit, match="working tree"):
            checks.verify_pushed()
        assert not checked


def test_push_rejects_other_commits_and_skips_deletions(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = git(repository, "rev-parse", "HEAD")
    git(repository, "commit", "--allow-empty", "-qm", "second commit")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/other {previous} refs/heads/other {'0' * 40}\n"),
    )
    with pytest.raises(SystemExit, match="Check out"):
        checks.verify_pushed()
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"(delete) {'0' * 40} refs/heads/other {previous}\n")
    )
    monkeypatch.setattr(
        checks, "verify_all", lambda: pytest.fail("Deletion must not run builds")
    )
    checks.verify_pushed()


def test_run_reports_missing_tools_and_preserves_exit_status(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Required command not found"):
        checks.run("missing-studio-check-command", env={"PATH": str(tmp_path)})
    with pytest.raises(subprocess.CalledProcessError) as error:
        checks.run(sys.executable, "-c", "raise SystemExit(17)", cwd=tmp_path)
    assert error.value.returncode == 17


@pytest.mark.skipif(os.name != "nt", reason="Windows command extension resolution")
def test_run_resolves_windows_command_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "studio-check.cmd").write_text("@exit /b 17\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(subprocess.CalledProcessError) as error:
        checks.run("studio-check", cwd=tmp_path)
    assert error.value.returncode == 17


@pytest.mark.parametrize(
    "entry,mode",
    [
        ("check-staged", "staged"),
        ("verify-studio-server", "server"),
        ("verify-studio-web", "web"),
        ("verify-studio", "all"),
    ],
)
def test_wrappers_use_their_own_repository_and_propagate_failure(
    tmp_path: Path, entry: str, mode: str
) -> None:
    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    windows = os.name == "nt"
    extension = ".ps1" if windows else ".sh"
    script = scripts / (entry + extension)
    shutil.copyfile(SOURCE_ROOT / "scripts" / script.name, script)
    shim = commands / ("uv.cmd" if windows else "uv")
    shim.write_text(
        "@echo off\necho %*\nexit /b 17\n"
        if windows
        else '#!/bin/sh\nprintf "%s\\n" "$@"\nexit 17\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    shell = shutil.which("pwsh" if windows else "sh")
    assert shell is not None
    command = (
        [shell, "-NoProfile", "-File", str(script)] if windows else [shell, str(script)]
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 17, result.stderr
    assert str(root) in result.stdout
    assert "--project" in result.stdout
    assert mode in result.stdout


def test_hook_installer_uses_target_repository(
    repository: Path, tmp_path: Path
) -> None:
    scripts = repository / "scripts"
    scripts.mkdir()
    extension = ".ps1" if os.name == "nt" else ".sh"
    script = scripts / ("install-git-hooks" + extension)
    shutil.copyfile(SOURCE_ROOT / "scripts" / script.name, script)
    shell = shutil.which("pwsh" if os.name == "nt" else "sh")
    assert shell is not None
    command = (
        [shell, "-NoProfile", "-File", str(script)]
        if os.name == "nt"
        else [shell, str(script)]
    )
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(repository, "config", "--local", "core.hooksPath") == ".githooks"


def test_installer_reports_git_failure(tmp_path: Path) -> None:
    scripts = tmp_path / "not-a-repository" / "scripts"
    scripts.mkdir(parents=True)
    extension = ".ps1" if os.name == "nt" else ".sh"
    script = scripts / ("install-git-hooks" + extension)
    shutil.copyfile(SOURCE_ROOT / "scripts" / script.name, script)
    shell = shutil.which("pwsh" if os.name == "nt" else "sh")
    assert shell is not None
    command = (
        [shell, "-NoProfile", "-File", str(script)]
        if os.name == "nt"
        else [shell, str(script)]
    )
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Enabled repository Git hooks" not in result.stdout
