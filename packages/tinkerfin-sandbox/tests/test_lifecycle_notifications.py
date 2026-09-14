"""Confirmed lifecycle facts, bounded observer delivery, and recovery interaction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from opensandbox.config import ConnectionConfig
from test_backend import _FakeSandbox
from test_manager import (
    _ExecuteResponse,
    _FakeBackend,
    _FakeClient,
    _FakeState,
    _new_manager,
    _ReconnectableFakeClient,
    _ResettableLocalBackend,
    _resource_key,
)
from test_recovery_policy import _fast_policy, _RecoveringClient
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxBinding,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxError,
    OpenSandboxErrorCode,
    OpenSandboxInitializationError,
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleObserver,
    OpenSandboxNotificationOptions,
    OpenSandboxObserverReentryError,
    OpenSandboxOwnerClaim,
    OpenSandboxResetError,
    OpenSandboxRuntimeInfo,
    OpenSandboxStateError,
    OpenSandboxUnavailableReason,
    OpenSandboxWarmPoolUnavailableError,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox import (
    OpenSandboxLifecycleEventType as Kind,
)
from tinkerfin_sandbox import (
    OpenSandboxLifecycleReason as Reason,
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[OpenSandboxLifecycleEvent] = []
        self.changed = asyncio.Condition()
        self.close_calls = 0

    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        async with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    async def wait_for(self, kind: Kind, count: int = 1) -> None:
        async with asyncio.timeout(3), self.changed:
            await self.changed.wait_for(
                lambda: sum(event.type is kind for event in self.events) >= count
            )

    async def aclose(self) -> None:
        self.close_calls += 1


def _kinds(observer: _Recorder) -> list[Kind]:
    return [event.type for event in observer.events]


@pytest.fixture
def startup_state_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """Resolve the disposable database before the asynchronous test starts."""
    if request.param == "mysql":
        return cast(str, request.getfixturevalue("mysql_sandbox_url"))
    return f"sqlite+aiosqlite:///{tmp_path / 'startup-claim.db'}"


@pytest.mark.asyncio
async def test_transient_recovery_preserves_identity_and_notifies_in_order() -> None:
    recorder = _Recorder()
    client = _RecoveringClient([OpenSandboxBackendTimeoutError("secret endpoint")])
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(), observers=[recorder]
    ) as manager:
        backend = await manager.get("owner")
        assert backend.id == "original"
        await manager.get("owner")
        await manager.reconnect("owner")
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING, Kind.RECOVERED]
    assert recorder.events[0].reason is Reason.TIMEOUT
    assert recorder.events[-1].recovered
    assert not any(event.replaced for event in recorder.events)
    assert recorder.events[-1].workspace_may_have_changed
    assert client.create_calls == 0
    assert client.destroy_calls == []
    assert state.bindings == {_resource_key("owner"): "original"}
    assert recorder.close_calls == 0


@pytest.mark.asyncio
async def test_missing_default_binding_and_repeated_checks_share_one_outage() -> None:
    recorder = _Recorder()
    client = _FakeClient()
    state = _FakeState({_resource_key("owner"): "missing"})
    client.inspection_results["missing"] = OpenSandboxRuntimeInfo.unavailable(
        "missing", "not_found"
    )
    async with _new_manager(
        client=client, state=state, observers=[recorder]
    ) as manager:
        for _ in range(3):
            details = await manager.get_details("owner")
            assert details is not None and not details.available
            with pytest.raises(OpenSandboxBackendUnavailableError):
                await manager.get("owner")
        assert state.bindings == {_resource_key("owner"): "missing"}
        assert client.create_calls == 0
        assert client.destroy_calls == []
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING, Kind.RECOVERY_FAILED]
    assert all(event.reason is Reason.NOT_FOUND for event in recorder.events)
    assert all(event.workspace_may_have_changed for event in recorder.events)


@pytest.mark.asyncio
async def test_replacement_waits_for_binding_and_handle_publication() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedState(_FakeState):
        async def bind_owner(
            self, claim: OpenSandboxOwnerClaim, sandbox_id: str
        ) -> OpenSandboxBinding:
            entered.set()
            await release.wait()
            return await super().bind_owner(claim, sandbox_id)

    recorder = _Recorder()
    state = GatedState({_resource_key("owner"): "missing"})
    client = _FakeClient()
    async with _new_manager(
        client=client,
        state=state,
        observers=[recorder],
        recovery_policy=_fast_policy(recreate=True),
    ) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        await entered.wait()
        await recorder.wait_for(Kind.RECOVERING)
        assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING]
        release.set()
        backend = await getting
        await recorder.wait_for(Kind.REPLACED)
        event = recorder.events[-1]
        assert state.bindings == {_resource_key("owner"): backend.id}
        assert event.diagnostic_context == {
            "sandbox_id": backend.id,
            "previous_sandbox_id": "missing",
        }
        assert event.replaced and event.recovered and event.workspace_may_have_changed


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_failed_or_uncertain_binding_never_announces_replacement(
    uncertain: bool,
) -> None:
    state = _FakeState({_resource_key("owner"): "missing"})
    state.save_error = RuntimeError("secret database response")
    if uncertain:
        state.commit_before_save_error = True
        state.read_error = RuntimeError("secret database unavailable")
    client = _FakeClient()
    recorder = _Recorder()
    async with _new_manager(
        client=client,
        state=state,
        observers=[recorder],
        recovery_policy=_fast_policy(recreate=True),
    ) as manager:
        with pytest.raises(OpenSandboxStateError):
            await manager.get("owner")
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING, Kind.RECOVERY_FAILED]
    assert recorder.events[-1].reason is Reason.STATE_FAILURE


@pytest.mark.asyncio
async def test_first_creation_is_silent_and_explicit_destroy_is_once() -> None:
    recorder = _Recorder()
    client = _FakeClient()
    async with _new_manager(client=client, observers=[recorder]) as manager:
        backend = await manager.recreate("owner")
        assert backend.id == "sandbox-1"
        assert recorder.events == []
        await manager.destroy("owner")
        await manager.destroy("owner")
        await manager.get("another-owner")
    assert _kinds(recorder) == [Kind.DESTROYED]
    assert recorder.events[0].reason is Reason.EXPLICIT_DESTROY


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_cancelled_committed_replacement_is_announced_when_reconnected(
    cached: bool,
) -> None:
    recorder = _Recorder()
    state = _FakeState({_resource_key("owner"): "original"})
    client = _RecoveringClient([])
    async with _new_manager(
        client=client,
        state=state,
        observers=[recorder],
        recovery_policy=_fast_policy(recreate=True),
    ) as manager:
        if cached:
            await manager.get("owner")
        state.cancel_after_save_commit = True
        with pytest.raises(asyncio.CancelledError):
            await manager.recreate("owner")
        await recorder.wait_for(Kind.RECOVERING)
        assert _kinds(recorder) == [Kind.RECOVERING]
        state.cancel_after_save_commit = False
        client.connected["sandbox-1"] = _FakeBackend("sandbox-1")
        backend = await manager.get("owner")
        assert backend.id == "sandbox-1"
    assert _kinds(recorder) == [Kind.RECOVERING, Kind.REPLACED]
    assert recorder.events[-1].diagnostic_context["previous_sandbox_id"] == "original"


@pytest.mark.asyncio
async def test_adopting_external_binding_change_announces_replacement() -> None:
    recorder = _Recorder()
    state = _FakeState()
    client = _FakeClient()
    async with _new_manager(
        client=client, state=state, observers=[recorder]
    ) as manager:
        handle = await manager.get("owner")
        state.bindings[_resource_key("owner")] = "external"
        client.connected["external"] = _FakeBackend("external")
        assert await manager.get("owner") is handle
    assert _kinds(recorder) == [Kind.REPLACED]
    assert recorder.events[-1].diagnostic_context["previous_sandbox_id"] == "sandbox-1"
    assert client.destroy_calls == []


@pytest.mark.asyncio
async def test_recovery_failures_do_not_rebuild_for_initialization_errors() -> None:
    recorder = _Recorder()
    failure = OpenSandboxInitializationError(
        "private initializer", cause=TimeoutError("private")
    )
    client = _RecoveringClient([failure])
    async with _new_manager(
        client=client,
        state=_FakeState({_resource_key("owner"): "original"}),
        observers=[recorder],
        recovery_policy=_fast_policy(recreate=True),
    ) as manager:
        with pytest.raises(OpenSandboxInitializationError) as raised:
            await manager.get("owner")
        assert raised.value is failure
        assert client.create_calls == 0
        assert client.destroy_calls == []
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERY_FAILED]
    assert all(
        event.reason is Reason.INITIALIZATION_FAILED for event in recorder.events
    )
    assert all(event.workspace_may_have_changed for event in recorder.events)


@pytest.mark.asyncio
async def test_cancellation_is_not_a_recovery_failure() -> None:
    recorder = _Recorder()
    client = _RecoveringClient([OpenSandboxBackendTimeoutError("temporary")])
    async with _new_manager(
        client=client,
        state=_FakeState({_resource_key("owner"): "original"}),
        observers=[recorder],
    ) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        await recorder.wait_for(Kind.RECOVERING)
        getting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await getting
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING]


@pytest.mark.asyncio
async def test_health_checks_report_recovery_without_creating_or_reconnecting() -> None:
    recorder = _Recorder()
    client = _FakeClient()
    async with _new_manager(client=client, observers=[recorder]) as manager:
        await manager.get("owner")
        backend = cast(_FakeBackend, client.backends[0])
        backend.healthy = False
        for _ in range(3):
            assert not await manager.is_healthy("owner")
        backend.healthy = True
        assert await manager.is_healthy("owner")
        backend.healthy = False
        assert not await manager.is_healthy("owner")
        assert client.create_calls == 1 and client.connect_calls == []
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERED, Kind.UNAVAILABLE]
    assert not any(event.workspace_may_have_changed for event in recorder.events)


@pytest.mark.asyncio
async def test_confirmed_absence_updates_workspace_evidence_without_repeating_it() -> (
    None
):
    recorder = _Recorder()
    client = _FakeClient()
    state = _FakeState({_resource_key("owner"): "original"})
    async with _new_manager(
        client=client, state=state, observers=[recorder]
    ) as manager:
        reasons: tuple[OpenSandboxUnavailableReason, ...] = (
            "unreachable",
            "not_found",
            "not_found",
        )
        for reason in reasons:
            client.inspection_results["original"] = OpenSandboxRuntimeInfo.unavailable(
                "original", reason
            )
            await manager.get_details("owner")
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.UNAVAILABLE]
    assert not recorder.events[0].workspace_may_have_changed
    assert recorder.events[1].workspace_may_have_changed


@pytest.mark.asyncio
async def test_initializer_side_effects_are_reported_without_claiming_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    marker = tmp_path / "initializer-output.txt"
    sandbox = _FakeSandbox("original")
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    creating = AsyncMock(side_effect=AssertionError("initialization must not recreate"))
    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.Sandbox.create", creating)

    async def initialize(_backend: OpenSandboxBackend) -> None:
        await asyncio.to_thread(
            marker.write_text, "initializer side effect", encoding="utf-8"
        )
        raise RuntimeError("initializer failed after writing")

    state = _FakeState({_resource_key("owner"): "original"})
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0, workspace_root=None),
        initializers=[initialize],
    )
    async with _new_manager(
        client=client,
        state=state,
        observers=[recorder],
        recovery_policy=_fast_policy(recreate=True),
    ) as manager:
        with pytest.raises(OpenSandboxInitializationError):
            await manager.get("owner")
        assert state.bindings == {_resource_key("owner"): "original"}
        assert marker.read_text(encoding="utf-8") == "initializer side effect"
        creating.assert_not_awaited()
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERY_FAILED]
    assert all(event.workspace_may_have_changed for event in recorder.events)
    assert not any(event.replaced for event in recorder.events)
    assert sandbox.closed and not sandbox.killed


@pytest.mark.asyncio
async def test_uncached_inspection_does_not_announce_recovery_before_handle_publication() -> (
    None
):
    recorder = _Recorder()
    client = _FakeClient()
    state = _FakeState({_resource_key("owner"): "original"})
    client.inspection_results["original"] = OpenSandboxRuntimeInfo.unavailable(
        "original", "unreachable"
    )
    client.connected["original"] = _FakeBackend("original")
    async with _new_manager(
        client=client, state=state, observers=[recorder]
    ) as manager:
        await manager.get_details("owner")
        client.inspection_results["original"] = OpenSandboxRuntimeInfo(
            sandbox_id="original", available=True, healthy=True
        )
        details = await manager.get_details("owner")
        assert details is not None and details.healthy and not details.cached
        assert (await manager.get("owner")).id == "original"
    assert _kinds(recorder) == [Kind.UNAVAILABLE, Kind.RECOVERING, Kind.RECOVERED]


@pytest.mark.asyncio
async def test_old_probe_does_not_announce_failure_after_replacement() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedBackend(_FakeBackend):
        async def aexecute(
            self, command: str, *, timeout: int | None = None
        ) -> _ExecuteResponse:
            if self.id == "sandbox-1":
                entered.set()
                await release.wait()
                return _ExecuteResponse(1)
            return await super().aexecute(command, timeout=timeout)

    recorder = _Recorder()
    client = _FakeClient(backend_factory=GatedBackend)
    async with _new_manager(client=client, observers=[recorder]) as manager:
        handle = await manager.get("owner")
        checking = asyncio.create_task(manager.is_healthy("owner"))
        await entered.wait()
        replacing = asyncio.create_task(manager.recreate("owner"))
        await recorder.wait_for(Kind.REPLACED)
        assert handle.id == "sandbox-2"
        release.set()
        assert not await checking
        await replacing
    assert _kinds(recorder) == [Kind.RECOVERING, Kind.REPLACED]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "cancel", "self_cancel"])
async def test_observer_failure_and_cancellation_are_isolated(
    mode: str,
) -> None:
    class FailingObserver:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0

        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            del event
            self.calls += 1
            self.active += 1
            try:
                if mode == "raise":
                    raise RuntimeError("secret callback payload")
                if mode == "cancel":
                    raise asyncio.CancelledError("secret callback payload")
                if mode == "self_cancel":
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                await asyncio.Event().wait()
            finally:
                self.active -= 1

    failing = FailingObserver()
    recorder = _Recorder()
    async with _new_manager(
        client=_FakeClient(),
        observers=[failing, recorder],
    ) as manager:
        await manager.get("owner")
        backend = await manager.recreate("owner")
        assert backend.id == "sandbox-2"
        await manager.destroy("owner")
    assert _kinds(recorder) == [Kind.RECOVERING, Kind.REPLACED, Kind.DESTROYED]
    assert failing.calls == 3 and failing.active == 0


@pytest.mark.asyncio
async def test_full_queue_drops_new_events_and_does_not_delay_other_observers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowObserver(_Recorder):
        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            entered.set()
            await release.wait()
            await super().on_sandbox_event(event)

    slow, fast = SlowObserver(), _Recorder()
    manager = _new_manager(
        client=_FakeClient(),
        observers=[slow, fast],
        notification_options=OpenSandboxNotificationOptions(
            max_pending_events=2, timeout=2
        ),
    )
    with caplog.at_level(logging.WARNING, logger="tinkerfin.sandbox.notifications"):
        try:
            await manager.start()
            await manager.get("owner")
            await manager.recreate("owner")
            await entered.wait()
            await manager.recreate("owner")
            await fast.wait_for(Kind.REPLACED, 2)
            assert slow.events == []
        finally:
            release.set()
            await manager.aclose()
    assert [event.event_id for event in slow.events] == [
        event.event_id for event in fast.events[:3]
    ]
    notices = [r for r in caplog.records if r.name == "tinkerfin.sandbox.notifications"]
    assert len(notices) == 1
    assert notices[0].__dict__["tinkerfin_queue_full"] == 1
    assert all(
        not task.get_name().startswith("tinkerfin-sandbox-notification")
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "get",
        "reconnect",
        "recreate",
        "reset",
        "destroy",
        "delete",
        "is_healthy",
        "get_details",
        "check_ready",
        "start",
        "aclose",
        "child",
    ],
)
async def test_observer_cannot_reenter_its_manager(operation: str) -> None:
    failures: list[OpenSandboxObserverReentryError] = []

    class ReentrantObserver:
        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            del event
            operations: dict[str, Callable[[], Awaitable[object]]] = {
                "get": lambda: manager.get("owner"),
                "reconnect": lambda: manager.reconnect("owner"),
                "recreate": lambda: manager.recreate("owner"),
                "reset": lambda: manager.reset("owner"),
                "destroy": lambda: manager.destroy("owner"),
                "delete": lambda: manager.delete("owner"),
                "is_healthy": lambda: manager.is_healthy("owner"),
                "get_details": lambda: manager.get_details("owner"),
                "check_ready": manager.check_ready,
                "start": manager.start,
                "aclose": manager.aclose,
            }
            try:
                if operation == "child":
                    await asyncio.create_task(manager.get("owner"))
                else:
                    await operations[operation]()
            except OpenSandboxObserverReentryError as error:
                failures.append(error)

    manager = _new_manager(client=_FakeClient(), observers=[ReentrantObserver()])
    async with manager:
        await manager.get("owner")
        await manager.destroy("owner")
    assert len(failures) == 1
    assert isinstance(failures[0], OpenSandboxError)
    assert failures[0].code is OpenSandboxErrorCode.OBSERVER_REENTRY
    assert dict(failures[0].context) == {}


@pytest.mark.asyncio
async def test_no_observer_creates_no_notification_tasks() -> None:
    async with _new_manager(client=_FakeClient()) as manager:
        await manager.get("owner")
        await manager.recreate("owner")
        await manager.destroy("owner")
        assert all(
            not task.get_name().startswith("tinkerfin-sandbox-notif")
            for task in asyncio.all_tasks()
        )


@pytest.mark.asyncio
async def test_shared_capacity_notifications_follow_readiness_only(
    sql_engine: SqlEngineFactory,
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}"
    recorder = _Recorder()
    state = SQLAlchemyOpenSandboxState(
        engine=sql_engine(url), namespace="notifications"
    )
    peer = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="notifications")
    async with _new_manager(
        client=_FakeClient(), state=state, warm_pool_size=1, observers=[recorder]
    ) as manager:
        await peer.start(warm_pool_size=1)
        try:
            claim = await peer.claim_ready_warm_slot(exclude_slots=())
            assert claim is not None
            await manager.check_ready()
            assert recorder.events == []
            await peer.discard_ready_warm_slot(claim)
            for _ in range(2):
                with pytest.raises(OpenSandboxWarmPoolUnavailableError):
                    await manager.check_ready()
            fill = await peer.claim_warm_slot()
            assert fill is not None
            await peer.publish_warm(fill, "replacement")
            await manager.check_ready()
        finally:
            await peer.aclose()
    assert _kinds(recorder) == [
        Kind.WARM_CAPACITY_DEGRADED,
        Kind.WARM_CAPACITY_RESTORED,
    ]
    assert all(
        event.owner_key is None and not event.diagnostic_context
        for event in recorder.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "startup_state_url",
    [
        "sqlite",
        pytest.param(
            "mysql",
            marks=[pytest.mark.docker_integration, pytest.mark.mysql_integration],
        ),
    ],
    indirect=True,
)
async def test_new_manager_cannot_accept_unverified_capacity_claimed_by_a_peer(
    sql_engine: SqlEngineFactory, startup_state_url: str, strict: bool
) -> None:
    url = startup_state_url
    peer = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="startup")
    await peer.start(warm_pool_size=1)
    creating = await peer.claim_warm_slot()
    assert creating is not None
    await peer.publish_warm(creating, "unverified")
    checking = await peer.claim_ready_warm_slot(exclude_slots=())
    assert checking is not None
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="startup"),
        warm_pool_size=1,
        fail_on_startup_warmup_error=strict,
    )
    try:
        if strict:
            with pytest.raises(OpenSandboxWarmPoolUnavailableError):
                await manager.start()
        else:
            await manager.start()
        assert await peer.warm_pool_ready()
        with pytest.raises(OpenSandboxWarmPoolUnavailableError):
            await manager.check_ready()
        assert client.connect_calls == [] and client.create_calls == 0
    finally:
        await peer.release_warm(checking)
        await manager.aclose()
        await peer.aclose()


@pytest.mark.asyncio
async def test_explicit_reset_notifies_only_after_workspace_contents_are_cleared(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "private.txt").write_text(
        "secret workspace contents", encoding="utf-8"
    )
    recorder = _Recorder()
    client = _ReconnectableFakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    async with _new_manager(
        client=client, state=_FakeState(), observers=[recorder]
    ) as manager:
        backend = await manager.get("owner", namespace="workspace-company")
        await manager.reset("owner", namespace="workspace-company")
        await recorder.wait_for(Kind.WORKSPACE_RESET)
        assert list(workspace.iterdir()) == []
        assert backend.id == "sandbox-1"
        assert client.create_calls == 1 and client.destroy_calls == []
    assert _kinds(recorder) == [Kind.WORKSPACE_RESET]
    assert recorder.events[0].namespace == "workspace-company"
    assert recorder.events[0].workspace_may_have_changed
    assert not recorder.events[0].replaced


@pytest.mark.asyncio
async def test_refused_reset_does_not_publish_a_success_event() -> None:
    recorder = _Recorder()
    async with _new_manager(client=_FakeClient(), observers=[recorder]) as manager:
        await manager.get("owner")
        with pytest.raises(OpenSandboxResetError):
            await manager.reset("owner")
    assert recorder.events == []


@pytest.mark.asyncio
async def test_cancelled_destroy_announces_the_confirmed_result_once() -> None:
    recorder = _Recorder()
    client = _FakeClient()
    client.destroy_gate = asyncio.Event()
    state = _FakeState({_resource_key("owner"): "existing"})
    async with _new_manager(
        client=client, state=state, observers=[recorder]
    ) as manager:
        destroying = asyncio.create_task(manager.destroy("owner"))
        await client.destroy_entered.wait()
        destroying.cancel()
        client.destroy_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await destroying
        await manager.destroy("owner")
    assert _kinds(recorder) == [Kind.DESTROYED]
    assert state.bindings == {}


@pytest.mark.asyncio
async def test_observer_may_operate_a_different_manager() -> None:
    other = _new_manager(client=_FakeClient())
    completed: list[str] = []

    class Observer:
        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            del event
            completed.append((await other.get("separate-owner")).id)

    async with (
        other,
        _new_manager(client=_FakeClient(), observers=[Observer()]) as manager,
    ):
        await manager.get("owner")
        await manager.destroy("owner")
    assert completed == ["sandbox-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_level", "component_level", "count"),
    [
        (logging.WARNING, logging.NOTSET, 1),
        (logging.ERROR, logging.NOTSET, 0),
        (logging.ERROR, logging.WARNING, 1),
        (logging.CRITICAL + 1, logging.NOTSET, 0),
    ],
)
async def test_delivery_logging_is_bounded_safe_and_controlled_by_host(
    caplog: pytest.LogCaptureFixture,
    parent_level: int,
    component_level: int,
    count: int,
) -> None:
    class Observer:
        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            raise RuntimeError(
                f"secret credentials and payload: {event.diagnostic_context}"
            )

    parent = logging.getLogger("tinkerfin")
    component = logging.getLogger("tinkerfin.sandbox")
    child = logging.getLogger("tinkerfin.sandbox.notifications")
    assert child.level == logging.NOTSET and child.propagate and not child.handlers
    before = (parent.level, component.level)
    try:
        parent.setLevel(parent_level)
        component.setLevel(component_level)
        async with _new_manager(
            client=_FakeClient(), observers=[Observer()]
        ) as manager:
            await manager.get("secret-owner")
            for _ in range(4):
                await manager.recreate("secret-owner")
    finally:
        parent.setLevel(before[0])
        component.setLevel(before[1])
    notices = [record for record in caplog.records if record.name == child.name]
    assert len(notices) == count
    for record in notices:
        assert record.exc_info is None
        rendered = repr(record.__dict__)
        assert "secret" not in rendered
        assert all(f"sandbox-{number}" not in rendered for number in range(1, 6))
        assert record.__dict__["tinkerfin_callback_failed"] == 1


def test_event_fields_are_immutable_and_diagnostic_ids_are_not_in_repr() -> None:
    diagnostics = {"sandbox_id": "trusted-remote-id"}
    event = OpenSandboxLifecycleEvent(
        event_id="event-id",
        type=Kind.REPLACED,
        owner_key="owner",
        occurred_at=datetime.now(UTC),
        reason=Reason.NOT_FOUND,
        workspace_may_have_changed=True,
        diagnostic_context=diagnostics,
    )
    diagnostics["sandbox_id"] = "mutated"
    assert event.diagnostic_context["sandbox_id"] == "trusted-remote-id"
    assert not hasattr(event.diagnostic_context, "__setitem__")
    with pytest.raises(FrozenInstanceError):
        object.__getattribute__(event, "__setattr__")("owner_key", "mutated")
    assert "trusted-remote-id" not in repr(event)
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert len({kind.value for kind in Kind}) == len(Kind)
    assert len({reason.value for reason in Reason}) == len(Reason)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_notification_capacity_must_be_a_positive_integer(value: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxNotificationOptions(max_pending_events=value)


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True])
def test_notification_timeout_must_be_positive_and_finite(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxNotificationOptions(timeout=value)


def test_duplicate_observers_are_rejected() -> None:
    observer = _Recorder()
    with pytest.raises(ValueError, match="same observer twice"):
        _new_manager(client=_FakeClient(), observers=[observer, observer])


def test_observer_protocol_is_a_real_host_extension() -> None:
    observer: OpenSandboxLifecycleObserver = _Recorder()
    assert callable(observer.on_sandbox_event)
