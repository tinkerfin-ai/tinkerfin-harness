"""Independent native borrows retain ownership through admission and closing."""

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest

from tinkerfin_sandbox import LocalError, LocalErrorCode, LocalSession, LocalWorkspace


async def test_owned_directory_failure_uses_the_public_error_family(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        failure = PermissionError("private temporary directory")

        def denied(*, prefix: str) -> str:
            raise failure

        monkeypatch.setattr(tempfile, "mkdtemp", denied)
        with pytest.raises(LocalError) as error:
            async with workspace.open():
                pytest.fail("failed allocation admitted a borrow")
        assert error.value.code == LocalErrorCode.UNAVAILABLE
        assert error.value.cause is failure and error.value.__cause__ is failure


async def test_independent_access_survives_another_borrow(
    tmp_path: Path, sandbox: Path
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        async with workspace.open() as first:
            async with workspace.open() as second:
                assert first.id != second.id
                assert (await second.awrite("shared.txt", "retained")).error is None
            result = await first.aread("shared.txt")
            assert result.file_data is not None
            assert result.file_data["content"] == "retained"
        with pytest.raises(LocalError) as error:
            await first.aexecute("true")
        assert error.value.code == LocalErrorCode.CLOSED
    assert (tmp_path / "shared.txt").read_text() == "retained"


async def test_manager_close_joins_admitted_directory_creation(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        entered, closing_started = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        allocated: list[Path] = []
        mkdtemp = tempfile.mkdtemp

        def allocate(*args, **kwargs):
            result = mkdtemp(*args, **kwargs)
            allocated.append(Path(result))
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return result

        monkeypatch.setattr(tempfile, "mkdtemp", allocate)

        async def borrow() -> None:
            async with workspace.open():
                pytest.fail("a closing workspace admitted a new borrow")

        async def close() -> None:
            closing_started.set()
            await workspace.aclose()

        opening = asyncio.create_task(borrow())
        closing: asyncio.Task[None] | None = None
        try:
            await entered.wait()
            closing = asyncio.create_task(close())
            await closing_started.wait()
            assert not closing.done()
            release.set()
            await closing
            with pytest.raises(LocalError) as error:
                await opening
            assert error.value.code == LocalErrorCode.CLOSED
            assert allocated and all(not path.exists() for path in allocated)
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)


async def test_cancelled_close_waits_for_cleanup_and_remains_cancelled(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        async with workspace.open() as session:
            entered, release = asyncio.Event(), asyncio.Event()
            original = LocalSession.aclose

            async def close_session(value: LocalSession) -> None:
                entered.set()
                await release.wait()
                await original(value)

            monkeypatch.setattr(LocalSession, "aclose", close_session)
            closing = asyncio.create_task(workspace.aclose())
            try:
                await entered.wait()
                closing.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await closing
                assert closing.cancelled()
                await workspace.aclose()
                with pytest.raises(LocalError) as error:
                    session.to_shell_path("file.txt")
                assert error.value.code == LocalErrorCode.CLOSED
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)


async def test_close_attempts_every_borrow_and_wraps_cleanup_failures(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = await LocalWorkspace(tmp_path, sandbox_executable=sandbox).__aenter__()
    contexts = [workspace.open(), workspace.open()]
    sessions = [await context.__aenter__() for context in contexts]
    closed: set[str] = set()
    original = LocalSession.aclose

    async def close_session(session: LocalSession) -> None:
        await original(session)
        closed.add(session.id)
        raise OSError("injected resource failure")

    monkeypatch.setattr(LocalSession, "aclose", close_session)
    try:
        with pytest.raises(LocalError) as error:
            await workspace.aclose()
        assert error.value.code == LocalErrorCode.CLEANUP
        assert error.value.cause is error.value.__cause__
        assert closed == {session.id for session in sessions}
    finally:
        for context in contexts:
            with pytest.raises(LocalError):
                await context.__aexit__(None, None, None)
        with pytest.raises(LocalError):
            await workspace.aclose()


async def test_cancelled_close_retains_cancellation_when_cleanup_fails(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = await LocalWorkspace(tmp_path, sandbox_executable=sandbox).__aenter__()
    context = workspace.open()
    await context.__aenter__()
    entered, release = asyncio.Event(), asyncio.Event()
    original = LocalSession.aclose

    async def close_session(session: LocalSession) -> None:
        await original(session)
        entered.set()
        await release.wait()
        raise OSError("private cleanup fixture")

    monkeypatch.setattr(LocalSession, "aclose", close_session)
    closing = asyncio.create_task(workspace.aclose())
    try:
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await closing
        assert closing.cancelled()
        assert isinstance(error.value.__cause__, LocalError)
        assert "private cleanup fixture" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        with pytest.raises(LocalError):
            await context.__aexit__(None, None, None)
        with pytest.raises(LocalError):
            await workspace.aclose()


async def test_body_cancellation_survives_borrow_cleanup_failure(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        entered = asyncio.Event()
        original = LocalSession.aclose

        async def close_session(session: LocalSession) -> None:
            await original(session)
            raise OSError("private cleanup fixture")

        monkeypatch.setattr(LocalSession, "aclose", close_session)

        async def run() -> None:
            async with workspace.open():
                entered.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(run())
        try:
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError) as error:
                await running
            assert running.cancelled()
            assert isinstance(error.value.__cause__, LocalError)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_cancelled_admission_does_not_log_a_later_private_failure(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with LocalWorkspace(tmp_path, sandbox_executable=sandbox) as workspace:
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def denied(*, prefix: str) -> str:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            raise PermissionError("private admission fixture")

        monkeypatch.setattr(tempfile, "mkdtemp", denied)

        async def run() -> None:
            async with workspace.open():
                pytest.fail("failed admission exposed a session")

        opening = asyncio.create_task(run())
        try:
            await entered.wait()
            opening.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert opening.cancelled()
            assert "private admission fixture" not in caplog.text
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
