"""Owned observation cleanup failures remain visible after every stream outcome."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_workspace_watch import _manager, _prepare, _watch_world

from tinkerfin_notifications import (
    NotificationLimits,
    Notifications,
)
from tinkerfin_sandbox import (
    OpenSandboxBusyError,
    OpenSandboxError,
)


def _contains_failure(error: BaseException, expected: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is expected:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(
            cause
            for cause in (current.__cause__, current.__context__)
            if cause is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return False


@pytest.mark.parametrize("cancel_body", [False, True])
async def test_body_failure_or_repeated_cancellation_retains_cleanup_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_body: bool
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        body_entered = asyncio.Event()
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()
        body_failure = ValueError("Controlled body failure")
        cleanup_failure = RuntimeError("Controlled cleanup failure")

        async def fail_close() -> None:
            cleanup_entered.set()
            await cleanup_release.wait()
            raise cleanup_failure

        async def consume() -> None:
            async with manager.workspace("owner", workspace_key="project-a").watch():
                stream = await remote.watch_opened.get()
                monkeypatch.setattr(stream, "aclose", fail_close)
                body_entered.set()
                if not cancel_body:
                    raise body_failure
                await asyncio.Event().wait()

        consuming = world.spawn(consume())
        try:
            await body_entered.wait()
            if cancel_body:
                consuming.cancel()
            await cleanup_entered.wait()
            if cancel_body:
                consuming.cancel()
            cleanup_release.set()
            with pytest.raises(
                asyncio.CancelledError if cancel_body else ValueError
            ) as caught:
                await consuming
            if not cancel_body:
                assert caught.value is body_failure
            assert _contains_failure(caught.value, cleanup_failure)
            with pytest.raises(OpenSandboxError) as manager_failure:
                await manager.aclose()
            assert _contains_failure(manager_failure.value, cleanup_failure)
        finally:
            cleanup_release.set()
            consuming.cancel()
            await asyncio.gather(consuming, return_exceptions=True)
            try:
                await manager.aclose()
            except OpenSandboxError:
                pass
            world.managers.remove(manager)


async def test_failed_response_remains_owned_without_unbounded_new_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        Notifications(limits=NotificationLimits(max_subscriptions=1)) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, _state = await _manager(world, notifications)
        await _prepare(manager)
        failure = RuntimeError("Controlled retained cleanup failure")
        project = manager.workspace("owner", workspace_key="project-a")

        async def fail_close() -> None:
            raise failure

        async def attempt() -> None:
            with pytest.raises(OpenSandboxBusyError):
                async with project.watch():
                    pytest.fail("Failed owned resources count toward capacity")

        try:
            with pytest.raises(OpenSandboxError):
                async with project.watch():
                    stream = await remote.watch_opened.get()
                    monkeypatch.setattr(stream, "aclose", fail_close)
            await asyncio.gather(attempt(), attempt())
            assert len(remote.change_streams) == 1
            with pytest.raises(OpenSandboxError) as caught:
                await manager.aclose()
            assert _contains_failure(caught.value, failure)
        finally:
            try:
                await manager.aclose()
            except OpenSandboxError:
                pass
            world.managers.remove(manager)
