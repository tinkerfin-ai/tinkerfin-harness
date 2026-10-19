"""Native path resolution stays inside the active resource scope's kernel policy."""

import asyncio
from pathlib import Path

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_sandbox import LocalAccess, LocalError, LocalErrorCode, LocalWorkspace

IDENTITY = RunIdentity(namespace="local-paths", thread_id="thread", run_id="run")


async def test_independent_scope_resolves_paths_without_granting_later_access(
    tmp_path: Path, sandbox: Path
) -> None:
    directory = tmp_path / "project"
    nested = directory / "reports" / "nested"
    nested.mkdir(parents=True)
    report = directory / "reports" / "report.txt"
    report.write_text("public report")
    private = directory / "private"
    private.mkdir()
    secret = private / "secret.txt"
    secret.write_text("private marker")
    (directory / "link").symlink_to(nested, target_is_directory=True)
    async with LocalWorkspace(
        directory,
        sandbox_executable=sandbox,
        access=LocalAccess(protected_paths=(private,)),
    ) as workspace:
        async with workspace.open() as session:
            assert await session.resolve_path(None, operation="read") == str(directory)
            path = await session.resolve_path("link/../report.txt", operation="read")
            assert path == str(report)
            downloaded = await session.adownload_files([path])
            assert downloaded[0].error is None
            assert downloaded[0].content == b"public report"
            missing = directory / "reports" / "future" / "output.txt"
            assert await session.resolve_path(
                "link/../future/output.txt", operation="write"
            ) == str(missing)
            assert not missing.parent.exists()
            report.unlink()
            report.symlink_to(secret)
            denied = await session.adownload_files([path])
            assert denied[0].error == "permission_denied"
            assert denied[0].content is None
            with pytest.raises(ValueError, match="cannot be resolved"):
                await session.resolve_path("link/../report.txt", operation="read")
        with pytest.raises(LocalError) as closed:
            await session.resolve_path(None, operation="read")
        assert closed.value.code is LocalErrorCode.CLOSED
    assert secret.read_text() == "private marker"


async def test_prepared_paths_resolve_native_targets_without_creating_files(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    project = managed / "current/project"
    private = managed / "other/private.txt"
    user_home = tmp_path / "home"
    outside = tmp_path / "outside"
    for directory in (project, private.parent, user_home, outside / "nested"):
        directory.mkdir(parents=True)
    private.write_text("private")
    link = project / "link"
    link.symlink_to(outside / "nested", target_is_directory=True)
    (project / "private-link").symlink_to(private)
    monkeypatch.setenv("HOME", str(user_home))
    async with LocalWorkspace(
        project,
        sandbox_executable=sandbox,
        access=LocalAccess(filesystem="user", isolated_roots=(managed,)),
    ) as workspace:
        async with workspace.prepare(IDENTITY) as prepared:
            resolve = prepared.file_path_resolver
            assert resolve is not None
            assert await resolve(None, operation="read") == str(project)
            assert await resolve("link/../report.txt", operation="write") == str(
                outside / "report.txt"
            )
            assert await resolve("~/future/report.txt", operation="write") == str(
                user_home / "future/report.txt"
            )
            assert not (outside / "report.txt").exists()
            assert not (user_home / "future").exists()
            with pytest.raises(ValueError, match="cannot be resolved"):
                await resolve("private-link", operation="read")
            link.unlink()
            link.symlink_to(user_home, target_is_directory=True)
            assert await resolve("link/report.txt", operation="read") == str(
                user_home / "report.txt"
            )
        with pytest.raises(LocalError) as closed:
            await resolve(None, operation="read")
        assert closed.value.code is LocalErrorCode.CLOSED
    assert private.read_text() == "private"


async def test_path_resolution_cancellation_reaps_the_started_process(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned, release = asyncio.Event(), asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def gated_spawn(*arguments, **options):
        process = await create(*arguments, **options)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        async with workspace.open() as session:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
            try:
                async with asyncio.TaskGroup() as tasks:
                    operation = tasks.create_task(
                        session.resolve_path("report.txt", operation="write")
                    )
                    await spawned.wait()
                    operation.cancel()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await operation
            finally:
                release.set()
            assert processes and all(
                process.returncode is not None for process in processes
            )
            assert not (tmp_path / "report.txt").exists()
