"""Run cross-platform local and CI checks for the Studio application."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

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
    cwd: Path = ROOT_DIR,
    env: dict[str, str] | None = None,
) -> None:
    """Run one check and stop immediately if it fails."""
    print(f"==> {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


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
    run("pnpm", "--dir", str(WEB_DIR), "test:proxy")
    run(
        "node",
        "--test",
        "tests/packaging/document-preview-licenses.test.mjs",
        cwd=WEB_DIR,
    )
    if not skip_browser:
        environment = os.environ.copy()
        environment.setdefault("PLAYWRIGHT_PORT", _free_port())
        run(
            "pnpm", "--dir", str(WEB_DIR), "exec", "playwright", "test", env=environment
        )


def verify_all() -> None:
    """Run the complete Studio server and web verification."""
    verify_server()
    verify_web()


def _staged_files() -> list[str]:
    result = subprocess.run(
        ("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"),
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.splitlines() if path]


def _ensure_staged_snapshot(paths: list[str]) -> None:
    for path in paths:
        result = subprocess.run(("git", "diff", "--quiet", "--", path), cwd=ROOT_DIR)
        if result.returncode != 0:
            raise SystemExit(
                f"Stage all changes to this file before committing: {path}"
            )


def verify_staged() -> None:
    """Run quick checks for staged files and their directly related unit tests."""
    run("git", "diff", "--cached", "--check")
    paths = _staged_files()
    if not paths:
        return
    _ensure_staged_snapshot(paths)

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
        if path.startswith("apps/studio/server/tests/") and path.endswith(".py")
    ]
    web_tests = [
        path.removeprefix("apps/studio/web/")
        for path in paths
        if path.startswith("apps/studio/web/src/")
        and path.endswith((".test.ts", ".test.tsx"))
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
        server_tests.extend(SERVER_ATTACHMENT_TESTS)
    if web_attachment_change:
        web_tests.extend(WEB_ATTACHMENT_TESTS)
    if server_tests:
        run("uv", "run", "pytest", "-q", *dict.fromkeys(server_tests))
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
    }
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        valid = ", ".join(commands)
        raise SystemExit(f"Usage: {Path(sys.argv[0]).name} <{valid}>")
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
