"""Pure contracts for repository Docker service descriptors."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any, cast
from unittest.mock import Mock
from urllib.request import ProxyHandler

import pytest
from docker import DockerClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import WaitStrategyTarget

from tests.support.docker_services import (
    MySQLTestService,
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
    RedisTestService,
    _MappedPortHttpWaitStrategy,
    _with_loopback_port,
)


class _MissingMappedPort:
    """Represent a container whose host port has not been published."""

    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def get_exposed_port(self, port: int) -> int:
        assert port == 8090
        raise ConnectionError("mapping not published")


def test_opensandbox_wait_bounds_missing_docker_port_publication() -> None:
    target = _MissingMappedPort()
    strategy = _MappedPortHttpWaitStrategy(
        8090,
        "/health",
        mapping_timeout_seconds=0,
    ).with_poll_interval(0)

    with pytest.raises(TimeoutError, match="host-port mapping"):
        strategy._build_url(cast(WaitStrategyTarget, target))


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ("127.0.0.1", 0)),
        ("linux", ("127.0.0.1", 0)),
    ],
)
def test_service_ports_bind_only_to_loopback_on_each_platform(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: object,
) -> None:
    monkeypatch.setattr("tests.support.docker_services.sys.platform", platform)
    monkeypatch.setattr("testcontainers.core.container.DockerClient", Mock())
    container = _with_loopback_port(DockerContainer("fixture-image"), 8090)
    _with_loopback_port(container, 2375)

    assert container.ports["8090"] == expected
    assert container.ports["2375"] == expected


def test_opensandbox_health_probe_ignores_host_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: list[object] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class Opener:
        def open(self, request: object, *, timeout: int) -> Response:
            del request
            assert timeout == 1
            return Response()

    def opener(*handlers: object) -> Opener:
        captured_handlers.extend(handlers)
        return Opener()

    monkeypatch.setattr("tests.support.docker_services.build_opener", opener)
    strategy = _MappedPortHttpWaitStrategy(8090, "/health")

    assert strategy._try_http_request("http://127.0.0.1:50000/health", {}, None)
    proxy = next(
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    )
    assert cast(Any, proxy).proxies == {}


def test_redis_service_selects_one_explicit_logical_database() -> None:
    service = RedisTestService(host="127.0.0.1", port=12345)

    assert service.url(0) == "redis://127.0.0.1:12345/0"
    assert service.url(15) == "redis://127.0.0.1:12345/15"


def test_mysql_service_builds_an_encoded_async_url() -> None:
    service = MySQLTestService(
        host="127.0.0.1",
        port=12346,
        password="secret:/ value",
    )

    url = make_url(service.url("sandbox_test"))

    assert url.drivername == "mysql+asyncmy"
    assert url.username == "root"
    assert url.password == "secret:/ value"
    assert url.host == "127.0.0.1"
    assert url.port == 12346
    assert url.database == "sandbox_test"
    assert url.query == {"charset": "utf8mb4"}


@pytest.mark.docker_integration
async def test_redis_stack_fixture_exposes_ordinary_and_search_databases(
    redis_url: str,
    redis_checkpoint_url: str,
) -> None:
    ordinary = Redis.from_url(redis_url, decode_responses=True)
    checkpoint = Redis.from_url(redis_checkpoint_url, decode_responses=True)
    try:
        assert await cast(Awaitable[bool], ordinary.ping()) is True
        assert isinstance(await checkpoint.execute_command("FT._LIST"), list)
    finally:
        await ordinary.aclose()
        await checkpoint.aclose()


@pytest.mark.docker_integration
async def test_mysql_fixture_runs_the_deployed_server_line(
    mysql_admin_url: str,
) -> None:
    engine = create_async_engine(mysql_admin_url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT VERSION()"))
    finally:
        await engine.dispose()

    assert isinstance(version, str)
    assert version.startswith("8.4.")


def test_runtime_construction_failure_releases_its_owned_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from docker import DockerClient
    from docker.models.networks import Network

    from tests.support import docker_services

    client = Mock(spec=DockerClient)
    network = Mock(spec=Network)
    client.networks.create.return_value = network

    def fail_construction(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic daemon construction failure")

    monkeypatch.setattr(docker_services, "DockerContainer", fail_construction)
    runtime = inspect.unwrap(docker_services.opensandbox_docker_runtime)(
        client, "synthetic-run"
    )
    with pytest.raises(RuntimeError, match="construction failure"):
        next(runtime)
    network.remove.assert_called_once_with()


@pytest.mark.parametrize("missing", [False, True])
def test_local_cleanup_is_idempotent_and_always_closes_the_sdk_session(
    missing: bool,
) -> None:
    from unittest.mock import Mock

    from docker.errors import NotFound
    from docker.models.networks import Network

    from tests.support.docker_services import (
        _remove_owned_network,
        _stop_owned_container,
    )

    container = Mock(spec=DockerContainer)
    network = Mock(spec=Network)
    if missing:
        container.stop.side_effect = NotFound("already removed")
        network.remove.side_effect = NotFound("already removed")
    for _ in range(2):
        _stop_owned_container(container)
        _remove_owned_network(network)
    assert container.get_docker_client.return_value.client.close.call_count == 2
    assert network.remove.call_count == 2


def test_start_failure_and_repeated_cleanup_leave_no_owned_container() -> None:
    from unittest.mock import Mock

    from docker.errors import NotFound

    from tests.support.docker_services import _running_container, _stop_owned_container

    container = Mock(spec=DockerContainer)
    container.start.side_effect = RuntimeError("synthetic readiness failure")
    with (
        pytest.raises(RuntimeError, match="readiness failure"),
        _running_container(container),
    ):
        pytest.fail("Failed container must not be yielded")
    container.stop.assert_called_once_with()
    container.stop.side_effect = NotFound("already removed")
    _stop_owned_container(container)
    assert container.get_docker_client.return_value.client.close.call_count == 2


def test_container_cleanup_closes_session_even_when_removal_fails() -> None:
    from unittest.mock import Mock

    from docker.errors import APIError

    from tests.support.docker_services import _stop_owned_container

    container = Mock(spec=DockerContainer)
    container.stop.side_effect = APIError("synthetic removal failure")
    with pytest.raises(APIError, match="removal failure"):
        _stop_owned_container(container)
    container.get_docker_client.return_value.client.close.assert_called_once_with()


@pytest.mark.opensandbox_e2e
def test_opensandbox_fixture_uses_an_independent_daemon_and_owned_mounts(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    docker_test_client: DockerClient,
    docker_test_run_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from pathlib import Path

    from tinkerfin_sandbox import OpenSandboxConfig

    runtime = opensandbox_docker_runtime
    assert runtime.client.info()["ID"] != docker_test_client.info()["ID"]
    daemon = docker_test_client.containers.get(runtime.container_id)
    assert daemon.attrs["HostConfig"]["Privileged"]
    assert all(mount["Type"] != "bind" for mount in daemon.attrs["Mounts"])
    for port in ("2375/tcp", "8090/tcp"):
        bindings = daemon.attrs["NetworkSettings"]["Ports"][port]
        assert bindings and all(
            binding["HostIp"] == "127.0.0.1" for binding in bindings
        )
    assert opensandbox_test_service.domain == runtime.domain
    source_id = docker_test_client.images.get(OpenSandboxConfig().image).id
    assert runtime.client.images.get(runtime.image).id == source_id
    assert (
        runtime.client.images.get("opensandbox/execd:v1.0.22").id
        == docker_test_client.images.get("opensandbox/execd:v1.0.22").id
    )
    servers = [
        container
        for container in docker_test_client.containers.list(
            all=True, filters={"label": f"tinkerfin.test/run={docker_test_run_id}"}
        )
        if container.id != runtime.container_id
        and container.attrs["HostConfig"]["NetworkMode"]
        == f"container:{runtime.container_id}"
    ]
    assert len(servers) == 1
    environment = dict(item.split("=", 1) for item in servers[0].attrs["Config"]["Env"])
    assert environment["DOCKER_HOST"] == "tcp://127.0.0.1:2375"
    mounts = servers[0].attrs["Mounts"]
    assert all("docker.sock" not in str(mount) for mount in mounts)
    assert len(mounts) == 1
    metadata = Path(mounts[0]["Source"])
    assert metadata.is_relative_to(tmp_path_factory.getbasetemp())
    assert mounts[0]["Destination"] == "/root/.opensandbox/metadata"


def test_root_fixture_closes_its_sdk_session_after_network_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docker.errors import APIError
    from docker.models.networks import Network

    from tests.support import docker_services

    client = Mock(spec=DockerClient)
    client.containers.list.return_value = []
    network = Mock(spec=Network)
    network.remove.side_effect = APIError("synthetic network cleanup failure")
    client.networks.list.return_value = [network]
    monkeypatch.setattr(docker_services.docker, "from_env", lambda: client)
    monkeypatch.setattr(
        docker_services.testcontainers_config,
        "ryuk_disabled",
        docker_services.testcontainers_config.ryuk_disabled,
    )
    fixture = inspect.unwrap(docker_services.docker_test_client)("synthetic-run")
    assert next(fixture) is client
    with pytest.raises(APIError, match="network cleanup failure"):
        next(fixture)
    client.close.assert_called_once_with()


def test_server_configuration_keeps_network_and_cleanup_labels_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support import docker_services

    owner = "synthetic-owner"
    transport = Mock()
    monkeypatch.setattr(
        "testcontainers.core.container.DockerClient", lambda **kwargs: transport
    )
    monkeypatch.setattr(docker_services.testcontainers_config, "ryuk_disabled", True)
    runtime = OpenSandboxDockerRuntime(
        client=Mock(spec=DockerClient),
        container_id="synthetic-daemon",
        domain="127.0.0.1:1",
        image="synthetic-image",
        run_id=owner,
    )
    container = DockerContainer("synthetic-server").with_kwargs(
        labels={"tinkerfin.test/run": owner}
    )
    runtime.configure_server(container).waiting_for(Mock())
    with docker_services._running_container(container):
        creation = transport.create.call_args.kwargs
        assert creation["network_mode"] == "container:synthetic-daemon"
        assert creation["labels"] == {"tinkerfin.test/run": owner}
        assert creation["volumes"] == {}
        assert creation["environment"]["DOCKER_HOST"] == "tcp://127.0.0.1:2375"


@pytest.mark.opensandbox_e2e
@pytest.mark.parametrize("failure", ["construction", "startup", "already_removed"])
def test_real_runtime_setup_failure_cleans_daemon_network_and_volumes(
    docker_test_client: DockerClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from uuid import uuid4

    from docker.errors import NotFound

    from tests.support import docker_services

    run_id = uuid4().hex
    label_filter: dict[str, str | list[str] | bool] = {
        "label": f"tinkerfin.test/run={run_id}"
    }
    volume_names: list[str] = []

    def construction_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic construction failure")

    def after_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        owned = docker_test_client.containers.list(all=True, filters=label_filter)
        assert len(owned) == 1
        volume_names.extend(
            mount["Name"]
            for mount in owned[0].attrs["Mounts"]
            if mount["Type"] == "volume"
        )
        if failure == "already_removed":
            owned[0].remove(force=True, v=True)
            for network in docker_test_client.networks.list(filters=label_filter):
                network.remove()
        raise RuntimeError("synthetic startup failure")

    if failure == "construction":
        monkeypatch.setattr(docker_services, "DockerContainer", construction_failure)
    elif failure == "startup":
        wait = Mock()
        wait.with_startup_timeout.return_value = wait
        wait.wait_until_ready.side_effect = after_start
        monkeypatch.setattr(
            docker_services, "ExecWaitStrategy", lambda *args, **kwargs: wait
        )
    else:
        monkeypatch.setattr(docker_services, "_copy_runtime_image", after_start)
    generator = inspect.unwrap(docker_services.opensandbox_docker_runtime)(
        docker_test_client, run_id
    )
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            next(generator)
        generator.close()
        assert not docker_test_client.containers.list(all=True, filters=label_filter)
        assert not docker_test_client.networks.list(filters=label_filter)
        for name in volume_names:
            with pytest.raises(NotFound):
                docker_test_client.volumes.get(name)
    finally:
        # This fallback owns only the fresh UUID used by this invocation, so even
        # a failed cleanup assertion cannot leave resources on the host daemon.
        generator.close()
        for container in docker_test_client.containers.list(
            all=True, filters=label_filter
        ):
            container.remove(force=True, v=True)
        for network in docker_test_client.networks.list(filters=label_filter):
            network.remove()
