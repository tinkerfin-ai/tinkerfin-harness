"""Normal macOS execution keeps native services and explicit file protection."""

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Literal

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")


async def test_normal_execution_needs_no_srt_or_node_and_exposes_native_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discover = shutil.which

    def native_only(
        command: str, mode: int = os.F_OK | os.X_OK, path: str | None = None
    ):
        assert command == "/usr/bin/sandbox-exec"
        return discover(command, mode, path)

    monkeypatch.setattr(shutil, "which", native_only)
    source = (
        "import json, os; print(json.dumps({'path': os.environ['PATH'].split(':'), "
        "'proxies': {key: value for key, value in os.environ.items() "
        "if key.upper().endswith('_PROXY')}}))"
    )
    async with LocalWorkspace(
        tmp_path, sandbox_executable=tmp_path / "missing-runtime"
    ) as workspace:
        async with workspace.open() as session:
            result = await session.aexecute("python -I -c " + shlex.quote(source))
            assert result.exit_code == 0, result
            environment = json.loads(result.output)
            assert "/usr/local/bin" in environment["path"]
            assert "/opt/homebrew/bin" in environment["path"]
            assert environment["proxies"] == {}


async def test_normal_user_access_includes_ordinary_credential_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    paths = [
        home / directory / "owned-fixture"
        for directory in (".ssh", ".aws", "Library/Keychains")
    ]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text("ordinary user file")
    monkeypatch.setenv("HOME", str(home))
    async with LocalWorkspace(
        project, access=LocalAccess(filesystem="user")
    ) as workspace:
        async with workspace.open() as session:
            downloaded = await session.adownload_files([str(path) for path in paths])
            assert all(
                file.error is None and file.content == b"ordinary user file"
                for file in downloaded
            )
            uploaded = await session.aupload_files(
                [(str(path), b"updated") for path in paths]
            )
            assert all(file.error is None for file in uploaded)
    assert all(path.read_bytes() == b"updated" for path in paths)


@pytest.mark.parametrize("filesystem", ["workspace", "user"])
async def test_read_only_contents_cannot_be_rewritten_through_hardlinks_or_moves(
    tmp_path: Path, filesystem: Literal["workspace", "user"]
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    source = reference / "data.txt"
    source.write_text("immutable")
    script = """
import os
from pathlib import Path
try:
    os.link('reference/data.txt', 'alias.txt')
    Path('alias.txt').write_text('changed')
except OSError:
    pass
try:
    Path('reference').rename('moved')
except OSError:
    pass
"""
    async with LocalWorkspace(
        tmp_path,
        access=LocalAccess(filesystem=filesystem, read_only_paths=(reference,)),
    ) as workspace:
        async with workspace.open() as session:
            result = await session.aexecute("python -I -c " + shlex.quote(script))
            assert result.exit_code == 0, result
    assert source.read_text() == "immutable"
    assert not (tmp_path / "moved").exists()


async def test_normal_ipc_is_generic_and_cannot_control_the_external_group_anchor(
    tmp_path: Path,
) -> None:
    script = """
import ctypes, json, os
library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap = ctypes.c_uint.in_dll(library, 'bootstrap_port').value
port = ctypes.c_uint()
name = ('com.tinkerfin.owned-normal-probe.' + str(os.getpid())).encode()
registered = library.bootstrap_check_in(bootstrap, name, ctypes.byref(port))
os.kill(os.getpid(), 0)
try:
    os.kill(os.getppid(), 0)
except PermissionError:
    anchor_signal_denied = True
else:
    anchor_signal_denied = False
task = ctypes.c_uint.in_dll(library, 'mach_task_self_').value
parent_task = ctypes.c_uint()
parent_port_denied = library.task_for_pid(task, os.getppid(), ctypes.byref(parent_task)) != 0
print(json.dumps({'registered': registered, 'anchor_signal_denied': anchor_signal_denied, 'parent_port_denied': parent_port_denied}))
"""
    async with LocalWorkspace(tmp_path) as workspace:
        async with workspace.open() as session:
            result = await session.aexecute("python -I -c " + shlex.quote(script))
            assert result.exit_code == 0, result
            assert json.loads(result.output) == {
                "registered": 0,
                "anchor_signal_denied": True,
                "parent_port_denied": True,
            }
