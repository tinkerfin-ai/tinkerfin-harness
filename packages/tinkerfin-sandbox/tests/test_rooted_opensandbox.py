"""Real OpenSandbox rooted descriptor operations on disposable Docker resources."""

from __future__ import annotations

import asyncio
import secrets
import shlex
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from deepagents.backends.protocol import INVALID_PATH
from docker import DockerClient
from opensandbox.config import ConnectionConfig
from testcontainers.core.container import DockerContainer
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
    _opensandbox_config,
    _stop_owned_container,
)
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxFileTooLargeError,
    OpenSandboxHandle,
    OpenSandboxLifecycleEvent,
    OpenSandboxLifecycleEventType,
    OpenSandboxManager,
    OpenSandboxRecoveryPolicy,
    RootedOpenSandboxBackend,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox.backends import _rooted_protocol
from tinkerfin_sandbox.backends.sdk import OpenSandboxBackend


@dataclass(frozen=True, slots=True)
class RaceCase:
    """One deterministic target-component replacement scenario."""

    name: str
    virtual_path: str
    outside_target: str
    setup_command: str
    swap_command: str


def _install_transfer_barrier(*, reached: str, release: str) -> str:
    original = _rooted_protocol._ROOTED_HELPER_SCRIPT
    function_start = original.index("def transfer_file(")
    function_end = original.find("\ndef ", function_start + 1)
    function_source = original[function_start:function_end]
    statement = "        canonical = canonical_parts(root, virtual_parts)"
    barrier = (
        statement
        + "\n"
        + f"        open({reached!r}, 'x').close()\n"
        + f"        while not os.path.exists({release!r}):\n"
        + "            time.sleep(0.01)"
    )
    if function_source.count(statement) != 1:
        raise RuntimeError("transfer helper barrier location is ambiguous")
    _rooted_protocol._ROOTED_HELPER_SCRIPT = (
        original[:function_start]
        + function_source.replace(statement, barrier)
        + original[function_end:]
    )
    return original


async def _wait_for_path(
    backend: OpenSandboxBackend,
    path: str,
    *,
    timeout: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    quoted = shlex.quote(path)
    while asyncio.get_running_loop().time() < deadline:
        if (await backend.aexecute(f"test -e {quoted}")).exit_code == 0:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"remote barrier was not reached: {path}")


async def _assert_outside_sentinel(
    backend: OpenSandboxBackend,
    path: str,
) -> None:
    response = (await backend.adownload_files([path]))[0]
    if response.error is not None or response.content != b"outside sentinel":
        raise AssertionError(f"outside sentinel changed: {path}, {response.error}")


async def _run_race(
    backend: OpenSandboxBackend,
    case: RaceCase,
    *,
    token: str,
) -> None:
    reached = f"/tmp/{token}-{case.name}.reached"
    release = f"/tmp/{token}-{case.name}.release"
    await backend.aexecute(case.setup_command)
    original = _install_transfer_barrier(reached=reached, release=release)
    transfer = None
    try:
        transfer = asyncio.create_task(
            backend._aupload_rooted_file(
                root="/workspace",
                path=case.virtual_path,
                content=b"must not reach outside",
            )
        )
        await _wait_for_path(backend, reached)
        swap = await backend.aexecute(case.swap_command)
        if swap.exit_code != 0:
            raise RuntimeError(f"race swap failed: {case.name}: {swap.output}")
        await backend.aexecute(f"touch {shlex.quote(release)}")
        response = await transfer
        if response.error != INVALID_PATH:
            raise AssertionError(
                f"race did not reject target: {case.name}: {response.error}"
            )
        await _assert_outside_sentinel(backend, case.outside_target)
    finally:
        _rooted_protocol._ROOTED_HELPER_SCRIPT = original
        try:
            await backend.aexecute(f"touch {shlex.quote(release)}")
        finally:
            if transfer is not None and not transfer.done():
                await asyncio.gather(transfer, return_exceptions=True)


def _race_cases(token: str) -> tuple[RaceCase, ...]:
    workspace_base = f"/workspace/{token}"
    outside_base = f"/tmp/{token}-outside"
    return (
        RaceCase(
            name="leaf",
            virtual_path=f"/{token}/leaf/target.bin",
            outside_target=f"{outside_base}/leaf.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/leaf {outside_base}; "
                f"printf 'inside' > {workspace_base}/leaf/target.bin; "
                f"printf 'outside sentinel' > {outside_base}/leaf.bin"
            ),
            swap_command=(
                f"rm -f {workspace_base}/leaf/target.bin; "
                f"ln -s {outside_base}/leaf.bin "
                f"{workspace_base}/leaf/target.bin"
            ),
        ),
        RaceCase(
            name="parent",
            virtual_path=f"/{token}/parent/target.bin",
            outside_target=f"{outside_base}/parent/target.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/parent {outside_base}/parent; "
                f"printf 'inside' > {workspace_base}/parent/target.bin; "
                f"printf 'outside sentinel' > {outside_base}/parent/target.bin"
            ),
            swap_command=(
                f"mv {workspace_base}/parent {workspace_base}/parent-detached; "
                f"ln -s {outside_base}/parent {workspace_base}/parent"
            ),
        ),
        RaceCase(
            name="multilevel",
            virtual_path=f"/{token}/level1/level2/target.bin",
            outside_target=f"{outside_base}/multilevel/target.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/level1/level2 "
                f"{outside_base}/multilevel; "
                f"printf 'inside' > "
                f"{workspace_base}/level1/level2/target.bin; "
                f"printf 'outside sentinel' > "
                f"{outside_base}/multilevel/target.bin"
            ),
            swap_command=(
                f"mv {workspace_base}/level1/level2 "
                f"{workspace_base}/level1/level2-detached; "
                f"ln -s {outside_base}/multilevel "
                f"{workspace_base}/level1/level2"
            ),
        ),
    )


async def _wait_for_child_cleanup(
    docker_client: DockerClient,
    metadata: dict[str, str],
    *,
    timeout: float = 10.0,
) -> None:
    label, value = next(iter(metadata.items()))
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        owned = await asyncio.to_thread(
            docker_client.containers.list,
            all=True,
            filters={"label": f"{label}={value}"},
        )
        if not owned:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("OpenSandbox child container was not removed")


async def _owned_sandbox_ids(
    docker_client: DockerClient,
    *,
    label: str,
    value: str,
) -> tuple[str, ...]:
    """Return current remote IDs without blocking the async test loop."""

    containers = await asyncio.to_thread(
        docker_client.containers.list,
        all=True,
        filters={"label": f"{label}={value}"},
    )
    return tuple(str(container.labels["opensandbox.io/id"]) for container in containers)


async def _wait_for_owned_sandbox_count(
    docker_client: DockerClient,
    *,
    label: str,
    value: str,
    count: int,
    timeout: float = 15.0,
) -> tuple[str, ...]:
    """Wait for one exact number of test-owned remote Sandboxes."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        sandbox_ids = await _owned_sandbox_ids(
            docker_client,
            label=label,
            value=value,
        )
        if len(sandbox_ids) == count:
            return sandbox_ids
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} owned Sandboxes")


def _recreated_opensandbox_server(
    *,
    api_key: str,
    config_path: Path,
    metadata_dir: Path,
    runtime: OpenSandboxDockerRuntime,
) -> DockerContainer:
    """Build a Server sharing only the test daemon and test-owned metadata."""

    return runtime.configure_server(
        DockerContainer(
            "opensandbox/server:v0.2.3@sha256:"
            "ae8dfbb277f40a39ff01ef35e5e1c10675acfe0fa9db15259b8f323e5efab778"
        )
        .with_env("OPENSANDBOX_SERVER_API_KEY", api_key)
        .with_volume_mapping(
            str(metadata_dir),
            "/root/.opensandbox/metadata",
            "rw",
        )
        .with_copy_into_container(config_path, "/etc/opensandbox/config.toml")
    )


@pytest.mark.opensandbox_e2e
async def test_real_rooted_descriptor_transfers_reject_symlink_races(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
) -> None:
    """Exercise one real Sandbox and prove every owned container is destroyed."""

    token = f"tinkerfin-rooted-{uuid4().hex}"
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=opensandbox_test_service.domain,
            api_key=opensandbox_test_service.api_key,
            request_timeout=timedelta(minutes=3),
            use_server_proxy=True,
        ),
        config=OpenSandboxConfig(
            image=opensandbox_docker_runtime.image,
            workspace_root="/workspace",
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            command_timeout=180,
        ),
    )
    backend: OpenSandboxBackend | None = None
    completed: list[str] = []
    try:
        backend = await client.create(
            metadata={
                "purpose": "rooted-integration",
                **opensandbox_test_service.sandbox_metadata,
            }
        )
        content = bytes(range(256)) * (64 * 1024)
        large_path = f"/{token}/large.bin"
        uploaded = await backend._aupload_rooted_file(
            root="/workspace",
            path=large_path,
            content=content,
        )
        assert uploaded.error is None
        downloaded = await backend._adownload_rooted_file(
            root="/workspace",
            path=large_path,
        )
        assert downloaded.error is None
        assert downloaded.content == content
        completed.append("descriptor_transfer_16mib")
        view = RootedOpenSandboxBackend(OpenSandboxHandle(backend))
        assert await view.aread_bytes(large_path, max_bytes=len(content)) == content
        with pytest.raises(OpenSandboxFileTooLargeError):
            await view.aread_bytes(large_path, max_bytes=10 * 1024 * 1024)
        with pytest.raises(FileNotFoundError):
            await view.aread_bytes(f"/{token}/missing/nested/file.bin", max_bytes=4096)
        assert (
            await backend.aexecute(f"test ! -e /workspace/{token}/missing")
        ).exit_code == 0
        empty_path = f"/{token}/empty.bin"
        assert (await view.aupload_files([(empty_path, b"")]))[0].error is None
        assert await view.aread_bytes(empty_path, max_bytes=0) == b""
        assert (
            await backend.aexecute(
                f"ln -s /etc/passwd /workspace/{token}/escape; "
                f"mkfifo /workspace/{token}/fifo"
            )
        ).exit_code == 0
        with pytest.raises(OpenSandboxBackendProtocolError):
            await view.aread_bytes(f"/{token}/escape", max_bytes=4096)
        with pytest.raises(IsADirectoryError):
            await view.aread_bytes(f"/{token}/fifo", max_bytes=4096, timeout=5)

        for case in _race_cases(token):
            await _run_race(backend, case, token=token)
            completed.append(f"race_{case.name}")
    finally:
        if backend is not None:
            try:
                await backend.akill()
            finally:
                await backend.aclose()
        await client.aclose()

    assert completed == [
        "descriptor_transfer_16mib",
        "race_leaf",
        "race_parent",
        "race_multilevel",
    ]
    await _wait_for_child_cleanup(
        opensandbox_docker_runtime.client,
        opensandbox_test_service.sandbox_metadata,
    )


@pytest.mark.opensandbox_e2e
async def test_real_manual_cleanup_sandbox_preserves_files_across_manager_restart(
    sql_engine: SqlEngineFactory,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    tmp_path: Path,
) -> None:
    """Manual lifetime keeps remote files and durable bindings until explicit destroy."""

    purpose = f"manual-lifetime-{uuid4().hex}"
    config = OpenSandboxConfig(
        image=opensandbox_docker_runtime.image,
        workspace_root="/workspace",
        warm_pool_size=1,
        ttl=None,
        metadata={"purpose": purpose, **opensandbox_test_service.sandbox_metadata},
    )
    connection = ConnectionConfig(
        domain=opensandbox_test_service.domain,
        api_key=opensandbox_test_service.api_key,
        request_timeout=timedelta(minutes=2),
        use_server_proxy=True,
    )
    state_url = f"sqlite+aiosqlite:///{tmp_path / 'manual-lifetime.db'}"
    first_client = OpenSandboxClient(connection_config=connection, config=config)
    first = OpenSandboxManager[str](
        client=first_client,
        key_resolver=lambda value: value,
        state=SQLAlchemyOpenSandboxState(
            engine=sql_engine(state_url), namespace=purpose
        ),
        fail_on_startup_warmup_error=True,
    )
    second: OpenSandboxManager[str] | None = None
    try:
        await first.start()
        await first.check_ready()
        owner = await first.get("project")
        original_id = owner.id
        write = await owner.aexecute(
            "printf manual-lifetime-content > /workspace/manual-lifetime.txt"
        )
        assert write.exit_code == 0
        details = await first.get_details("project")
        assert details is not None and details.available and details.healthy
        assert details.expires_at is None
        await _wait_for_owned_sandbox_count(
            opensandbox_docker_runtime.client,
            label="purpose",
            value=purpose,
            count=2,
        )
        await first.aclose()

        second_client = OpenSandboxClient(connection_config=connection, config=config)
        second = OpenSandboxManager[str](
            client=second_client,
            key_resolver=lambda value: value,
            state=SQLAlchemyOpenSandboxState(
                engine=sql_engine(state_url), namespace=purpose
            ),
            fail_on_startup_warmup_error=True,
        )
        await second.start()
        await second.check_ready()
        restored = await second.reconnect("project")
        assert restored.id == original_id
        assert await second.get("project") is restored
        read = await restored.aexecute("cat /workspace/manual-lifetime.txt")
        assert read.exit_code == 0
        assert read.output == "manual-lifetime-content"
        for sandbox_id in await _owned_sandbox_ids(
            opensandbox_docker_runtime.client, label="purpose", value=purpose
        ):
            info = await second_client.inspect(sandbox_id)
            assert info.available and info.healthy
            assert info.expires_at is None
        await second.destroy("project")
        assert await second.get_details("project") is None
        assert (
            await second_client.inspect(original_id)
        ).unavailable_reason == "not_found"
    finally:
        if second is not None:
            await second.aclose()
        await first.aclose()
        cleanup_client = OpenSandboxClient(connection_config=connection, config=config)
        try:
            for sandbox_id in await _owned_sandbox_ids(
                opensandbox_docker_runtime.client, label="purpose", value=purpose
            ):
                await cleanup_client.destroy(sandbox_id)
        finally:
            await cleanup_client.aclose()


@pytest.mark.docker_integration
@pytest.mark.opensandbox_e2e
async def test_recreated_server_restores_persisted_expiration_override(
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    docker_test_run_id: str,
    tmp_path: Path,
) -> None:
    """A Server replacement must honor the latest Docker expiration metadata."""

    api_key = secrets.token_urlsafe(32)
    config_path = tmp_path / "config.toml"
    await asyncio.to_thread(
        config_path.write_text,
        _opensandbox_config(docker_host="127.0.0.1"),
        encoding="utf-8",
    )
    metadata_dir = tmp_path / "metadata"
    await asyncio.to_thread(metadata_dir.mkdir)
    purpose = f"server-restart-{uuid4().hex}"
    purpose_label = "purpose"
    sandbox_id: str | None = None
    try:
        first_server = _recreated_opensandbox_server(
            api_key=api_key,
            config_path=config_path,
            metadata_dir=metadata_dir,
            runtime=opensandbox_docker_runtime,
        )
        try:
            await asyncio.to_thread(first_server.start)
            first_domain = opensandbox_docker_runtime.domain
            connection = ConnectionConfig(
                domain=first_domain,
                api_key=api_key,
                request_timeout=timedelta(minutes=2),
                use_server_proxy=True,
            )
            client = OpenSandboxClient(
                connection_config=connection,
                config=OpenSandboxConfig(
                    image=opensandbox_docker_runtime.image,
                    workspace_root="/workspace",
                    warm_pool_size=0,
                    ttl=timedelta(seconds=60),
                    metadata={
                        purpose_label: purpose,
                        "tinkerfin.test/sandbox-run": docker_test_run_id,
                    },
                ),
            )
            backend = await client.create()
            sandbox_id = backend.id
            try:
                await backend.arenew(timedelta(seconds=12))
            finally:
                await backend.aclose()
                await client.aclose()
            expiration_file = metadata_dir / "_expiration" / f"{sandbox_id}.json"
            assert await asyncio.to_thread(expiration_file.is_file)
        finally:
            await asyncio.to_thread(_stop_owned_container, first_server)

        second_server = _recreated_opensandbox_server(
            api_key=api_key,
            config_path=config_path,
            metadata_dir=metadata_dir,
            runtime=opensandbox_docker_runtime,
        )
        try:
            await asyncio.to_thread(second_server.start)
            await _wait_for_owned_sandbox_count(
                opensandbox_docker_runtime.client,
                label=purpose_label,
                value=purpose,
                count=0,
                timeout=20.0,
            )
        finally:
            await asyncio.to_thread(_stop_owned_container, second_server)
    finally:
        remaining = await asyncio.to_thread(
            opensandbox_docker_runtime.client.containers.list,
            all=True,
            filters={"label": f"{purpose_label}={purpose}"},
        )
        for container in remaining:
            await asyncio.to_thread(container.remove, force=True)


@pytest.mark.opensandbox_e2e
async def test_real_recovery_preserves_files_and_requires_opt_in_for_recreation(
    sql_engine: SqlEngineFactory,
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    tmp_path: Path,
) -> None:
    """Verify recovery and lifecycle notices against real test-owned container files."""

    events: list[OpenSandboxLifecycleEvent] = []

    class Observer:
        async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
            events.append(event)

    observer = Observer()
    owner = "recovery-owner"
    namespace = f"recovery-{uuid4().hex}"
    url = f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}"
    config = OpenSandboxConfig(
        image=opensandbox_docker_runtime.image,
        warm_pool_size=0,
        workspace_root="/workspace",
        ttl=timedelta(minutes=20),
        health_command="test ! -e /workspace/.probe-down",
        metadata=opensandbox_test_service.sandbox_metadata,
    )
    connection = ConnectionConfig(
        domain=opensandbox_test_service.domain,
        api_key=opensandbox_test_service.api_key,
        use_server_proxy=True,
    )
    async with OpenSandboxManager[str](
        client=OpenSandboxClient(connection_config=connection, config=config),
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace=namespace),
        key_resolver=lambda key: key,
        observers=(observer,),
    ) as preserving:
        backend = await preserving.get(owner)
        original_id = backend.id
        upload = await backend.aupload_files([("/sentinel.txt", b"preserved contents")])
        assert upload[0].error is None
        await backend.aexecute("touch /workspace/.probe-down")

        with pytest.raises(OpenSandboxBackendUnavailableError):
            await preserving.get(owner)
        assert backend.id == original_id
        assert (await backend.adownload_files(["/sentinel.txt"]))[
            0
        ].content == b"preserved contents"
        await backend.aexecute("rm /workspace/.probe-down")
        assert (await preserving.get(owner)).id == original_id
        assert (await backend.adownload_files(["/sentinel.txt"]))[
            0
        ].content == b"preserved contents"

        await backend.aexecute("touch /workspace/.probe-down")
        async with OpenSandboxManager[str](
            client=OpenSandboxClient(connection_config=connection, config=config),
            state=SQLAlchemyOpenSandboxState(
                engine=sql_engine(url), namespace=namespace
            ),
            key_resolver=lambda key: key,
            recovery_policy=OpenSandboxRecoveryPolicy(
                max_attempts=1, on_failure="recreate"
            ),
            observers=(observer,),
        ) as recreating:
            replacement = await recreating.get(owner)
            assert replacement.id != original_id
            assert (await replacement.adownload_files(["/sentinel.txt"]))[
                0
            ].error is not None
            await recreating.destroy(owner)

    replaced = [
        event
        for event in events
        if event.type is OpenSandboxLifecycleEventType.REPLACED
    ]
    assert len(replaced) == 1
    assert replaced[0].workspace_may_have_changed
    assert any(
        event.type is OpenSandboxLifecycleEventType.RECOVERED for event in events
    )
    assert (
        sum(event.type is OpenSandboxLifecycleEventType.DESTROYED for event in events)
        == 1
    )


async def test_recreated_server_start_failure_closes_its_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Settle a partially started test Server before handing control to Pytest."""
    from unittest.mock import Mock

    container = Mock(spec=DockerContainer)
    container.start.side_effect = RuntimeError("synthetic Server readiness failure")
    docker_client = Mock(spec=DockerClient)
    docker_client.containers.list.return_value = []
    runtime = OpenSandboxDockerRuntime(
        client=docker_client,
        container_id="synthetic-daemon",
        domain="127.0.0.1:1",
        image="synthetic-image",
        run_id="synthetic-run",
    )
    monkeypatch.setitem(
        globals(),
        "_recreated_opensandbox_server",
        lambda **kwargs: container,
    )

    with pytest.raises(RuntimeError, match="readiness failure"):
        await test_recreated_server_restores_persisted_expiration_override(
            runtime, "synthetic-run", tmp_path
        )
    container.stop.assert_called_once_with()
