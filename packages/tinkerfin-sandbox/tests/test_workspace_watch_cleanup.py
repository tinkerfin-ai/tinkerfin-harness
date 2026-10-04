"""Owned observation cleanup failures remain visible after every stream outcome."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_workspace_watch import _manager, _prepare, _watch_world

from tinkerfin_notifications import (
    MemoryBackend,
    Notification,
    NotificationLimits,
    Notifications,
    NotificationScope,
    ResyncRequired,
)
from tinkerfin_sandbox import (
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
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


@pytest.mark.parametrize("remote_closed", [False, True])
async def test_watch_exit_reports_response_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_closed: bool
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        failure = RuntimeError("Controlled response cleanup failure")
        cleanup_calls = 0

        async def fail_close() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            raise failure

        try:
            with pytest.raises(OpenSandboxError) as caught:
                async with manager.workspace(
                    "owner", workspace_key="project-a"
                ).watch() as changes:
                    stream = await remote.watch_opened.get()
                    monkeypatch.setattr(stream, "aclose", fail_close)
                    await stream.waiting.wait()
                    if remote_closed:
                        await stream.pending.put({"type": "closed"})
                        assert await anext(changes) == ResyncRequired("disconnected")
                        with pytest.raises(StopAsyncIteration):
                            await anext(changes)
            assert _contains_failure(caught.value, failure)
            assert cleanup_calls == 1
            assert not stream.closed.is_set()
        finally:
            try:
                await manager.aclose()
            except OpenSandboxError:
                pass
            world.managers.remove(manager)


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


async def test_failed_admission_keeps_its_original_cause_and_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, _state = await _manager(world)
        await _prepare(manager)
        remote.watch_ready.clear()
        failure = RuntimeError("Controlled unready response cleanup failure")

        async def fail_close() -> None:
            raise failure

        async def enter() -> None:
            async with manager.workspace("owner", workspace_key="project-a").watch():
                pytest.fail("Invalid source identities cannot enter a watch")

        entering = world.spawn(enter())
        try:
            stream = await remote.watch_opened.get()
            stream.source = "invalid-source-identity"
            monkeypatch.setattr(stream, "aclose", fail_close)
            remote.watch_ready.set()
            with pytest.raises(OpenSandboxBackendProtocolError) as caught:
                await entering
            assert isinstance(caught.value.cause, ValidationError)
            assert _contains_failure(caught.value, failure)
        finally:
            remote.watch_ready.set()
            entering.cancel()
            await asyncio.gather(entering, return_exceptions=True)
            try:
                await manager.aclose()
            except OpenSandboxError:
                pass
            world.managers.remove(manager)


@pytest.mark.parametrize("borrowed", [False, True])
async def test_manager_reports_all_cleanup_failures_after_closing_other_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, borrowed: bool
) -> None:
    backend = MemoryBackend()
    closed_backends: list[MemoryBackend] = []
    close_memory = MemoryBackend.aclose

    async def track_memory_close(service_backend: MemoryBackend) -> None:
        await close_memory(service_backend)
        closed_backends.append(service_backend)

    monkeypatch.setattr(MemoryBackend, "aclose", track_memory_close)
    async with (
        Notifications(backend=backend) as notifications,
        _watch_world(tmp_path) as (world, remote),
    ):
        manager, state = await _manager(world, notifications if borrowed else None)
        for project in ("first", "second", "healthy"):
            await _prepare(manager, project)
        handle = await manager.get("raw-owner")
        contexts = [
            manager.workspace("owner", workspace_key=project).watch()
            for project in ("first", "second", "healthy")
        ]
        failures = (
            RuntimeError("First cleanup failure"),
            RuntimeError("Second cleanup failure"),
        )
        closed: set[str] = set()
        client = world.clients[0]
        close_client, close_state = client.aclose, state.aclose

        async def track_client_close() -> None:
            await close_client()
            closed.add("client")

        async def track_state_close() -> None:
            await close_state()
            closed.add("state")

        async def fail_first() -> None:
            closed.add("first_attempt")
            raise failures[0]

        async def fail_second() -> None:
            closed.add("second_attempt")
            raise failures[1]

        monkeypatch.setattr(client, "aclose", track_client_close)
        monkeypatch.setattr(state, "aclose", track_state_close)
        try:
            for context in contexts:
                await context.__aenter__()
            first_stream = await remote.watch_opened.get()
            second_stream = await remote.watch_opened.get()
            healthy_stream = await remote.watch_opened.get()
            monkeypatch.setattr(first_stream, "aclose", fail_first)
            monkeypatch.setattr(second_stream, "aclose", fail_second)
            with pytest.raises(OpenSandboxError) as caught:
                await manager.aclose()
            assert all(_contains_failure(caught.value, failure) for failure in failures)
            assert closed == {"client", "state", "first_attempt", "second_attempt"}
            assert healthy_stream.closed.is_set() and handle.is_closed
            assert len(closed_backends) == (0 if borrowed else 1)
            assert backend not in closed_backends
            with pytest.raises(OpenSandboxError) as repeated:
                await manager.aclose()
            assert repeated.value is caught.value
            notification = Notification(
                scope=NotificationScope("borrowed"), topic="usable", key="one"
            )
            async with notifications.subscribe() as subscription:
                await notifications.publish(notification)
                assert await anext(subscription) == notification
        finally:
            for context in reversed(contexts):
                try:
                    await context.__aexit__(None, None, None)
                except OpenSandboxError:
                    pass
            try:
                await manager.aclose()
            except OpenSandboxError:
                pass
            world.managers.remove(manager)


async def test_manager_keeps_watch_failure_when_client_cleanup_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _watch_world(tmp_path) as (world, remote):
        manager, state = await _manager(world)
        await _prepare(manager)
        client = world.clients[0]
        close_client, close_state = client.aclose, state.aclose
        watch_failure = RuntimeError("Controlled sticky watch failure")
        client_failure = OpenSandboxBackendError("Controlled client close failure")
        watch_closes = 0
        client_closes = 0
        state_closes = 0
        context = manager.workspace("owner", workspace_key="project-a").watch()

        async def fail_watch() -> None:
            nonlocal watch_closes
            watch_closes += 1
            raise watch_failure

        async def fail_client_once() -> None:
            nonlocal client_closes
            client_closes += 1
            if client_closes == 1:
                raise client_failure
            await close_client()

        async def observe_state_close() -> None:
            nonlocal state_closes
            state_closes += 1
            await close_state()

        monkeypatch.setattr(client, "aclose", fail_client_once)
        monkeypatch.setattr(state, "aclose", observe_state_close)
        try:
            await context.__aenter__()
            stream = await remote.watch_opened.get()
            monkeypatch.setattr(stream, "aclose", fail_watch)
            with pytest.raises(OpenSandboxError) as first:
                await manager.aclose()
            assert _contains_failure(first.value, watch_failure)
            assert _contains_failure(first.value, client_failure)
            assert watch_closes == client_closes == state_closes == 1
            with pytest.raises(OpenSandboxError) as second:
                await manager.aclose()
            assert second.value is first.value
            assert client_closes == 2
            assert watch_closes == state_closes == 1
            with pytest.raises(OpenSandboxError) as third:
                await manager.aclose()
            assert third.value is first.value
            assert client_closes == 2
            assert watch_closes == state_closes == 1
        finally:
            try:
                await context.__aexit__(None, None, None)
            except OpenSandboxError:
                pass
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
