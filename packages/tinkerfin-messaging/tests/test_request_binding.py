"""Immutable command admission across every storage implementation."""

from __future__ import annotations

import pytest
from backend_harness import MessagingBackendHarness

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import RunRequestConflict
from tinkerfin_messaging.backend_contract import MessagingStateQuery


async def test_request_binding_is_atomic_and_survives_run_updates(
    messaging_backend: MessagingBackendHarness,
) -> None:
    backend = messaging_backend
    identity = RunIdentity(namespace="commands", thread_id="thread", run_id="request")
    owner = await backend.prepare(
        channel="commands",
        identity=identity,
        codec="test.bytes",
        after=0,
        cancellable=True,
        recoverable=True,
        request_digest="a" * 64,
    )
    await backend.append(
        owner.handle, message_id="one", codec="test.bytes", payload=b"one"
    )
    await backend.renew(owner.handle)
    for requested in (None, "b" * 64):
        with pytest.raises(RunRequestConflict):
            await backend.prepare(
                channel="commands",
                identity=identity,
                codec="test.bytes",
                after=0,
                cancellable=True,
                recoverable=True,
                request_digest=requested,
            )
    state = await backend.load_messaging_state(
        MessagingStateQuery(channel="commands", identity=identity)
    )
    assert state.target_run is not None
    assert state.target_run.request_digest == "a" * 64
    assert state.target_run.producer_token == owner.handle.owner_token
    await backend.finish(owner.handle, status="completed")
    # get_run_status exercises Redis's run/page snapshot as well as the general
    # state snapshot above, without relying on a private decoding function.
    assert (
        await backend.get_run_status(channel="commands", identity=identity)
        == "completed"
    )
    attached = await backend.prepare(
        channel="commands",
        identity=identity,
        codec="test.bytes",
        after=0,
        cancellable=True,
        recoverable=True,
        request_digest="a" * 64,
    )
    assert not attached.is_owner
    assert [
        item.payload
        for item in await backend.read(channel="commands", identity=identity)
    ] == [b"one"]
    state = await backend.load_messaging_state(
        MessagingStateQuery(channel="commands", identity=identity)
    )
    assert state.target_run is not None and state.target_run.request_digest == "a" * 64


async def test_command_cannot_adopt_an_existing_unbound_run(
    messaging_backend: MessagingBackendHarness,
) -> None:
    identity = RunIdentity(namespace="commands", thread_id="thread", run_id="unbound")
    await messaging_backend.prepare(
        channel="commands",
        identity=identity,
        codec="test.bytes",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    with pytest.raises(RunRequestConflict):
        await messaging_backend.prepare(
            channel="commands",
            identity=identity,
            codec="test.bytes",
            after=0,
            cancellable=True,
            recoverable=False,
            request_digest="a" * 64,
        )
