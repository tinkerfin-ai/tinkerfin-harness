"""Deterministic contracts for opt-in CI hang evidence."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support import ci_diagnostics

pytest_plugins = ("pytester",)


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_observation_preserves_task_and_always_cancels_timer(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    loop = asyncio.get_running_loop()
    baseline = asyncio.all_tasks()
    entered = asyncio.Event()
    release = asyncio.Event()
    timer = Mock(spec=asyncio.TimerHandle)
    callbacks: list[Callable[[], None]] = []
    output: list[bytes] = []
    seen: list[asyncio.Task[object] | None] = []
    failure = ValueError("private-exception-content")

    def schedule(delay: float, callback: Callable[..., None], *args: object) -> Mock:
        assert delay == 60
        callbacks.append(lambda: callback(*args))
        return timer

    def write(descriptor: int, data: bytes) -> int:
        assert descriptor == 123
        output.append(data)
        return len(data)

    async def original(value: int) -> int:
        private_value = "credential-must-not-appear"
        seen.append(asyncio.current_task())
        entered.set()
        await release.wait()
        assert private_value
        if outcome == "error":
            raise failure
        return value

    monkeypatch.setattr(loop, "call_later", schedule)
    monkeypatch.setattr(ci_diagnostics.os, "write", write)
    observed = ci_diagnostics._observe(
        original, nodeid="test_example.py::test_wait", descriptor=123, timeout=60
    )
    assert inspect.signature(observed) == inspect.signature(original)
    task = asyncio.ensure_future(observed(42))
    task.set_name("secret-task-name")
    try:
        await entered.wait()
        assert seen == [task]
        assert len(callbacks) == 1
        callbacks[0]()
        payload = json.loads(b"".join(output))
        assert payload["nodeid"] == "test_example.py::test_wait"
        assert payload["pid"] == os.getpid()
        assert payload["tasks"][0]["identity"] == id(task)
        assert payload["tasks"][0]["test_task"] is True
        assert any(
            frame.get("function") == "original"
            for frame in payload["tasks"][0]["await_chain"]
        )
        assert all(
            secret not in b"".join(output)
            for secret in (
                b"credential-must-not-appear",
                b"secret-task-name",
                b"private-exception-content",
            )
        )
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "error":
                with pytest.raises(ValueError) as captured:
                    await task
                assert captured.value is failure
            else:
                assert await task == 42
        timer.cancel.assert_called_once_with()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert asyncio.all_tasks() == baseline


async def test_snapshot_bounds_task_count_chain_depth_and_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_diagnostics, "_MAX_TASKS", 1)
    monkeypatch.setattr(ci_diagnostics, "_MAX_DEPTH", 2)
    entered = asyncio.Event()
    release = asyncio.Event()
    output: list[bytes] = []

    def write(_descriptor: int, data: bytes) -> int:
        output.append(data)
        return len(data)

    async def wait() -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(ci_diagnostics.os, "write", write)
    task = asyncio.create_task(wait())
    try:
        await entered.wait()
        ci_diagnostics._write_snapshot(123, "n" * 1000, task)
        assert ci_diagnostics._MAX_BYTES == 4096
        assert len(b"".join(output)) <= 4096
        assert b"".join(output).startswith(b"\n{")
        assert b"".join(output).endswith(b"}\n")
        payload = json.loads(b"".join(output))
        assert len(payload["tasks"]) == 1
        assert len(payload["nodeid"]) == ci_diagnostics._MAX_TEXT
        assert len(payload["tasks"][0]["await_chain"]) == 2
        assert payload["truncated"] is True
        assert payload["tasks"][0]["truncated"] is True
        monkeypatch.setattr(ci_diagnostics, "_MAX_BYTES", 600)
        encoded = ci_diagnostics._encode(payload)
        assert len(encoded) <= 600
        assert json.loads(encoded)["tasks"][0]["identity"] == id(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_owned_stderr_descriptor_closes_at_session_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with (tmp_path / "stderr").open("wb") as destination:
        config = Mock(spec=pytest.Config)
        config.stash = pytest.Stash()
        monkeypatch.setattr(ci_diagnostics, "get_stderr_fileno", destination.fileno)
        ci_diagnostics.pytest_configure(config)
        descriptor = config.stash[ci_diagnostics._STDERR]
        assert descriptor != destination.fileno()
        cleanup = config.add_cleanup.call_args.args[0]
        cleanup()
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert not destination.closed


def test_xdist_snapshot_bypasses_test_capture(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))
    pytester.makeini("[pytest]\nasyncio_mode=auto\nfaulthandler_timeout=60")
    pytester.makeconftest(
        """
        import asyncio
        from unittest.mock import Mock
        import pytest

        @pytest.fixture
        async def fire_observation(monkeypatch):
            callbacks = []
            def schedule(delay, callback, *args):
                assert delay == 60
                callbacks.append(lambda: callback(*args))
                return Mock(spec=asyncio.TimerHandle)
            monkeypatch.setattr(asyncio.get_running_loop(), "call_later", schedule)
            return callbacks
        """
    )
    pytester.makepyfile(
        """
        import asyncio
        import pytest

        @pytest.mark.parametrize("value", [42])
        async def test_visible_before_completion(fire_observation, capfd, value):
            assert value == 42
            assert len(fire_observation) == 1
            fire_observation[0]()
            assert "ci-asyncio-snapshot" not in capfd.readouterr().err
            assert not asyncio.current_task().done()
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "tests.support.ci_diagnostics", "-n", "1", "-vv", "--capture=fd"
    )
    result.assert_outcomes(passed=1)
    snapshots = [
        json.loads(line)
        for line in result.errlines
        if line.startswith('{"event": "ci-asyncio-snapshot"')
    ]
    assert len(snapshots) == 1
    assert snapshots[0]["pid"] != os.getpid()
    assert snapshots[0]["nodeid"].endswith("::test_visible_before_completion[42]")
    assert snapshots[0]["tasks"][0]["test_task"] is True
