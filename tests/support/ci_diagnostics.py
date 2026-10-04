"""Opt-in, bounded asyncio evidence for the package coverage CI job.

The existing faulthandler deadline triggers one observation, not a test timeout.
Only coroutine test calls are observed; synchronous work and fixture phases
remain covered by pytest's thread dump. No task names, values or locals are read.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import ParamSpec, TypedDict, TypeVar

import pytest
from _pytest.faulthandler import get_stderr_fileno
from pytest_asyncio import is_async_test

_STDERR = pytest.StashKey[int]()
_MAX_TASKS = 16
_MAX_DEPTH = 24
_MAX_TEXT = 256
_MAX_BYTES = 32768
_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


class _TaskEvidence(TypedDict):
    identity: int
    test_task: bool
    done: bool
    cancelling: int
    await_chain: list[dict[str, str | int]]
    truncated: bool


class _Snapshot(TypedDict):
    event: str
    pid: int
    nodeid: str
    task_count: int
    tasks: list[_TaskEvidence]
    truncated: bool


def _await_chain(awaitable: object) -> tuple[list[dict[str, str | int]], bool]:
    frames: list[dict[str, str | int]] = []
    seen: set[int] = set()
    clipped = False
    while awaitable is not None and len(frames) < _MAX_DEPTH:
        if id(awaitable) in seen:
            break
        seen.add(id(awaitable))
        if inspect.iscoroutine(awaitable):
            frame = awaitable.cr_frame
            awaitable = awaitable.cr_await
        elif inspect.isgenerator(awaitable):
            frame = awaitable.gi_frame
            awaitable = awaitable.gi_yieldfrom
        else:
            frames.append({"kind": "opaque_awaitable"})
            awaitable = None
            break
        if frame is not None:
            clipped |= (
                len(frame.f_code.co_filename) > _MAX_TEXT
                or len(frame.f_code.co_name) > _MAX_TEXT
            )
            frames.append(
                {
                    "file": frame.f_code.co_filename[:_MAX_TEXT],
                    "function": frame.f_code.co_name[:_MAX_TEXT],
                    "line": frame.f_lineno,
                }
            )
    return frames, clipped or awaitable is not None


def _encode(snapshot: _Snapshot) -> bytes:
    while True:
        encoded = (json.dumps(snapshot, ensure_ascii=True) + "\n").encode("ascii")
        if len(encoded) <= _MAX_BYTES:
            return encoded
        snapshot["truncated"] = True
        if len(snapshot["tasks"]) > 1:
            snapshot["tasks"].pop()
        else:
            snapshot["tasks"][0]["await_chain"].pop()
            snapshot["tasks"][0]["truncated"] = True


def _write_snapshot(descriptor: int, nodeid: str, owner: asyncio.Task[object]) -> None:
    tasks = asyncio.all_tasks()
    ordered = [owner, *sorted(tasks - {owner}, key=id)[: _MAX_TASKS - 1]]
    evidence: list[_TaskEvidence] = []
    for task in ordered:
        frames, truncated = _await_chain(task.get_coro())
        evidence.append(
            {
                "identity": id(task),
                "test_task": task is owner,
                "done": task.done(),
                "cancelling": task.cancelling(),
                "await_chain": frames,
                "truncated": truncated,
            }
        )
    output = _encode(
        {
            "event": "ci-asyncio-snapshot",
            "pid": os.getpid(),
            "nodeid": nodeid[:_MAX_TEXT],
            "task_count": len(tasks),
            "tasks": evidence,
            "truncated": len(tasks) > len(ordered) or len(nodeid) > _MAX_TEXT,
        }
    )
    try:
        while output:
            output = output[os.write(descriptor, output) :]
    except OSError:
        return


def _observe(
    function: Callable[_Parameters, Awaitable[_Result]],
    *,
    nodeid: str,
    descriptor: int,
    timeout: float,
) -> Callable[_Parameters, Awaitable[_Result]]:
    @wraps(function)
    async def observed(
        *args: _Parameters.args, **kwargs: _Parameters.kwargs
    ) -> _Result:
        owner = asyncio.current_task()
        assert owner is not None
        timer = asyncio.get_running_loop().call_later(
            timeout, _write_snapshot, descriptor, nodeid, owner
        )
        try:
            return await function(*args, **kwargs)
        finally:
            timer.cancel()

    return observed


def pytest_configure(config: pytest.Config) -> None:
    """Own a stderr duplicate that is not redirected by per-test capture."""
    descriptor = os.dup(get_stderr_fileno())
    config.stash[_STDERR] = descriptor
    config.add_cleanup(partial(os.close, descriptor))


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Observe coroutine calls without introducing another task or runner."""
    for item in items:
        if not is_async_test(item) or not inspect.iscoroutinefunction(item.obj):
            continue
        timeout = float(item.config.getini("faulthandler_timeout") or 0)
        if timeout > 0:
            item.obj = _observe(
                item.obj,
                nodeid=item.nodeid,
                descriptor=item.config.stash[_STDERR],
                timeout=timeout,
            )
