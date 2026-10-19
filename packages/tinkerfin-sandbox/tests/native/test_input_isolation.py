"""Explicit input reads preserve every enclosing tree's write and IPC boundary."""

from __future__ import annotations

import json
import socket
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

import pytest

from tinkerfin_sandbox import LocalAccess, LocalWorkspace

PROBE = """
import json, pathlib, socket, sys
base = pathlib.Path(sys.argv[1])
project = base / 'projects/current'
inputs = base / 'input-parent/inputs'
allowed = inputs / 'scope-a'
empty = base / 'empty-parent/inputs'
private = base / 'private/data'
def connect(path):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(path))
print('ready', flush=True)
sys.stdin.buffer.read(1)
results = {}
for name, action in [
    ('input_read', lambda: (allowed / 'content').read_text()),
    ('second_read', lambda: (inputs / 'scope-c/content').read_text()),
    ('input_realpath', lambda: (allowed / 'content').resolve(strict=True)),
    ('input_alias', lambda: (base / 'scope-alias/content').read_text()),
    ('input_root_listing', lambda: list(inputs.iterdir())),
    ('input_sibling', lambda: (inputs / 'scope-b/content').read_text()),
    ('input_future', lambda: (inputs / 'future/content').read_text()),
    ('input_write_grant_read', lambda: (inputs / 'write-only/content').read_text()),
    ('input_write', lambda: (allowed / 'content').write_text('changed')),
    ('input_create', lambda: (allowed / 'created').write_text('changed')),
    ('input_delete', lambda: (allowed / 'content').unlink()),
    ('input_rename', lambda: allowed.rename(base / 'moved-input')),
    ('input_parent_rename', lambda: inputs.parent.rename(base / 'moved-parent')),
    ('input_socket', lambda: connect(allowed / 'service.sock')),
    ('input_socket_alias', lambda: connect(project / 'socket-alias')),
    ('sibling_alias', lambda: (project / 'private-alias').read_text()),
    ('nested_read', lambda: (allowed / 'nested/public/content').read_text()),
    ('nested_private', lambda: (allowed / 'nested/private').read_text()),
    ('nested_listing', lambda: list((allowed / 'nested').iterdir())),
    ('empty_read', lambda: (empty / 'content').read_text()),
    ('empty_metadata', lambda: empty.stat()),
    ('empty_future', lambda: (empty / 'future/content').read_text()),
    ('empty_write', lambda: (empty / 'created').write_text('changed')),
    ('empty_parent_rename', lambda: empty.parent.rename(base / 'moved-empty-parent')),
    ('protected_read', lambda: (private / 'content').read_text()),
    ('protected_metadata', lambda: private.stat()),
    ('protected_write', lambda: (private / 'content').write_text('changed')),
    ('protected_create', lambda: (private / 'created').write_text('changed')),
    ('protected_delete', lambda: (private / 'content').unlink()),
    ('protected_rename', lambda: private.parent.rename(base / 'moved-private')),
    ('protected_socket', lambda: connect(private / 'service.sock')),
    ('project_read', lambda: (project / 'content').read_text()),
    ('project_write', lambda: (project / 'created').write_text('permitted')),
    ('project_socket', lambda: connect(project / 'service.sock')),
    ('project_sibling', lambda: (base / 'projects/other/content').read_text()),
]:
    try:
        action()
        results[name] = True
    except OSError:
        results[name] = False
print(json.dumps(results), flush=True)
"""


@pytest.mark.parametrize("filesystem", ["workspace", "user"])
@pytest.mark.parametrize("network", ["normal", "restricted"])
async def test_external_read_grants_do_not_release_isolated_writes_or_connections(
    sandbox: Path,
    filesystem: Literal["workspace", "user"],
    network: Literal["normal", "restricted"],
) -> None:
    with tempfile.TemporaryDirectory(prefix="native-inputs-", dir="/tmp") as name:
        base = Path(name).resolve()
        project = base / "projects/current"
        inputs = base / "input-parent/inputs"
        allowed = inputs / "scope-a"
        empty = base / "empty-parent/inputs"
        private = base / "private/data"
        for directory in (
            project,
            project.parent / "other",
            allowed,
            inputs / "scope-b",
            inputs / "scope-c",
            inputs / "write-only",
            allowed / "nested/public",
            empty,
            private,
        ):
            directory.mkdir(parents=True)
            (directory / "content").write_text("original")
        (allowed / "nested/private").write_text("nested secret")
        (base / "input-alias").symlink_to(inputs, target_is_directory=True)
        (base / "scope-alias").symlink_to(allowed, target_is_directory=True)
        (project / "private-alias").symlink_to(inputs / "scope-b/content")
        (project / "socket-alias").symlink_to(allowed / "service.sock")
        with ExitStack() as resources:
            for directory in (project, allowed, private):
                listener = resources.enter_context(
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                )
                listener.bind(str(directory / "service.sock"))
                listener.listen(4)
            async with LocalWorkspace(
                project,
                sandbox_executable=sandbox,
                access=LocalAccess(
                    filesystem=filesystem,
                    network=network,
                    isolated_roots=(
                        project.parent,
                        base / "input-alias",
                        allowed / "nested",
                        empty,
                    ),
                    read_directories=(
                        base,
                        inputs,
                        base / "scope-alias",
                        inputs / "scope-c",
                        allowed / "nested/public",
                        private,
                    ),
                    write_directories=(allowed, inputs / "write-only", empty, private),
                    unix_socket_paths=(project, inputs, empty, private),
                    protected_paths=(private.parent,),
                ),
            ) as workspace:
                async with workspace.open() as session:
                    async with session.open_process(
                        ("python", "-I", "-c", PROBE, str(base))
                    ) as process:
                        assert await process.stdout.readline() == b"ready\n"
                        for root in (inputs, empty):
                            future = root / "future"
                            future.mkdir()
                            (future / "content").write_text("future secret")
                        output, error = await process.communicate(b"x")
                        assert process.returncode == 0, error.decode()
                        observed = json.loads(output)
                        permitted = {
                            "input_read",
                            "second_read",
                            "input_realpath",
                            "input_alias",
                            "nested_read",
                            "project_read",
                            "project_write",
                            "project_socket",
                        }
                        assert observed == {key: key in permitted for key in observed}
                    read = await session.aread(str(allowed / "content"))
                    assert read.error is None
                    denied = await session.awrite(str(allowed / "new"), "changed")
                    assert denied.error is not None
        assert (allowed / "content").read_text() == "original"
        assert (private / "content").read_text() == "original"
        assert (project / "created").read_text() == "permitted"


@pytest.mark.parametrize("invalid", ["root", "working_directory", "file"])
async def test_isolated_roots_require_distinct_non_root_directories(
    tmp_path: Path, sandbox: Path, invalid: str
) -> None:
    file = tmp_path / "file"
    file.write_text("fixture")
    selected = {"root": Path("/"), "working_directory": tmp_path, "file": file}[invalid]
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        access=LocalAccess(isolated_roots=(selected,)),
    ) as workspace:
        with pytest.raises(ValueError, match="isolated roots"):
            async with workspace.open():
                pytest.fail("an invalid isolated root was accepted")
