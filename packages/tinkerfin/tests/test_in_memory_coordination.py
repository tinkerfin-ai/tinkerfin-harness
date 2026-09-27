from __future__ import annotations

import asyncio

import pytest

from tinkerfin import RunIdentity
from tinkerfin.coordination import InMemoryRunCoordinator


def _identity(thread_id: str, *, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


@pytest.mark.parametrize("custom_key", [False, True])
async def test_namespaces_isolate_default_and_custom_coordination(
    custom_key: bool,
) -> None:
    coordinator = InMemoryRunCoordinator(
        key_resolver=(lambda _: "same-resource") if custom_key else None
    )
    entered = asyncio.Event()
    attempted = asyncio.Event()
    release = asyncio.Event()

    async def other_namespace() -> None:
        attempted.set()
        async with coordinator(
            RunIdentity(namespace="other", thread_id="same", run_id="run")
        ):
            entered.set()
            await release.wait()

    async with coordinator(
        RunIdentity(namespace="test", thread_id="same", run_id="run")
    ):
        other = asyncio.create_task(other_namespace())
        try:
            await attempted.wait()
            assert entered.is_set()
        finally:
            release.set()
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)


@pytest.mark.parametrize("custom_key", [False, True])
async def test_same_resolved_key_serializes_runs(custom_key: bool) -> None:
    coordinator = InMemoryRunCoordinator(
        key_resolver=(lambda value: value.thread_id) if custom_key else None
    )
    entered, attempted, release, successor_entered = (asyncio.Event() for _ in range(4))

    async def holder() -> None:
        async with coordinator(_identity("same", run_id="one")):
            entered.set()
            await release.wait()

    async def successor() -> None:
        attempted.set()
        async with coordinator(_identity("same", run_id="two")):
            successor_entered.set()

    first = asyncio.create_task(holder())
    second: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        second = asyncio.create_task(successor())
        await attempted.wait()
        assert not successor_entered.is_set()
    finally:
        release.set()
        await first
        if second is not None:
            await second
    assert successor_entered.is_set()


@pytest.mark.asyncio
async def test_different_resolved_keys_enter_without_waiting_for_each_other() -> None:
    coordinator = InMemoryRunCoordinator(key_resolver=lambda value: value.thread_id)
    release = asyncio.Event()
    attempting = (asyncio.Event(), asyncio.Event())
    entered = (asyncio.Event(), asyncio.Event())

    async def coordinate(index: int, identity: RunIdentity) -> None:
        attempting[index].set()
        async with coordinator(identity):
            entered[index].set()
            await release.wait()

    tasks = (
        asyncio.create_task(coordinate(0, _identity("user-1"))),
        asyncio.create_task(coordinate(1, _identity("user-2"))),
    )
    try:
        await asyncio.gather(*(event.wait() for event in attempting))
        assert all(event.is_set() for event in entered)
    finally:
        release.set()
    await asyncio.gather(*tasks)


async def test_cancelled_waiter_releases_its_registration_and_keeps_key_usable() -> (
    None
):
    coordinator = InMemoryRunCoordinator()
    holder_entered, attempted, release = (asyncio.Event() for _ in range(3))

    async def holder() -> None:
        async with coordinator(_identity("same", run_id="holder")):
            holder_entered.set()
            await release.wait()

    async def waiter() -> None:
        attempted.set()
        async with coordinator(_identity("same", run_id="waiter")):
            raise AssertionError("A cancelled waiter cannot enter the occupied scope")

    holding = asyncio.create_task(holder())
    waiting: asyncio.Task[None] | None = None
    try:
        await holder_entered.wait()
        waiting = asyncio.create_task(waiter())
        await attempted.wait()
        waiting.cancel("stop waiting")
        with pytest.raises(asyncio.CancelledError, match="stop waiting"):
            await waiting
    finally:
        release.set()
        await holding
        if waiting is not None:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    async with coordinator(_identity("same", run_id="after")):
        pass


async def test_failed_scope_keeps_its_key_usable() -> None:
    coordinator = InMemoryRunCoordinator()
    with pytest.raises(ValueError, match="scope failed"):
        async with coordinator(_identity("same", run_id="failed")):
            raise ValueError("scope failed")
    async with coordinator(_identity("same", run_id="after")):
        pass
