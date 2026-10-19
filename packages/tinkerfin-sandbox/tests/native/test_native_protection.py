"""Native user-file access retains protected-tree isolation in running commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import pytest
from deepagents.backends.composite import CompositeBackend

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import LocalAccess, LocalWorkspace

IDENTITY = RunIdentity(namespace="native-test", thread_id="thread", run_id="run")


@pytest.mark.parametrize("filesystem", ["workspace", "user"])
async def test_private_profile_cannot_move_and_runtime_artifacts_remain_writable(
    tmp_path: Path, sandbox: Path, filesystem: Literal["workspace", "user"]
) -> None:
    profile = tmp_path / ".tinkerfin/browser-profile"
    profile.mkdir(parents=True)
    (profile / "cookie-fixture").write_text("private-fixture")
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        access=LocalAccess(filesystem=filesystem, protected_paths=(profile,)),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            renamed = await prepared.workspace.aexecute(
                'python -I -c \'import os; os.rename(".tinkerfin", "relocated")\''
            )
            assert renamed.exit_code != 0
            assert profile.is_dir() and not (tmp_path / "relocated").exists()
            assert isinstance(prepared.backend, CompositeBackend)
            artifact = str(Path(prepared.backend.artifacts_root) / "output.txt")
            result = await prepared.backend.awrite(artifact, "retained artifact")
            assert result.error is None
            assert Path(artifact).read_text() == "retained artifact"


PROBE = """
import json, os, pathlib, sys
active, other, outside, managed, private = map(pathlib.Path, sys.argv[1:])
print('ready', flush=True)
future = pathlib.Path(sys.stdin.readline().strip())
results = {}
for name, action in [
    ('own_read', lambda: (active / 'mine').read_text()),
    ('own_realpath', lambda: (active / 'mine').resolve(strict=True)),
    ('outside_read', lambda: (outside / 'public').read_text()),
    ('outside_write', lambda: (outside / 'new').write_text('yes')),
    ('private_read', lambda: (other / 'private').read_text()),
    ('private_directory_stat', lambda: other.stat()),
    ('private_directory_lstat', lambda: other.lstat()),
    ('private_file_stat', lambda: (other / 'private').stat()),
    ('future_read', lambda: (future / 'private').read_text()),
    ('future_write', lambda: (future / 'created').write_text('changed')),
    ('credential_read', lambda: private.read_text()),
    ('credential_write', lambda: private.write_text('changed')),
    ('symlink_read', lambda: (active / 'link').read_text()),
    ('private_write', lambda: (other / 'private').write_text('changed')),
    ('hardlink', lambda: os.link(other / 'private', outside / 'hardlink')),
    ('rename_private', lambda: other.rename(outside / 'other')),
    ('rename_credential', lambda: private.rename(outside / 'credential')),
    ('rename_active', lambda: active.rename(outside / 'active')),
    ('rename_parent', lambda: managed.rename(outside / 'managed')),
]:
    try:
        action()
        results[name] = True
    except OSError:
        results[name] = False
print(json.dumps(results), flush=True)
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS protection")
async def test_user_paths_preserve_protected_trees_created_after_process_start(
    tmp_path: Path, sandbox: Path
) -> None:
    managed = tmp_path / "managed"
    active = managed / "account/project"
    other = managed / "other/project"
    outside = tmp_path / "outside"
    for directory in (active, other, outside):
        directory.mkdir(parents=True)
    private = tmp_path / "credential"
    private.write_text("private")
    (active / "mine").write_text("mine")
    (other / "private").write_text("private")
    (outside / "public").write_text("outside")
    (active / "link").symlink_to(other / "private")
    async with LocalWorkspace(
        active,
        sandbox_executable=sandbox,
        access=LocalAccess(
            filesystem="user", isolated_roots=(managed,), protected_paths=(private,)
        ),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            async with prepared.workspace.open_process(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    PROBE,
                    str(active),
                    str(other),
                    str(outside),
                    str(managed),
                    str(private),
                )
            ) as process:
                assert await process.stdout.readline() == b"ready\n"
                future = managed / "future/project"
                future.mkdir(parents=True)
                (future / "private").write_text("future")
                output, error = await process.communicate((str(future) + "\n").encode())
                assert process.returncode == 0, error.decode()
                assert json.loads(output) == {
                    "own_read": True,
                    "own_realpath": True,
                    "outside_read": True,
                    "outside_write": True,
                    "private_read": False,
                    "private_directory_stat": False,
                    "private_directory_lstat": False,
                    "private_file_stat": False,
                    "future_read": False,
                    "future_write": False,
                    "credential_read": False,
                    "credential_write": False,
                    "symlink_read": False,
                    "private_write": False,
                    "hardlink": False,
                    "rename_private": False,
                    "rename_credential": False,
                    "rename_active": False,
                    "rename_parent": False,
                }
    assert private.read_text() == "private"
    assert (other / "private").read_text() == "private"
    assert (outside / "new").read_text() == "yes"
    assert not (future / "created").exists()


async def test_exact_workload_arguments_never_become_host_shell_operations(
    tmp_path: Path, sandbox: Path
) -> None:
    values = (
        "中文 空格",
        "one'two",
        '"three"',
        "$(touch escaped)",
        "a; touch escaped",
        "line\nbreak",
        "",
    )
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            async with prepared.workspace.open_process(
                (
                    "python",
                    "-I",
                    "-B",
                    "-c",
                    "import json,sys;print(json.dumps(sys.argv[1:]))",
                    *values,
                )
            ) as process:
                output, error = await process.communicate()
                assert process.returncode == 0, error.decode()
                assert json.loads(output) == list(values)
    assert not (tmp_path / "escaped").exists()
