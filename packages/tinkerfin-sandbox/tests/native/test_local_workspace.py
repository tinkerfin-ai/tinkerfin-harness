"""Native filesystem, process ownership, and Runtime workspace contracts."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import (
    LocalAccess,
    LocalError,
    LocalErrorCode,
    LocalLimits,
    LocalProcess,
    LocalWorkspace,
)

IDENTITY = RunIdentity(namespace="local-test", thread_id="thread", run_id="run")

BUFFERED_READER = (
    "import os, signal, sys\n"
    "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})\n"
    "print(os.getpid(), flush=True)\n"
    "signal.sigwait({signal.SIGUSR1})\n"
    "data = sys.stdin.buffer.read()\n"
    "print(len(data), flush=True)\n"
)


@pytest.fixture
async def workspace(tmp_path: Path, sandbox: Path) -> AsyncIterator[LocalWorkspace]:
    directory = tmp_path / "project"
    directory.mkdir()
    async with LocalWorkspace(directory, sandbox_executable=sandbox) as value:
        yield value


async def test_files_commands_and_binary_transfer_share_one_directory(
    workspace: LocalWorkspace,
) -> None:
    async with workspace.prepare(IDENTITY) as prepared:
        session = prepared.workspace
        assert (await session.awrite("notes.txt", "first\nsecond\n")).error is None
        assert (await session.aedit("notes.txt", "first", "changed")).occurrences == 1
        read = await session.aread("notes.txt", offset=0, limit=1)
        assert read.file_data is not None and "changed" in read.file_data["content"]
        assert (await session.agrep("second", max_count=1)).matches
        listed = await session.als(".")
        assert listed.entries and listed.entries[0]["path"] == str(
            session.directory / "notes.txt"
        )
        assert (await session.aglob("*.txt")).matches
        assert (await session.aexecute("cat notes.txt")).output == "changed\nsecond\n"
        content = b"\x00\xffbinary"
        assert (await session.aupload_files([("binary.bin", content)]))[0].error is None
        assert (await session.adownload_files(["binary.bin"]))[0].content == content
        assert (await session.adelete("notes.txt")).error is None
    with pytest.raises(LocalError) as error:
        await session.aexecute("touch after-close")
    assert error.value.code is LocalErrorCode.CLOSED
    assert not (session.directory / "after-close").exists()
    assert (session.directory / "binary.bin").read_bytes() == content


async def test_native_policy_blocks_ungranted_paths_and_symlink_targets(
    tmp_path: Path,
    workspace: LocalWorkspace,
) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("private-fixture")
    async with workspace.prepare(IDENTITY) as prepared:
        session = prepared.workspace
        (session.directory / "outside-link").symlink_to(secret)
        for argument in (str(secret), "outside-link"):
            read = await session.aexecute("cat " + shlex.quote(argument))
            assert read.exit_code != 0 and "private-fixture" not in read.output
        write = await session.aexecute("printf changed > " + shlex.quote(str(secret)))
        assert write.exit_code != 0 and secret.read_text() == "private-fixture"
        assert (await session.adownload_files(["outside-link"]))[
            0
        ].error == "permission_denied"


async def test_additional_read_grant_does_not_allow_writes(
    tmp_path: Path, sandbox: Path
) -> None:
    project = tmp_path / "project"
    references = tmp_path / "references"
    project.mkdir()
    references.mkdir()
    source = references / "input.txt"
    source.write_text("reference")
    async with LocalWorkspace(
        project,
        sandbox_executable=sandbox,
        access=LocalAccess(read_directories=(references,)),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            session = prepared.workspace
            assert (
                await session.aexecute("cat " + shlex.quote(str(source)))
            ).output == "reference"
            assert (
                await session.aexecute("printf changed > " + shlex.quote(str(source)))
            ).exit_code != 0
    assert source.read_text() == "reference"


async def test_reference_paths_enforce_native_read_only_access(
    tmp_path: Path,
    sandbox: Path,
) -> None:
    project = tmp_path / "project"
    references = project / "authorized references"
    references.mkdir(parents=True)
    source = references / "guide.md"
    source.write_text("reference")
    async with LocalWorkspace(
        project,
        sandbox_executable=sandbox,
        access=LocalAccess(read_only_paths=(references,)),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            session = prepared.workspace
            read = await session.aread(str(source))
            assert (
                read.file_data is not None and "reference" in read.file_data["content"]
            )
            (downloaded,) = await session.adownload_files([str(source)])
            assert downloaded.path == str(source) and downloaded.content == b"reference"
            assert session.to_shell_path(str(source)) == str(source)
            assert (await session.aglob("*.md", str(references))).matches
            assert (await session.agrep("reference", str(references))).matches
            assert (await session.awrite(str(source), "changed")).error is not None
            (uploaded,) = await session.aupload_files([(str(source), b"changed")])
            assert uploaded.error is not None
            write = await session.aexecute(
                "printf changed > " + shlex.quote(session.to_shell_path(str(source)))
            )
            assert write.exit_code != 0
            assert (await session.awrite("output.md", "allowed")).error is None
            with pytest.raises(ValueError):
                session.to_shell_path("bad\0path")
        with pytest.raises(LocalError):
            await session.adownload_files([str(source)])
    assert source.read_text() == "reference"
    assert (project / "output.md").read_text() == "allowed"


async def test_workload_cannot_modify_its_trusted_sandbox_installation(
    tmp_path: Path, sandbox: Path
) -> None:
    project = tmp_path / "project"
    installation = project / "runtime"
    installation.mkdir(parents=True)
    modules = installation / "node_modules"
    await asyncio.to_thread(
        shutil.copytree, sandbox.resolve().parents[3], modules, symlinks=True
    )
    launcher = modules / "@anthropic-ai/sandbox-runtime/dist/cli.js"
    dependency = modules / "dependency.js"
    dependency.write_text("trusted")
    async with LocalWorkspace(
        project, sandbox_executable=launcher, access=LocalAccess(network="restricted")
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            changed = await prepared.workspace.aexecute(
                "printf changed > runtime/node_modules/dependency.js"
            )
            assert changed.exit_code != 0
            assert (
                await prepared.workspace.awrite("output.md", "allowed")
            ).error is None
    assert dependency.read_text() == "trusted"


async def test_native_absolute_relative_and_home_paths_share_tool_and_command_targets(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_home, outside = (
        tmp_path / name for name in ("project", "home", "其他目录")
    )
    for directory in (project, user_home, outside):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    async with LocalWorkspace(
        project,
        sandbox_executable=sandbox,
        access=LocalAccess(write_directories=(user_home, outside)),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            session = prepared.workspace
            for path, expected in (
                ("report.txt", project / "report.txt"),
                ("~/report.txt", user_home / "report.txt"),
                (str(outside / "report.txt"), outside / "report.txt"),
            ):
                assert session.to_shell_path(path) == str(expected)
                assert (await session.awrite(path, "original")).error is None
                assert (
                    await session.aedit(path, "original", "changed")
                ).occurrences == 1
                read = await session.aread(path)
                assert (
                    read.file_data is not None
                    and "changed" in read.file_data["content"]
                )
                assert (await session.aglob("*.txt", str(expected.parent))).matches
                assert (await session.agrep("changed", str(expected.parent))).matches
                assert (
                    await session.aexecute("cat " + shlex.quote(str(expected)))
                ).output == "changed"
                assert (await session.adownload_files([path]))[0].content == b"changed"
                assert (await session.aupload_files([(path, b"\0\xffbinary")]))[
                    0
                ].error is None
                assert expected.read_bytes() == b"\0\xffbinary"
                assert (await session.adelete(path)).error is None
                assert not expected.exists()
            assert (
                await session.aexecute("printf native > ~/shell.txt")
            ).exit_code == 0
            assert (user_home / "shell.txt").read_text() == "native"


async def test_symlink_parent_components_preserve_native_file_and_command_targets(
    tmp_path: Path, sandbox: Path
) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    (outside / "nested").mkdir(parents=True)
    (project / "link").symlink_to(outside / "nested", target_is_directory=True)
    (project / "report.txt").write_text("project")
    target = outside / "report.txt"
    target.write_text("native")
    async with LocalWorkspace(
        project,
        sandbox_executable=sandbox,
        access=LocalAccess(filesystem="user"),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            session = prepared.workspace
            path = "link/../report.txt"
            assert (await session.adownload_files([path]))[0].content == b"native"
            assert (
                await session.aexecute(
                    "cat " + shlex.quote(session.to_shell_path(path))
                )
            ).output == "native"
            assert (await session.awrite(path, "updated")).error is None
            assert target.read_text() == "updated"
            assert (await session.aupload_files([(path, b"\0\xffbinary")]))[
                0
            ].error is None
    assert target.read_bytes() == b"\0\xffbinary"
    assert (project / "report.txt").read_text() == "project"


async def test_workload_does_not_inherit_credentials(
    workspace: LocalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINKERFIN_TEST_HOST_SECRET", "must-remain-in-host")
    async with workspace.prepare(IDENTITY) as prepared:
        response = await prepared.workspace.aexecute(
            'printf "%s" "$TINKERFIN_TEST_HOST_SECRET"'
        )
        assert response.exit_code == 0 and response.output == ""


async def test_managed_python_has_application_packages_and_separate_task_dependencies(
    workspace: LocalWorkspace,
) -> None:
    script = """
import json, pathlib, subprocess, sys, sysconfig
import pydantic
from tinkerfin_sandbox import LocalWorkspace
config = pathlib.Path(sys.prefix) / 'pyvenv.cfg'
try:
    config.write_text('untrusted')
except PermissionError:
    pass
else:
    raise AssertionError('managed Python configuration must be read-only')
subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '.task-env'], check=True)
python = str(pathlib.Path('.task-env/bin/python').absolute())
subprocess.run([python, '-c', "import pathlib, sysconfig; (pathlib.Path(sysconfig.get_path('purelib')) / 'task_dependency.py').write_text('value = 7')"], check=True)
subprocess.run([python, '-c', 'import task_dependency; assert task_dependency.value == 7'], check=True)
try:
    import task_dependency
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('task dependencies must not enter the managed environment')
print(json.dumps({'managed': sys.prefix, 'task': python}))
"""
    async with workspace.prepare(IDENTITY) as prepared:
        response = await prepared.workspace.aexecute("python -c " + shlex.quote(script))
        assert response.exit_code == 0, response.output
        result = json.loads(response.output)
        assert result["managed"] != str(prepared.workspace.directory)
        assert Path(result["task"]).is_relative_to(prepared.workspace.directory)


async def test_bound_commands_and_process_capacity_are_enforced(
    tmp_path: Path, sandbox: Path
) -> None:
    async with LocalWorkspace(
        tmp_path,
        sandbox_executable=sandbox,
        commands={"announce": "/usr/bin/printf"},
        limits=LocalLimits(processes=1, command_timeout=30),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            session = prepared.workspace
            async with session.open_process(
                ("/bin/sh", "-c", "echo ready; read value")
            ) as process:
                assert await process.stdout.readline() == b"ready\n"
                with pytest.raises(LocalError) as limit:
                    await session.aexecute("announce unavailable")
                assert limit.value.code == LocalErrorCode.LIMIT
            assert (await session.aexecute("announce available")).output == "available"
            with pytest.raises(ValueError, match="workspace command limit"):
                await session.aexecute("announce unavailable", timeout=31)


async def test_output_limit_stops_the_owned_process(
    tmp_path: Path, sandbox: Path
) -> None:
    async with LocalWorkspace(
        tmp_path, sandbox_executable=sandbox, limits=LocalLimits(output_bytes=1024)
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            with pytest.raises(LocalError) as failure:
                await prepared.workspace.aexecute("python -I -c 'print(\"x\" * 4096)' ")
            assert failure.value.code is LocalErrorCode.LIMIT
            assert (
                await prepared.workspace.aexecute("printf healthy")
            ).output == "healthy"


async def test_cancelled_service_is_reaped_without_waiting_for_a_timer(
    workspace: LocalWorkspace,
) -> None:
    ready = asyncio.Event()
    processes: list[LocalProcess] = []
    async with workspace.prepare(IDENTITY) as prepared:

        async def consume() -> None:
            async with prepared.workspace.open_process(
                ("/bin/sh", "-c", "printf 'ready\\n'; read answer")
            ) as process:
                processes.append(process)
                assert process.stdout is not None
                assert await process.stdout.readline() == b"ready\n"
                ready.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(consume())
        await ready.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert processes[0].returncode is not None
        assert (await prepared.workspace.aexecute("printf ready")).output == "ready"


async def test_workspace_close_revokes_an_active_service(
    workspace: LocalWorkspace,
) -> None:
    async with workspace.prepare(IDENTITY) as prepared:
        async with prepared.workspace.open_process(
            ("/bin/sh", "-c", "printf 'ready\\n'; read answer")
        ) as process:
            assert process.stdout is not None
            assert await process.stdout.readline() == b"ready\n"
            await workspace.aclose()
            assert process.returncode is not None
            with pytest.raises(LocalError) as error:
                await prepared.workspace.als("/")
            assert error.value.code is LocalErrorCode.CLOSED


async def test_argv_is_not_interpreted_by_the_host_shell(
    workspace: LocalWorkspace,
) -> None:
    async with workspace.prepare(IDENTITY) as prepared:
        value = "$(touch unexpected); ' spaced value"
        async with prepared.workspace.open_process(
            ("/usr/bin/printf", "%s", value)
        ) as process:
            output, _error = await process.communicate()
            assert output.decode() == value
        assert not (prepared.workspace.directory / "unexpected").exists()


async def test_process_scope_keeps_its_group_identity_after_command_exit(
    workspace: LocalWorkspace,
) -> None:
    async with workspace.prepare(IDENTITY) as prepared:
        async with prepared.workspace.open_process(
            ("/usr/bin/printf", "done")
        ) as process:
            output, _error = await process.communicate()
            assert output == b"done" and process.returncode == 0
            assert os.getpgid(process.pid) == process.pid
            denied = await prepared.workspace.aexecute(f"/bin/kill -9 {process.pid}")
            assert denied.exit_code != 0
            assert os.getpgid(process.pid) == process.pid


async def test_process_scope_accepts_closed_buffered_stdin(
    workspace: LocalWorkspace,
) -> None:
    async with workspace.prepare(IDENTITY) as prepared:
        async with prepared.workspace.open_process(
            (
                str(Path(sys.executable).resolve(strict=True)),
                "-I",
                "-B",
                "-c",
                BUFFERED_READER,
            )
        ) as process:
            child_pid = int(await process.stdout.readline())
            connection = process.stdin.get_extra_info("socket")
            assert connection is not None
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            capacity = connection.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            payload = b"x" * (capacity * 4)
            process.stdin.write(payload)
            assert process.stdin.transport.get_write_buffer_size() > 0
            process.stdin.close()
            os.kill(child_pid, signal.SIGUSR1)
            assert int(await process.stdout.readline()) == len(payload)
            assert await process.stderr.read() == b""
            assert await process.wait() == 0
            await process.stdin.wait_closed()


async def test_cancellation_discards_unconsumed_closing_stdin(
    workspace: LocalWorkspace,
) -> None:
    buffered = asyncio.Event()
    processes: list[LocalProcess] = []
    async with workspace.prepare(IDENTITY) as prepared:

        async def consume() -> None:
            async with prepared.workspace.open_process(
                (
                    str(Path(sys.executable).resolve(strict=True)),
                    "-I",
                    "-B",
                    "-c",
                    BUFFERED_READER,
                )
            ) as process:
                processes.append(process)
                assert int(await process.stdout.readline()) > 0
                connection = process.stdin.get_extra_info("socket")
                assert connection is not None
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                capacity = connection.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
                process.stdin.write(b"x" * (capacity * 4))
                process.stdin.close()
                assert process.stdin.transport.get_write_buffer_size() > 0
                buffered.set()
                await asyncio.Event().wait()

        async with asyncio.TaskGroup() as group:
            running = group.create_task(consume())
            await buffered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        assert processes[0].returncode is not None
        assert (
            await prepared.workspace.aexecute("printf recovered")
        ).output == "recovered"


async def test_missing_runtime_fails_before_command_admission(tmp_path: Path) -> None:
    with pytest.raises(LocalError) as failure:
        async with LocalWorkspace(
            tmp_path,
            sandbox_executable=tmp_path / "missing",
            access=LocalAccess(network="restricted"),
        ):
            pytest.fail("missing sandbox must never admit execution")
    assert failure.value.code is LocalErrorCode.UNAVAILABLE


async def test_native_access_never_falls_back_on_unsupported_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(LocalError) as failure:
        async with LocalWorkspace(tmp_path):
            pytest.fail("unsupported platforms cannot admit native execution")
    assert failure.value.code is LocalErrorCode.UNAVAILABLE


async def test_literal_directory_names_do_not_grant_glob_matches(
    tmp_path: Path, sandbox: Path
) -> None:
    selected = tmp_path / "[ab]"
    outside = tmp_path / "a"
    selected.mkdir()
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("not-granted")
    async with LocalWorkspace(selected, sandbox_executable=sandbox) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            assert (
                await prepared.workspace.aexecute("printf valid > result")
            ).exit_code == 0
            response = await prepared.workspace.aexecute(
                "cat " + shlex.quote(str(secret))
            )
            assert response.exit_code != 0 and "not-granted" not in response.output
    assert (selected / "result").read_text() == "valid"


async def test_controlled_deadline_preserves_timeout_and_closes_execution(
    workspace: LocalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Deadline:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.owner: asyncio.Task[object] | None = None
            self.expired = False

        async def __aenter__(self) -> None:
            self.owner = asyncio.current_task()
            self.entered.set()

        async def __aexit__(
            self, kind: object, error: BaseException | None, traceback: object
        ) -> None:
            if self.expired and isinstance(error, asyncio.CancelledError):
                raise TimeoutError from error

        def expire(self) -> None:
            self.expired = True
            assert self.owner is not None
            self.owner.cancel()

    deadline = Deadline()
    async with workspace.prepare(IDENTITY) as prepared:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "timeout", lambda _seconds: deadline)
            running = asyncio.create_task(prepared.workspace.aexecute("cat"))
            await deadline.entered.wait()
            deadline.expire()
            with pytest.raises(LocalError) as failure:
                await running
        assert failure.value.code is LocalErrorCode.TIMEOUT
        assert (
            await prepared.workspace.aexecute("printf recovered")
        ).output == "recovered"


async def test_cancellation_during_creation_retains_and_reaps_the_process(
    workspace: LocalWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    created: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(
        *arguments: str, **options: Any
    ) -> asyncio.subprocess.Process:
        entered.set()
        await release.wait()
        process = await spawn(*arguments, **options)
        created.append(process)
        return process

    async with workspace.prepare(IDENTITY) as prepared:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
            running = asyncio.create_task(prepared.workspace.aexecute("read value"))
            await entered.wait()
            running.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
    assert len(created) == 1 and created[0].returncode is not None


async def test_file_reads_respect_the_binary_and_text_byte_limit(
    tmp_path: Path, sandbox: Path
) -> None:
    (tmp_path / "large.txt").write_text("x" * 16)
    async with LocalWorkspace(
        tmp_path, sandbox_executable=sandbox, limits=LocalLimits(file_bytes=8)
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            assert (await prepared.workspace.aread("large.txt")).error is not None
            with pytest.raises(LocalError) as failure:
                await prepared.workspace.aupload_files([("copy.bin", b"x" * 9)])
            assert failure.value.code is LocalErrorCode.LIMIT
    assert not (tmp_path / "copy.bin").exists()


@pytest.mark.parametrize(
    "domain",
    [
        "*",
        "*.com",
        "127.0.0.1",
        "10.0.0.1:443",
        "[::1]",
        "localhost",
        "*.localhost",
        "127.1",
        "2130706433",
        "0x7f.0.0.1",
        "0177.0.0.1",
    ],
)
def test_domain_grants_cannot_bypass_private_address_protection(domain: str) -> None:
    with pytest.raises(ValueError):
        LocalAccess(network="restricted", allowed_domains=(domain,))
