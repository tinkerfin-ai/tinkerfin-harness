"""Run cross-platform local and CI checks for the Studio application."""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from pytest import ExitCode

ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "apps" / "studio" / "web"
SERVER_ATTACHMENT_TESTS = [
    "apps/studio/server/tests/test_attachments.py",
    "apps/studio/server/tests/test_sandbox_attachment_tools.py",
    "apps/studio/server/tests/test_work_file_tools.py",
]
WEB_ATTACHMENT_TESTS = [
    "src/features/conversation/attachments/AttachmentList.test.tsx",
    "src/features/conversation/attachments/DocumentAttachmentPreview.test.tsx",
    "src/features/conversation/useAttachments.test.ts",
]


def run(
    *command: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run one check and stop immediately if it fails."""
    print(f"==> {shlex.join(command)}", flush=True)
    # Resolve PATHEXT on Windows, where pnpm is usually installed as pnpm.cmd.
    executable = shutil.which(command[0], path=None if env is None else env.get("PATH"))
    if executable is None:
        raise SystemExit(f"Required command not found: {command[0]}")
    subprocess.run((executable, *command[1:]), cwd=cwd or ROOT_DIR, check=True, env=env)


def verify_server() -> None:
    """Run the complete Studio server verification."""
    run(
        "uv",
        "run",
        "ruff",
        "format",
        "--no-cache",
        "--check",
        "apps/studio/server",
        "scripts",
        "tests",
    )
    run(
        "uv",
        "run",
        "ruff",
        "check",
        "--no-cache",
        "apps/studio/server",
        "scripts",
        "tests",
    )
    run("uv", "run", "pyright")
    run(
        "uv",
        "run",
        "pytest",
        "tests/test_latest_only_contracts.py",
        "tests/test_docker_services.py",
        "tests/test_studio_checks.py",
        "apps/studio/server",
        "--durations=10",
    )


def _free_port() -> str:
    with socket.socket() as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return str(server_socket.getsockname()[1])


def verify_web(*, skip_browser: bool = False) -> None:
    """Run the complete Studio web verification."""
    run("pnpm", "--dir", str(WEB_DIR), "test", "--run")
    run("pnpm", "--dir", str(WEB_DIR), "lint")
    run("pnpm", "--dir", str(WEB_DIR), "build")
    run(
        "node",
        "--test",
        "tests/packaging/document-preview-licenses.test.mjs",
        cwd=WEB_DIR,
    )
    if not skip_browser:
        # The upload proxy test launches Chromium, just like the UI tests.
        run("pnpm", "--dir", str(WEB_DIR), "test:proxy")
        environment = os.environ.copy()
        environment.setdefault("PLAYWRIGHT_PORT", _free_port())
        run(
            "pnpm", "--dir", str(WEB_DIR), "exec", "playwright", "test", env=environment
        )


def verify_all() -> None:
    """Run the complete Studio server and web verification."""
    verify_server()
    verify_web()


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _staged_files() -> list[str]:
    return [
        path
        for path in _git(
            "diff", "--cached", "--name-only", "--diff-filter=ACMRD", "-z"
        ).split("\0")
        if path
    ]


def _ensure_worktree_matches(reference: str | None = None) -> None:
    """Require checked files and their dependencies to match the Git snapshot.

    The index is used for commits; HEAD is used for pushes. Files are never
    stashed or overwritten. Ignored dependencies and local configuration remain
    available, so this guard does not replace checks in a fresh CI checkout.
    """
    revisions = () if reference is None else (reference,)
    changed = _git("diff", "--name-only", "-z", *revisions, "--")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    if changed or untracked:
        raise SystemExit(
            "The working tree does not match the content to check. "
            "Stage or save unstaged/untracked files before committing; "
            "commit or save all changes before pushing."
        )


def verify_pushed() -> None:
    """Check pushes whose non-deleted ref targets all resolve to checked-out HEAD."""
    updates = [line.split() for line in sys.stdin if line.strip()]
    head = _git("rev-parse", "HEAD").strip() if updates else ""
    check_needed = False
    for update in updates:
        if len(update) != 4:
            raise SystemExit("Invalid pre-push input from Git")
        _, local_oid, _, _ = update
        if set(local_oid) == {"0"}:
            continue
        target = _git("rev-parse", "--verify", f"{local_oid}^{{commit}}").strip()
        if target != head:
            raise SystemExit("Check out the commit being pushed before pushing it")
        check_needed = True
    if check_needed:
        _ensure_worktree_matches("HEAD")
        verify_all()


def verify_staged() -> None:
    """Run quick checks for staged files and their directly related unit tests."""
    run("git", "diff", "--cached", "--check")
    paths = _staged_files()
    if not paths:
        return
    _ensure_worktree_matches()

    python_files = [
        path for path in paths if path.endswith(".py") and (ROOT_DIR / path).is_file()
    ]
    web_files = [
        path.removeprefix("apps/studio/web/")
        for path in paths
        if path.startswith("apps/studio/web/src/")
        and path.endswith((".ts", ".tsx"))
        and (ROOT_DIR / path).is_file()
    ]
    server_tests = [
        path
        for path in paths
        if path.startswith("apps/studio/server/tests/")
        and path.endswith(".py")
        and (ROOT_DIR / path).is_file()
    ]
    web_tests = [
        path.removeprefix("apps/studio/web/")
        for path in paths
        if path.startswith("apps/studio/web/src/")
        and path.endswith((".test.ts", ".test.tsx"))
        and (ROOT_DIR / path).is_file()
    ]
    server_attachment_change = any(
        path.startswith("apps/studio/server/src/tinkerfin_studio/attachments/")
        or path == "apps/studio/server/src/tinkerfin_studio/agent/runtime.py"
        for path in paths
    )
    web_attachment_change = any(
        path.startswith("apps/studio/web/src/features/conversation/attachments/")
        or path
        in {
            "apps/studio/web/src/features/conversation/useAttachments.ts",
            "apps/studio/web/src/features/conversation/components/Composer.tsx",
            "apps/studio/web/src/features/automation/AutomationEditor.tsx",
        }
        for path in paths
    )

    if python_files:
        run("uv", "run", "ruff", "format", "--no-cache", "--check", *python_files)
        run("uv", "run", "ruff", "check", "--no-cache", *python_files)
    if web_files:
        run("pnpm", "--dir", str(WEB_DIR), "exec", "eslint", *web_files)
    if server_attachment_change:
        server_tests.extend(
            path for path in SERVER_ATTACHMENT_TESTS if (ROOT_DIR / path).is_file()
        )
    if web_attachment_change:
        web_tests.extend(
            path for path in WEB_ATTACHMENT_TESTS if (WEB_DIR / path).is_file()
        )
    if any(path.startswith(("scripts/", ".githooks/")) for path in paths):
        server_tests.append("tests/test_studio_checks.py")
    if server_tests:
        try:
            run("uv", "run", "pytest", "-q", *dict.fromkeys(server_tests))
        except subprocess.CalledProcessError as error:
            if error.returncode != ExitCode.NO_TESTS_COLLECTED:
                raise
            print(
                "No runnable tests matched the staged files under the repository's "
                "test selection; excluded integration tests were not run.",
                flush=True,
            )
    if web_tests:
        run("pnpm", "--dir", str(WEB_DIR), "test", "--run", *dict.fromkeys(web_tests))


def main() -> None:
    """Dispatch a requested verification mode."""
    commands = {
        "server": verify_server,
        "web": verify_web,
        "web-no-browser": lambda: verify_web(skip_browser=True),
        "all": verify_all,
        "staged": verify_staged,
        "pushed": verify_pushed,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        valid = ", ".join(commands)
        raise SystemExit(f"Usage: {Path(sys.argv[0]).name} <{valid}>")
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
