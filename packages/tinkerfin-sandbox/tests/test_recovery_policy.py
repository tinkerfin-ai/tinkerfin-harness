from __future__ import annotations

import asyncio

import pytest
from test_manager import (
    _FakeBackend,
    _FakeClient,
    _FakeState,
    _new_manager,
    _resource_key,
)

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxInitializationError,
    OpenSandboxRecoveryPolicy,
    OpenSandboxStateOwnershipError,
    UnexpectedOpenSandboxBackendError,
)


class _RecoveringClient(_FakeClient):
    def __init__(self, failures: list[Exception]) -> None:
        super().__init__()
        self.failures = failures
        self.connect_entered = asyncio.Event()
        self.connected["original"] = _FakeBackend("original")

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_entered.set()
        if self.failures:
            self.connect_calls.append(sandbox_id)
            raise self.failures.pop(0)
        return await super().connect(sandbox_id)


def _fast_policy(*, recreate: bool = False) -> OpenSandboxRecoveryPolicy:
    return OpenSandboxRecoveryPolicy(
        initial_delay=0, max_delay=0, on_failure="recreate" if recreate else "raise"
    )


@pytest.mark.asyncio
async def test_transient_recovery_retries_only_the_original_id() -> None:
    client = _RecoveringClient(
        [
            OpenSandboxBackendTimeoutError("temporary timeout"),
            OpenSandboxBackendUnavailableError(
                "temporary network failure", context={"reason": "unreachable"}
            ),
        ]
    )
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy()
    ) as manager:
        backend = await manager.get("owner")
        assert backend.id == "original"
        assert client.connect_calls == ["original"] * 3
        assert state.bindings == {_resource_key("owner"): "original"}
        assert client.create_calls == 0
        assert client.destroy_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        OpenSandboxBackendUnavailableError(
            "auth", context={"reason": "authentication"}
        ),
        OpenSandboxBackendUnavailableError("denied", context={"reason": "permission"}),
        OpenSandboxBackendProtocolError("protocol"),
        OpenSandboxInitializationError(
            "initializer", cause=OpenSandboxBackendTimeoutError("initializer timeout")
        ),
        UnexpectedOpenSandboxBackendError("unknown 404 not found"),
    ],
)
async def test_nonrecoverable_failures_never_retry_or_recreate(
    failure: OpenSandboxBackendError,
) -> None:
    client = _RecoveringClient([failure])
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(type(failure)) as raised:
            await manager.get("owner")
        assert raised.value is failure
        assert client.connect_calls == ["original"]
        assert client.create_calls == 0
        assert client.destroy_calls == []
        assert state.bindings == {_resource_key("owner"): "original"}


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_confirmed_missing_skips_retries_and_obeys_the_failure_action(
    recreate: bool,
) -> None:
    client = _RecoveringClient(
        [OpenSandboxBackendUnavailableError("gone", context={"reason": "not_found"})]
    )
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=recreate)
    ) as manager:
        if recreate:
            backend = await manager.get("owner")
            assert backend.id != "original"
            assert state.bindings == {_resource_key("owner"): backend.id}
            assert client.destroy_calls == ["original"]
        else:
            with pytest.raises(OpenSandboxBackendUnavailableError) as raised:
                await manager.get("owner")
            assert raised.value.context["attempts"] == 1
            assert state.bindings == {_resource_key("owner"): "original"}
            assert client.destroy_calls == []
        assert client.connect_calls == ["original"]
        assert client.create_calls == int(recreate)


@pytest.mark.asyncio
async def test_reconnect_cannot_recreate_even_when_get_is_allowed_to() -> None:
    client = _RecoveringClient(
        [OpenSandboxBackendUnavailableError("gone", context={"reason": "not_found"})]
    )
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await manager.reconnect("owner")
        assert client.create_calls == 0
        assert client.destroy_calls == []
        assert state.bindings == {_resource_key("owner"): "original"}


@pytest.mark.asyncio
async def test_cancellation_during_retry_keeps_binding_and_instance() -> None:
    client = _RecoveringClient([OpenSandboxBackendTimeoutError("temporary")])
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(client=client, state=state) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        await client.connect_entered.wait()
        getting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await getting
        assert state.bindings == {_resource_key("owner"): "original"}
        assert client.create_calls == 0
        assert client.destroy_calls == []


@pytest.mark.asyncio
async def test_lost_binding_does_not_authorize_disposal_of_the_local_handle() -> None:
    client = _FakeClient()
    state = _FakeState()
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        handle = await manager.get("owner")
        state.bindings.clear()
        with pytest.raises(OpenSandboxStateOwnershipError):
            await manager.get("owner")
        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.destroy_calls == []


@pytest.mark.asyncio
async def test_recreate_rejects_a_local_handle_without_authoritative_binding() -> None:
    client = _FakeClient()
    state = _FakeState()
    async with _new_manager(client=client, state=state) as manager:
        handle = await manager.get("owner")
        state.bindings.clear()
        with pytest.raises(OpenSandboxStateOwnershipError):
            await manager.recreate("owner")
        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.destroy_calls == []
        assert client.backends[0].kill_calls == 0


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_recovery_policy_rejects_invalid_attempt_counts(value: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxRecoveryPolicy(max_attempts=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True])
def test_recovery_policy_rejects_invalid_durations(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxRecoveryPolicy(timeout=value)
