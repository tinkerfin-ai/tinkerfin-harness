"""Real container faults and observer delivery on an isolated Docker daemon."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar
from uuid import uuid4

import pytest
import pytest_asyncio
from docker import DockerClient
from docker.models.containers import Container
from opensandbox.config import ConnectionConfig
from test_manager import _resource_key
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxBackendUnavailableError,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleObserver,
    OpenSandboxManager,
    OpenSandboxNotificationOptions,
    OpenSandboxRecoveryPolicy,
    OpenSandboxWarmPoolUnavailableError,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox import OpenSandboxLifecycleEventType as Kind
from tinkerfin_sandbox import OpenSandboxLifecycleReason as Reason

pytestmark = pytest.mark.opensandbox_e2e
_RUN_LABEL = "tinkerfin.test/sandbox-run"
_PURPOSE_LABEL = "tinkerfin.test/fault-case"
_ID_LABEL = "opensandbox.io/id"
_T = TypeVar("_T")
_Json: TypeAlias = str | int | float | bool | None | list["_Json"] | dict[str, "_Json"]


@pytest.fixture
def additional_lifecycle_observers() -> list[OpenSandboxLifecycleObserver]:
    """Allow a host integration plugin to borrow actual Manager event delivery."""
    return []


class _Evidence:
    """Record only fixed lifecycle fields and synthetic file hashes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.observations: list[_Json] = []

    def record(self, action: str, **values: _Json) -> None:
        self.observations.append({"action": action, **values})

    def event(self, event: OpenSandboxLifecycleEvent) -> None:
        self.record(
            "callback",
            event_id=event.event_id,
            type=event.type.value,
            reason=event.reason.value,
            owner_key=event.owner_key,
            occurred_at=event.occurred_at.isoformat(),
            workspace_may_have_changed=event.workspace_may_have_changed,
            sandbox_id=event.diagnostic_context.get("sandbox_id"),
            previous_sandbox_id=event.diagnostic_context.get("previous_sandbox_id"),
        )


@pytest.fixture
def fault_evidence(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[_Evidence]:
    """Write safe JSON even when a fault assertion fails."""
    evidence = _Evidence(request.node.name)
    evidence.record("environment", opensandbox_sdk=version("opensandbox"))
    try:
        yield evidence
    finally:
        directory = Path(
            os.environ.get("TINKERFIN_SANDBOX_FAULT_EVIDENCE_DIR", str(tmp_path))
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        output = directory / f"{request.node.name}.json"
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(evidence.observations, indent=2, ensure_ascii=True))
            stream.write("\n")


class _OwnedDocker:
    """Serialize finite Docker calls and require exact ownership before mutation.

    Docker's SDK is synchronous. A dedicated single-worker executor limits this
    test to one call at a time with a 15-second socket timeout. Cancellation cannot
    stop an in-flight Docker request, so the caller waits for settlement before
    releasing ownership or closing the executor. Only the fixture's verified
    isolated daemon is reachable; no host Docker client is created here.
    """

    def __init__(self, runtime: OpenSandboxDockerRuntime, evidence: _Evidence) -> None:
        self.purpose = uuid4().hex
        self.run_id = runtime.run_id
        self._evidence = evidence
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fault-docker"
        )
        self._lock = asyncio.Lock()
        self._client = DockerClient(
            base_url=runtime.client.api.base_url, version="1.51", timeout=15
        )
        self._client.api.trust_env = False

    async def _call(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            pending = asyncio.get_running_loop().run_in_executor(
                self._executor, operation
            )
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                settled = asyncio.gather(pending, return_exceptions=True)
                while not settled.done():
                    try:
                        await asyncio.shield(settled)
                    except asyncio.CancelledError:
                        continue
                raise

    def _containers(self, sandbox_id: str | None = None) -> list[Container]:
        labels = [f"{_RUN_LABEL}={self.run_id}", f"{_PURPOSE_LABEL}={self.purpose}"]
        if sandbox_id is not None:
            labels.append(f"{_ID_LABEL}={sandbox_id}")
        containers = self._client.containers.list(all=True, filters={"label": labels})
        for container in containers:
            assert container.labels[_RUN_LABEL] == self.run_id
            assert container.labels[_PURPOSE_LABEL] == self.purpose
            assert isinstance(container.labels[_ID_LABEL], str)
            assert container.labels[_ID_LABEL]
            if sandbox_id is not None:
                assert container.labels[_ID_LABEL] == sandbox_id
        return containers

    async def ids(self) -> tuple[str, ...]:
        def read() -> tuple[str, ...]:
            return tuple(
                container.labels[_ID_LABEL] for container in self._containers()
            )

        return await self._call(read)

    def _snapshot(self, sandbox_id: str) -> dict[str, _Json]:
        containers = self._containers(sandbox_id)
        assert len(containers) <= 1
        if not containers:
            return {"sandbox_id": sandbox_id, "status": "absent"}
        container = containers[0]
        return {
            "sandbox_id": sandbox_id,
            "container_id": container.id,
            "status": container.status,
        }

    async def snapshot(self, sandbox_id: str) -> dict[str, _Json]:
        return await self._call(partial(self._snapshot, sandbox_id))

    async def change(
        self, sandbox_id: str, action: Literal["stop", "start", "remove"]
    ) -> dict[str, _Json]:
        def apply() -> dict[str, _Json]:
            containers = self._containers(sandbox_id)
            assert len(containers) == 1, "Mutation requires one exact owned Sandbox"
            container = containers[0]
            if action == "stop":
                container.stop(timeout=1)
            elif action == "start":
                container.start()
            elif action == "remove":
                container.remove(force=True, v=True)
            else:
                raise ValueError("Unknown test fault action")
            return self._snapshot(sandbox_id)

        snapshot = await self._call(apply)
        self._evidence.record(action, **snapshot)
        return snapshot

    async def aclose(self) -> None:
        try:
            for sandbox_id in await self.ids():
                await self.change(sandbox_id, "remove")
            assert await self.ids() == ()
            self._evidence.record("cleanup", remaining_containers=0)
        finally:
            try:
                await self._call(self._client.close)
            finally:
                await asyncio.to_thread(self._executor.shutdown, wait=True)


@pytest_asyncio.fixture
async def fault_docker(
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    opensandbox_test_service: OpenSandboxTestService,
    fault_evidence: _Evidence,
) -> AsyncIterator[_OwnedDocker]:
    """Reclaim only exact test-case labels after all Manager resources settle."""
    assert opensandbox_docker_runtime.run_id == opensandbox_test_service.run_id
    owned = _OwnedDocker(opensandbox_docker_runtime, fault_evidence)
    fault_evidence.record("ownership", run_id=owned.run_id, purpose=owned.purpose)
    try:
        yield owned
    finally:
        await owned.aclose()


@pytest_asyncio.fixture(autouse=True)
async def check_all_task_cleanup(
    fault_docker: _OwnedDocker, fault_evidence: _Evidence
) -> AsyncIterator[None]:
    """Reject every newly pending task after the test's Managers have closed.

    The async fixture is entered before the test constructs a Manager and exits
    before Docker cleanup. Pytest's setup and teardown drivers are separate tasks;
    ignore the current driver and identities already present at setup. Task names
    are diagnostic only: unnamed cleanup and observer tasks are checked equally.
    """
    del fault_docker
    baseline = asyncio.all_tasks()
    try:
        yield
    finally:
        current = asyncio.current_task()
        unexpected = [
            task
            for task in asyncio.all_tasks()
            if task is not current and task not in baseline and not task.done()
        ]
        fault_evidence.record(
            "all_task_cleanup",
            remaining_tasks=[task.get_name() for task in unexpected],
            baseline_count=len(baseline),
            remaining_count=len(unexpected),
        )
        assert unexpected == []


class _Recorder:
    """Observe actual Manager callbacks and expose a bounded delivery wait."""

    def __init__(self, evidence: _Evidence) -> None:
        self.events: list[OpenSandboxLifecycleEvent] = []
        self._changed = asyncio.Condition()
        self._evidence = evidence

    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        async with self._changed:
            self.events.append(event)
            self._evidence.event(event)
            self._changed.notify_all()

    async def wait_for(
        self, kind: Kind, count: int = 1, *, timeout: float = 10
    ) -> None:
        async with asyncio.timeout(timeout), self._changed:
            await self._changed.wait_for(
                lambda: sum(event.type is kind for event in self.events) >= count
            )

    @property
    def kinds(self) -> list[Kind]:
        return [event.type for event in self.events]


def _client(
    service: OpenSandboxTestService,
    runtime: OpenSandboxDockerRuntime,
    owned: _OwnedDocker,
    *,
    warm_pool_size: int = 0,
    ttl: timedelta | None = None,
) -> OpenSandboxClient:
    return OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=service.domain,
            api_key=service.api_key,
            request_timeout=timedelta(seconds=5),
            use_server_proxy=True,
        ),
        config=OpenSandboxConfig(
            image=runtime.image,
            workspace_root="/workspace",
            warm_pool_size=warm_pool_size,
            ttl=ttl,
            metadata={
                **service.sandbox_metadata,
                _PURPOSE_LABEL: owned.purpose,
            },
        ),
    )


def _assert_no_named_tasks(evidence: _Evidence) -> None:
    remaining = [
        task.get_name()
        for task in asyncio.all_tasks()
        if not task.done()
        and task.get_name().startswith(("tinkerfin-sandbox-", "tinkerfin-opensandbox-"))
    ]
    evidence.record("named_task_cleanup", remaining_tasks=[name for name in remaining])
    assert remaining == []


def _assert_event_identity(observer: _Recorder, owner: str, sandbox_id: str) -> None:
    assert len({event.event_id for event in observer.events}) == len(observer.events)
    assert all(event.owner_key == owner for event in observer.events)
    assert all(
        event.diagnostic_context.get("sandbox_id") == sandbox_id
        for event in observer.events
    )
    assert all(
        event.occurred_at.utcoffset() == timedelta() for event in observer.events
    )
    assert [event.occurred_at for event in observer.events] == sorted(
        event.occurred_at for event in observer.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["explicit", "automatic"])
async def test_real_deleted_container_requires_selected_replacement(
    sql_engine: SqlEngineFactory,
    replacement: str,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    fault_docker: _OwnedDocker,
    fault_evidence: _Evidence,
    additional_lifecycle_observers: list[OpenSandboxLifecycleObserver],
    tmp_path: Path,
) -> None:
    """Remote deletion preserves durable ownership until replacement is selected."""
    observer = _Recorder(fault_evidence)
    url = f"sqlite+aiosqlite:///{tmp_path / 'deleted.db'}"
    owner = "deleted-owner"
    state = SQLAlchemyOpenSandboxState(
        engine=sql_engine(url), namespace=fault_docker.purpose
    )
    content = b"deleted-container-sentinel\x00\x80"
    async with OpenSandboxManager[str](
        client=_client(
            opensandbox_test_service, opensandbox_docker_runtime, fault_docker
        ),
        state=state,
        key_resolver=lambda key: key,
        observers=(observer, *additional_lifecycle_observers),
    ) as preserving:
        backend = await preserving.get(owner)
        original_id = backend.id
        assert (await backend.aupload_files([("/sentinel.bin", content)]))[
            0
        ].error is None
        read = (await backend.adownload_files(["/sentinel.bin"]))[0]
        assert read.error is None and read.content == content
        fault_evidence.record(
            "created",
            sandbox_id=original_id,
            file_sha256=hashlib.sha256(content).hexdigest(),
        )
        assert (await fault_docker.change(original_id, "remove"))["status"] == "absent"
        assert observer.events == []
        for _ in range(2):
            with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
                await preserving.get(owner)
            assert captured.value.context["reason"] == "not_found"
            assert captured.value.context["attempts"] == 1
            fault_evidence.record(
                "get_failed",
                code=captured.value.code.value,
                reason="not_found",
                attempts=1,
            )
            binding = await state.read_binding(_resource_key(owner))
            assert binding is not None and binding.sandbox_id == original_id
            assert await fault_docker.ids() == ()
        await observer.wait_for(Kind.RECOVERY_FAILED)
        assert observer.kinds == [Kind.UNAVAILABLE, Kind.RECOVERY_FAILED]
        assert all(event.reason is Reason.NOT_FOUND for event in observer.events)
        assert all(event.workspace_may_have_changed for event in observer.events)

        if replacement == "explicit":
            replaced = await preserving.recreate(owner)
            new_id = replaced.id
            assert new_id != original_id
            missing = (await replaced.adownload_files(["/sentinel.bin"]))[0]
            fault_evidence.record(
                "download_after_replacement",
                mode=replacement,
                sandbox_id=new_id,
                error=missing.error,
                content_is_none=missing.content is None,
            )
            assert missing.error == "file_not_found"
            assert missing.content is None
            replaced_binding = await state.read_binding(_resource_key(owner))
            assert (
                replaced_binding is not None and replaced_binding.sandbox_id == new_id
            )
            await preserving.destroy(owner)

    if replacement == "automatic":
        automatic = _Recorder(fault_evidence)
        replacement_state = SQLAlchemyOpenSandboxState(
            engine=sql_engine(url), namespace=fault_docker.purpose
        )
        async with OpenSandboxManager[str](
            client=_client(
                opensandbox_test_service, opensandbox_docker_runtime, fault_docker
            ),
            state=replacement_state,
            key_resolver=lambda key: key,
            recovery_policy=OpenSandboxRecoveryPolicy(on_failure="recreate"),
            observers=(automatic, *additional_lifecycle_observers),
        ) as recreating:
            before = await replacement_state.read_binding(_resource_key(owner))
            assert before is not None and before.sandbox_id == original_id
            replaced = await recreating.get(owner)
            new_id = replaced.id
            assert new_id != original_id
            missing = (await replaced.adownload_files(["/sentinel.bin"]))[0]
            fault_evidence.record(
                "download_after_replacement",
                mode=replacement,
                sandbox_id=new_id,
                error=missing.error,
                content_is_none=missing.content is None,
            )
            assert missing.error == "file_not_found"
            assert missing.content is None
            after = await replacement_state.read_binding(_resource_key(owner))
            assert after is not None and after.sandbox_id == new_id
            await recreating.destroy(owner)
        assert automatic.kinds == [
            Kind.UNAVAILABLE,
            Kind.RECOVERING,
            Kind.REPLACED,
            Kind.DESTROYED,
        ]
        assert [event.reason for event in automatic.events] == [
            Reason.NOT_FOUND,
            Reason.NOT_FOUND,
            Reason.NOT_FOUND,
            Reason.EXPLICIT_DESTROY,
        ]
        replacement_events = automatic.events
    else:
        assert observer.kinds == [
            Kind.UNAVAILABLE,
            Kind.RECOVERY_FAILED,
            Kind.RECOVERING,
            Kind.REPLACED,
            Kind.DESTROYED,
        ]
        assert observer.events[2].reason is Reason.EXPLICIT_RECREATE
        assert observer.events[3].reason is Reason.EXPLICIT_RECREATE
        replacement_events = observer.events

    replacement_event = next(
        event for event in replacement_events if event.type is Kind.REPLACED
    )
    assert replacement_event.owner_key == owner
    assert replacement_event.diagnostic_context == {
        "sandbox_id": new_id,
        "previous_sandbox_id": original_id,
    }
    assert replacement_event.recovered and replacement_event.replaced
    assert replacement_event.workspace_may_have_changed
    assert await fault_docker.ids() == ()
    fault_evidence.record(
        "replacement_verified",
        mode=replacement,
        original_id=original_id,
        replacement_id=new_id,
        original_file_missing=True,
    )
    _assert_no_named_tasks(fault_evidence)


@pytest.mark.asyncio
async def test_real_reset_and_repeated_destroy_report_confirmed_operations(
    sql_engine: SqlEngineFactory,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    fault_docker: _OwnedDocker,
    fault_evidence: _Evidence,
    additional_lifecycle_observers: list[OpenSandboxLifecycleObserver],
    tmp_path: Path,
) -> None:
    """Each reset clears real files; repeating an already completed destroy is silent."""
    observer = _Recorder(fault_evidence)
    state = SQLAlchemyOpenSandboxState(
        engine=sql_engine(f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}"),
        namespace=fault_docker.purpose,
    )
    async with OpenSandboxManager[str](
        client=_client(
            opensandbox_test_service, opensandbox_docker_runtime, fault_docker
        ),
        state=state,
        key_resolver=lambda key: key,
        observers=(observer, *additional_lifecycle_observers),
    ) as manager:
        owner = "reset-owner"
        backend = await manager.get(owner)
        original_id = backend.id
        for index in range(2):
            content = f"real-reset-{index}".encode()
            assert (await backend.aupload_files([("/sentinel.bin", content)]))[
                0
            ].error is None
            assert (await backend.adownload_files(["/sentinel.bin"]))[
                0
            ].content == content
            await manager.reset(owner)
            assert backend.id == original_id
            missing = (await backend.adownload_files(["/sentinel.bin"]))[0]
            fault_evidence.record(
                "download_after_reset",
                sandbox_id=original_id,
                iteration=index,
                error=missing.error,
                content_is_none=missing.content is None,
            )
            assert missing.error == "file_not_found"
            assert missing.content is None
            binding = await state.read_binding(_resource_key(owner))
            assert binding is not None and binding.sandbox_id == original_id
            fault_evidence.record(
                "reset_verified",
                sandbox_id=original_id,
                prior_file_sha256=hashlib.sha256(content).hexdigest(),
                file_missing=True,
            )
        await manager.destroy(owner)
        await manager.destroy(owner)
        assert await state.read_binding(_resource_key(owner)) is None
        assert await manager.get_details(owner) is None
        assert (await fault_docker.snapshot(original_id))["status"] == "absent"
    assert observer.kinds == [
        Kind.WORKSPACE_RESET,
        Kind.WORKSPACE_RESET,
        Kind.DESTROYED,
    ]
    assert [event.reason for event in observer.events] == [
        Reason.EXPLICIT_RESET,
        Reason.EXPLICIT_RESET,
        Reason.EXPLICIT_DESTROY,
    ]
    assert all(event.workspace_may_have_changed for event in observer.events)
    _assert_event_identity(observer, owner, original_id)
    _assert_no_named_tasks(fault_evidence)


@pytest.mark.asyncio
async def test_real_stopped_warm_container_reports_capacity_loss_and_restoration(
    sql_engine: SqlEngineFactory,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    fault_docker: _OwnedDocker,
    fault_evidence: _Evidence,
    additional_lifecycle_observers: list[OpenSandboxLifecycleObserver],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real background warm checks report capacity changes independently of owners."""
    # Use a short maintenance interval to observe background health changes
    # while keeping the remote Sandbox lifetime valid.
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle._manager_resources._WARM_MAINTENANCE_MAX_SECONDS",
        1.0,
    )
    observer = _Recorder(fault_evidence)
    client = _client(
        opensandbox_test_service,
        opensandbox_docker_runtime,
        fault_docker,
        warm_pool_size=1,
        # The server's CreateSandboxRequest.timeout requires at least 60 seconds.
        ttl=timedelta(seconds=60),
    )
    async with OpenSandboxManager[str](
        client=client,
        state=SQLAlchemyOpenSandboxState(
            engine=sql_engine(f"sqlite+aiosqlite:///{tmp_path / 'warm.db'}"),
            namespace=fault_docker.purpose,
        ),
        key_resolver=lambda key: key,
        observers=(observer, *additional_lifecycle_observers),
        fail_on_startup_warmup_error=True,
    ) as manager:
        await manager.check_ready()
        warm_ids = await fault_docker.ids()
        assert len(warm_ids) == 1
        warm_id = warm_ids[0]
        assert observer.events == []
        assert (await fault_docker.change(warm_id, "stop"))["status"] == "exited"
        await observer.wait_for(Kind.WARM_CAPACITY_DEGRADED, timeout=30)
        with pytest.raises(OpenSandboxWarmPoolUnavailableError) as captured:
            await manager.check_ready()
        fault_evidence.record("warm_unready", code=captured.value.code.value)
        assert (await fault_docker.change(warm_id, "start"))["status"] == "running"
        await observer.wait_for(Kind.WARM_CAPACITY_RESTORED, timeout=30)
        await manager.check_ready()
        assert await fault_docker.ids() == (warm_id,)
        runtime = await client.inspect(warm_id)
        assert runtime.available and runtime.healthy
        await manager.check_ready()
        fault_evidence.record(
            "warm_restored", sandbox_id=warm_id, healthy=runtime.healthy
        )
    assert observer.kinds == [Kind.WARM_CAPACITY_DEGRADED, Kind.WARM_CAPACITY_RESTORED]
    assert [event.reason for event in observer.events] == [
        Reason.WARM_CAPACITY_UNAVAILABLE,
        Reason.WARM_CAPACITY_AVAILABLE,
    ]
    assert all(event.owner_key is None for event in observer.events)
    assert all(not event.workspace_may_have_changed for event in observer.events)
    assert all(not event.diagnostic_context for event in observer.events)
    _assert_no_named_tasks(fault_evidence)


@pytest.mark.asyncio
async def test_real_manager_drops_full_observer_queue_without_delaying_other_observers(
    sql_engine: SqlEngineFactory,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    fault_docker: _OwnedDocker,
    fault_evidence: _Evidence,
    additional_lifecycle_observers: list[OpenSandboxLifecycleObserver],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Actual reset completions fill one queue while another observer keeps up."""
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedObserver:
        """Retain one borrowed callback until all real resource operations finish."""

        def __init__(self) -> None:
            self.events: list[OpenSandboxLifecycleEvent] = []
            self.active = 0

        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            self.active += 1
            entered.set()
            try:
                await release.wait()
                self.events.append(event)
            finally:
                self.active -= 1

    slow = GatedObserver()
    observer = _Recorder(fault_evidence)
    manager = OpenSandboxManager[str](
        client=_client(
            opensandbox_test_service, opensandbox_docker_runtime, fault_docker
        ),
        state=SQLAlchemyOpenSandboxState(
            engine=sql_engine(f"sqlite+aiosqlite:///{tmp_path / 'observer-queue.db'}"),
            namespace=fault_docker.purpose,
        ),
        key_resolver=lambda key: key,
        observers=(slow, observer, *additional_lifecycle_observers),
        notification_options=OpenSandboxNotificationOptions(
            max_pending_events=1, timeout=15
        ),
    )
    with caplog.at_level(logging.WARNING, logger="tinkerfin.sandbox.notifications"):
        try:
            await manager.start()
            owner = "queue-owner"
            backend = await manager.get(owner)
            original_id = backend.id
            for index in range(3):
                content = f"queued-observer-{index}".encode()
                assert (await backend.aupload_files([("/sentinel.bin", content)]))[
                    0
                ].error is None
                await manager.reset(owner)
                missing = (await backend.adownload_files(["/sentinel.bin"]))[0]
                fault_evidence.record(
                    "download_after_queue_reset",
                    sandbox_id=original_id,
                    iteration=index,
                    error=missing.error,
                    content_is_none=missing.content is None,
                )
                assert missing.error == "file_not_found"
                assert missing.content is None
                await observer.wait_for(Kind.WORKSPACE_RESET, count=index + 1)
                async with asyncio.timeout(3):
                    await entered.wait()
            await manager.destroy(owner)
            await observer.wait_for(Kind.DESTROYED)
            assert slow.events == [] and slow.active == 1
        finally:
            release.set()
            await manager.aclose()
    assert observer.kinds == [Kind.WORKSPACE_RESET] * 3 + [Kind.DESTROYED]
    assert [event.event_id for event in slow.events] == [
        event.event_id for event in observer.events[:2]
    ]
    assert slow.active == 0
    notices = [
        record
        for record in caplog.records
        if record.name == "tinkerfin.sandbox.notifications"
    ]
    assert len(notices) == 1
    assert notices[0].__dict__["tinkerfin_queue_full"] == 2
    assert notices[0].__dict__["tinkerfin_callback_timeout"] == 0
    assert (await fault_docker.snapshot(original_id))["status"] == "absent"
    fault_evidence.record(
        "queue_saturation_verified",
        fast_callbacks=len(observer.events),
        slow_callbacks=len(slow.events),
        dropped=2,
        active=slow.active,
    )
    _assert_event_identity(observer, owner, original_id)
    _assert_no_named_tasks(fault_evidence)
