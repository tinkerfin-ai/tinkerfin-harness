"""Disposable Docker services shared by repository-wide integration tests.

The fixtures own every container they create and expose only loopback URLs with
random host ports. A missing Docker daemon skips Docker integration tests; once
the daemon responds, image pulls, startup, readiness, and cleanup failures remain
real test failures.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sys
import time
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import (
    BaseHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4

import docker
import pytest
import pytest_asyncio
from docker import DockerClient
from docker.errors import DockerException, ImageLoadError, ImageNotFound, NotFound
from docker.models.networks import Network
from docker.types import LogConfig
from requests import Response
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy, HttpWaitStrategy
from testcontainers.core.waiting_utils import WaitStrategyTarget

_MYSQL_IMAGE = (
    "mysql:8.4@sha256:b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb"
)
_MYSQL57_IMAGE = (
    "mysql:5.7@sha256:4bc6bc963e6d8443453676cae56536f4b8156d78bae03c0145cbe47c2aad73bb"
)
_POSTGRESQL_IMAGE = "postgres:17@sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675"
_REDIS_STACK_IMAGE = (
    "redis/redis-stack-server:7.4.0-v8@sha256:"
    "798ab84d9f266936b034ab11c4d04a2b8e4b441884c5aa7d17ac951eefdf742a"
)
_OPENSANDBOX_SERVER_IMAGE = (
    "ghcr.io/tinkerfin-ai/opensandbox-server:0.2.3-tinkerfin.1@sha256:"
    "238060e23a14ebd22e437b15100e8c939f6865de60075d2edd488977c24c1fda"
)
_OPENSANDBOX_EXECD_IMAGE = (
    "ghcr.io/tinkerfin-ai/opensandbox-execd:1.0.22-tinkerfin.1@sha256:"
    "50fcc0386fb893e56eee047c87b3bae40ed2451629606feeaa605ab283df06ce"
)
_DIND_IMAGE = (
    "docker:28.3.3-dind@sha256:"
    "a56b3bdde89315ed2cc0e4906e582b5033d93bf20d9cb9510c2cdd4e7f7690b1"
)
_DNS_CONFIG_MAX_BYTES = 64 * 1024
_TEST_LABEL = "tinkerfin.test/run"
_SANDBOX_TEST_LABEL = "tinkerfin.test/sandbox-run"
_MYSQL_SANDBOX_DATABASE_PATTERN = re.compile(r"\Atinkerfin_sandbox_[a-f0-9]{32}\Z")
_DOCKER_FIXTURE_NAMES = frozenset(
    {
        "docker_test_client",
        "docker_platform_dns_servers",
        "mysql_admin_url",
        "mysql_sandbox_url",
        "mysql_test_service",
        "mysql57_test_service",
        "postgresql_test_service",
        "opensandbox_test_service",
        "opensandbox_docker_runtime",
        "redis_checkpoint_url",
        "redis_test_service",
        "redis_url",
    }
)


class _MappedPortHttpWaitStrategy(HttpWaitStrategy):
    """Retry Docker's mapped-port publication before starting HTTP readiness.

    Docker Desktop can report a container as running a few milliseconds before its
    random host-port mapping becomes visible. Testcontainers 4.15 builds the HTTP URL
    once, so that narrow daemon race otherwise aborts a healthy container immediately.
    The probe re-resolves that mapping, bypasses host proxy settings for loopback, and
    keeps the inherited status-code and response checks within one startup timeout.
    """

    def __init__(
        self,
        port: int,
        path: str,
        *,
        mapping_timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(port, path)
        self._mapping_timeout_seconds = mapping_timeout_seconds

    def _build_url(self, container: WaitStrategyTarget) -> str:
        deadline = time.monotonic() + self._mapping_timeout_seconds
        while True:
            try:
                url = super()._build_url(container)
                if sys.platform == "darwin":
                    return url.replace("://localhost:", "://127.0.0.1:", 1)
                return url
            except ConnectionError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Docker did not publish the OpenSandbox host-port mapping "
                        f"within {self._mapping_timeout_seconds:g} seconds"
                    ) from error
                time.sleep(self._poll_interval)

    def wait_until_ready(self, container: WaitStrategyTarget) -> None:
        """Re-resolve a random host port on every bounded HTTP attempt."""

        started_at = time.monotonic()
        headers = self._setup_headers()
        ssl_context = self._setup_ssl_context()
        last_url = f"http://unresolved:{self._port}{self._path}"
        while True:
            if time.monotonic() - started_at > self._startup_timeout:
                self._raise_timeout_error(last_url)
            last_url = self._build_url(container)
            if self._try_http_request(last_url, headers, ssl_context):
                return
            time.sleep(self._poll_interval)

    def _try_http_request(
        self,
        url: str,
        headers: dict[str, str],
        ssl_context: Any,
    ) -> bool:
        """Probe loopback directly without macOS or host proxy configuration."""

        handlers: list[BaseHandler] = [ProxyHandler({})]
        if ssl_context is not None:
            handlers.append(HTTPSHandler(context=ssl_context))
        opener = build_opener(*handlers)
        request = Request(
            url,
            headers=headers,
            method=self._method,
            data=self._body.encode() if self._body else None,
        )
        try:
            with opener.open(request, timeout=1) as response:
                return self._check_response(response, url)
        except (URLError, HTTPError) as error:
            return self._handle_http_error(error)
        except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError, OSError):
            return False


def _with_loopback_port(container: DockerContainer, port: int) -> DockerContainer:
    """Expose one test service on a random, explicitly loopback-bound port."""

    container.with_exposed_ports(port)
    ports = cast(dict[str, object], container.ports)
    ports[str(port)] = ("127.0.0.1", 0)
    return container


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Classify tests that consume a disposable Docker service fixture."""

    marker = pytest.mark.docker_integration
    for item in items:
        if isinstance(item, pytest.Function) and _DOCKER_FIXTURE_NAMES.intersection(
            item.fixturenames
        ):
            item.add_marker(marker)


@dataclass(frozen=True, slots=True)
class MySQLTestService:
    """One session-owned MySQL server with an internal random root password."""

    host: str
    port: int
    password: str

    def url(self, database: str) -> str:
        """Return one async SQLAlchemy URL for a database on this server."""

        return URL.create(
            drivername="mysql+asyncmy",
            username="root",
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
            query={"charset": "utf8mb4"},
        ).render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class RedisTestService:
    """One session-owned Redis Stack server without authentication."""

    host: str
    port: int

    def url(self, database: int) -> str:
        """Return one Redis URL for the selected logical database."""

        if database < 0:
            raise ValueError("Redis database must be non-negative")
        return f"redis://{self.host}:{self.port}/{database}"


@dataclass(frozen=True, slots=True)
class PostgreSQLTestService:
    """One session-owned PostgreSQL server with a random test credential."""

    host: str
    port: int
    password: str

    def url(self, database: str = "tinkerfin_test") -> str:
        """Return an asyncpg SQLAlchemy URL without exposing credentials in reports."""

        return URL.create(
            drivername="postgresql+asyncpg",
            username="postgres",
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
        ).render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class OpenSandboxTestService:
    """One session-owned OpenSandbox Server and its child ownership label."""

    domain: str
    api_key: str
    run_id: str

    @property
    def sandbox_metadata(self) -> dict[str, str]:
        """Return metadata that makes every child Sandbox exactly reclaimable."""

        return {_SANDBOX_TEST_LABEL: self.run_id}


@pytest.fixture(scope="session")
def docker_test_run_id() -> str:
    """Return the unique ownership label value for one Pytest process."""

    return uuid4().hex


@pytest.fixture(scope="session")
def docker_test_client(docker_test_run_id: str) -> Iterator[DockerClient]:
    """Yield a verified Docker client or skip tests when no daemon is available."""

    client: DockerClient | None = None
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as error:
        if client is not None:
            client.close()
        pytest.skip(f"Docker daemon is unavailable: {type(error).__name__}")
    assert client is not None
    # Docker Desktop on macOS exposes a VM-owned user socket that Ryuk cannot
    # bind mount. The exact-label finalizer remains active on every platform;
    # Linux CI additionally retains Testcontainers' out-of-process reaper.
    if sys.platform == "darwin":
        testcontainers_config.ryuk_disabled = True
    try:
        yield client
    finally:
        try:
            for label in (_SANDBOX_TEST_LABEL, _TEST_LABEL):
                owned = client.containers.list(
                    all=True,
                    filters={"label": f"{label}={docker_test_run_id}"},
                )
                for container in owned:
                    try:
                        container.remove(force=True, v=True)
                    except NotFound:
                        continue
            for network in client.networks.list(
                filters={"label": f"{_TEST_LABEL}={docker_test_run_id}"},
            ):
                try:
                    network.remove()
                except NotFound:
                    continue
        finally:
            client.close()


@pytest.fixture(scope="session")
def redis_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[RedisTestService]:
    """Start one Redis Stack container for ordinary and RediSearch tests."""

    del docker_test_client
    wait = (
        ExecWaitStrategy(
            [
                "sh",
                "-ec",
                "redis-cli ping | grep -q PONG && redis-cli FT._LIST >/dev/null",
            ]
        )
        .with_poll_interval(0.2)
        .with_startup_timeout(120)
    )
    container = (
        DockerContainer(_REDIS_STACK_IMAGE)
        .with_exposed_ports(6379)
        .with_kwargs(labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(wait)
    )
    with container:
        yield RedisTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(6379),
        )


@pytest.fixture(scope="session")
def redis_url(redis_test_service: RedisTestService) -> str:
    """Return the ordinary Redis test URL isolated on logical DB 15."""

    return redis_test_service.url(15)


@pytest.fixture(scope="session")
def redis_checkpoint_url(redis_test_service: RedisTestService) -> str:
    """Return the RediSearch-compatible checkpoint URL on logical DB 0."""

    return redis_test_service.url(0)


@pytest.fixture(scope="session")
def mysql_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[MySQLTestService]:
    """Start one MySQL 8.4 container for repository database integrations."""

    del docker_test_client
    password = secrets.token_urlsafe(24)
    wait = (
        ExecWaitStrategy(
            [
                "mysqladmin",
                "ping",
                "-h",
                "127.0.0.1",
                "-uroot",
                f"-p{password}",
                "--silent",
            ]
        )
        .with_poll_interval(0.5)
        .with_startup_timeout(180)
    )
    container = (
        DockerContainer(_MYSQL_IMAGE)
        .with_env("MYSQL_ROOT_PASSWORD", password)
        .with_env("MYSQL_DATABASE", "tinkerfin_test_admin")
        .with_exposed_ports(3306)
        .with_kwargs(labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(wait)
    )
    with container:
        yield MySQLTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(3306),
            password=password,
        )


@pytest.fixture(scope="session")
def mysql_admin_url(mysql_test_service: MySQLTestService) -> str:
    """Return the management URL used to create disposable test databases."""

    return mysql_test_service.url("tinkerfin_test_admin")


@pytest.fixture(scope="session")
def mysql57_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[MySQLTestService]:
    """Exercise actual MySQL 5.7 behavior using an independently owned server."""

    del docker_test_client
    password = secrets.token_urlsafe(24)
    container = (
        _with_loopback_port(DockerContainer(_MYSQL57_IMAGE), 3306)
        .with_env("MYSQL_ROOT_PASSWORD", password)
        .with_env("MYSQL_DATABASE", "tinkerfin_test_admin")
        .with_kwargs(platform="linux/amd64", labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(
            ExecWaitStrategy(
                [
                    "mysqladmin",
                    "ping",
                    "-h",
                    "127.0.0.1",
                    "-uroot",
                    f"-p{password}",
                    "--silent",
                ]
            )
            .with_poll_interval(0.5)
            .with_startup_timeout(180)
        )
    )
    with _running_container(container):
        yield MySQLTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(3306),
            password=password,
        )


@pytest.fixture(scope="session")
def postgresql_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[PostgreSQLTestService]:
    """Start PostgreSQL for isolated transaction and storage contract tests."""

    del docker_test_client
    password = secrets.token_urlsafe(24)
    container = (
        _with_loopback_port(DockerContainer(_POSTGRESQL_IMAGE), 5432)
        .with_env("POSTGRES_PASSWORD", password)
        .with_env("POSTGRES_DB", "tinkerfin_test")
        .with_kwargs(labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(
            ExecWaitStrategy(
                [
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "tinkerfin_test",
                ]
            )
            .with_poll_interval(0.2)
            .with_startup_timeout(120)
        )
    )
    with _running_container(container):
        yield PostgreSQLTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(5432),
            password=password,
        )


def _opensandbox_config(*, docker_host: str) -> str:
    """Return the isolated Docker runtime configuration used by the E2E server."""

    return f"""\
[server]
host = "0.0.0.0"
port = 8090
max_sandbox_timeout_seconds = 1200

[log]
level = "INFO"
file_enabled = false

[runtime]
type = "docker"
execd_image = "tinkerfin-execd-e2e:isolated"

[storage]
allowed_host_paths = []
volume_default_size = "1Gi"

[store]
type = "sqlite"
path = "/tmp/opensandbox-test.db"

[docker]
network_mode = "bridge"
host_ip = "{docker_host}"
port_range_min = 40000
port_range_max = 60000
drop_capabilities = [
    "AUDIT_WRITE",
    "MKNOD",
    "NET_ADMIN",
    "NET_RAW",
    "SYS_ADMIN",
    "SYS_MODULE",
    "SYS_PTRACE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
]
no_new_privileges = true
pids_limit = 4096

[ingress]
mode = "direct"

[egress]
image = "opensandbox/egress:v1.1.7"
mode = "dns"
"""


class OpenSandboxDockerRuntime:
    """Describe test-owned remote Docker resources borrowed by E2E services."""

    def __init__(
        self,
        *,
        client: DockerClient,
        container_id: str,
        domain: str,
        image: str,
        run_id: str,
    ) -> None:
        self.client = client
        self.container_id = container_id
        self.domain = domain
        self.image = image
        self.run_id = run_id

    def configure_server(self, container: DockerContainer) -> DockerContainer:
        """Connect a test Server only to this isolated Docker daemon."""
        # Testcontainers 4.15 with_kwargs replaces the complete mapping. Keep
        # isolation and cleanup ownership together in this single configuration.
        return (
            container.with_env("DOCKER_HOST", "tcp://127.0.0.1:2375")
            .with_env("NO_PROXY", "*")
            .with_env("no_proxy", "*")
            .with_kwargs(
                network_mode=f"container:{self.container_id}",
                labels={_TEST_LABEL: self.run_id},
            )
            .waiting_for(_SandboxServerWait(self.domain))
        )


class _SandboxServerWait(_MappedPortHttpWaitStrategy):
    """Reach the Server through the port published by its shared network namespace."""

    def __init__(self, domain: str) -> None:
        super().__init__(8090, "/health")
        self._domain = domain
        self.with_poll_interval(0.5)
        self.with_startup_timeout(180)

    def _build_url(self, container: WaitStrategyTarget) -> str:
        del container
        return f"http://{self._domain}/health"


def _stop_owned_container(container: DockerContainer) -> None:
    """Close a test-owned container and its SDK session, including repeated cleanup."""
    try:
        try:
            container.stop()
        except NotFound:
            pass
    finally:
        # Testcontainers 4.15 stop() skips client.close() when remove() raises.
        # requests Session.close() is idempotent, including successful stop().
        container.get_docker_client().client.close()


@contextmanager
def _running_container(container: DockerContainer) -> Iterator[DockerContainer]:
    """Own start failures and normal exit under the same local cleanup boundary."""
    try:
        container.start()
        yield container
    finally:
        _stop_owned_container(container)


def _remove_owned_network(network: Network) -> None:
    try:
        network.remove()
    except NotFound:
        pass


def _copy_runtime_image(
    source: DockerClient,
    target: DockerClient,
    *,
    reference: str,
    repository: str,
    tag: str,
    export_budget_seconds: float = 300.0,
    source_archive: Path | None = None,
) -> None:
    """Stream one immutable image between owned sessions, then verify its tag.

    Docker 7.2 Image.save() disables socket timeouts in _stream_raw_result. The
    public requests Session interface preserves the selected daemon and its
    30-second read timeout, including the wait for response headers. Export checks
    its total budget between 1 MiB chunks; in-flight I/O cannot be interrupted
    by that check. Requests streams the iterable upload with backpressure, without
    a host archive or a whole-image buffer. Both HTTP responses and the upload
    iterator close on failure. Import retains the target SDK's request timeout.
    An explicitly prepared archive bypasses daemon export, but must still load the
    exact source image ID. It remains borrowed and is never removed by this helper.
    """
    if export_budget_seconds <= 0:
        raise ValueError("Image export budget must be positive")
    try:
        source_image = source.images.get(reference)
    except ImageNotFound:
        source_image = source.images.pull(reference)
    source_id = source_image.id
    if (
        not isinstance(source_id, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", source_id) is None
    ):
        raise RuntimeError("The selected source image has no immutable content ID")
    endpoint = f"{source.api.base_url}/v{source.api.api_version}/images/{source_id}/get"
    deadline = time.monotonic() + export_budget_seconds
    with ExitStack() as export:
        if source_archive is None:
            response: Response = export.enter_context(
                source.api.get(
                    endpoint, stream=True, timeout=min(30.0, export_budget_seconds)
                )
            )
            response.raise_for_status()
            content = response.iter_content(chunk_size=1024 * 1024)
        else:
            archive = export.enter_context(source_archive.open("rb"))
            content = iter(lambda: archive.read(1024 * 1024), b"")

        def chunks() -> Generator[bytes, None, None]:
            for chunk in content:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Docker image export exceeded its budget")
                yield chunk
            if time.monotonic() >= deadline:
                raise TimeoutError("Docker image export exceeded its budget")

        target_endpoint = f"{target.api.base_url}/v{target.api.api_version}/images/load"
        with (
            closing(chunks()) as body,
            target.api.post(
                target_endpoint,
                data=body,
                stream=True,
                timeout=target.api.timeout,
                headers={"Content-Type": "application/x-tar"},
            ) as result,
        ):
            result.raise_for_status()
            for line in result.iter_lines():
                if not line:
                    continue
                progress = json.loads(line)
                if not isinstance(progress, dict):
                    raise TypeError("Docker returned invalid image import progress")
                if "error" in progress or "errorDetail" in progress:
                    detail = progress.get("errorDetail")
                    message = (
                        detail.get("message")
                        if isinstance(detail, dict)
                        else progress.get("error")
                    )
                    raise ImageLoadError(
                        message
                        if isinstance(message, str)
                        else "Docker reported an image import failure"
                    )
    imported = target.images.get(source_id)
    if imported.id != source_id or not imported.tag(repository, tag=tag):
        raise RuntimeError("The imported image ID or tag does not match its source")
    if target.images.get(f"{repository}:{tag}").id != source_id:
        raise RuntimeError("The imported runtime tag resolves to a different image")


def _parse_dns_servers(content: bytes) -> tuple[str, ...]:
    if len(content) > _DNS_CONFIG_MAX_BYTES:
        raise ValueError("Docker resolver configuration exceeds its byte limit")
    addresses: list[str] = []
    for line in content.decode("utf-8").splitlines():
        fields = line.split("#", 1)[0].split(";", 1)[0].split()
        if not fields or fields[0] != "nameserver":
            continue
        if len(fields) != 2:
            raise ValueError("A nameserver directive must contain one numeric address")
        address = ipaddress.ip_address(fields[1])
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id is not None or address.is_link_local:
                raise ValueError(
                    "Interface-dependent DNS cannot cross container namespaces"
                )
            effective = address.ipv4_mapped or address
        else:
            effective = address
        if (
            effective.is_unspecified
            or effective.is_multicast
            or effective == ipaddress.IPv4Address("255.255.255.255")
        ):
            raise ValueError("A DNS server must have a unicast destination address")
        if not effective.is_loopback:
            addresses.append(str(address))
    if not addresses:
        raise ValueError("Docker supplies no non-loopback numeric DNS server")
    return tuple(addresses)


@pytest.fixture(scope="session")
def docker_platform_dns_servers(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> tuple[str, ...]:
    """Read Docker's bridge DNS once without querying any resolver.

    The owned probe bounds its output before the SDK buffers it. Its default
    bridge membership is temporary; the shared network is never modified or
    removed. Interface-dependent addresses cannot be passed to a nested daemon.

    Returns:
        Transferable numeric resolver addresses in Docker's configured order.

    Raises:
        RuntimeError: The finite probe fails to read its resolver configuration.
        ValueError: The file is oversized, malformed, or has no usable address.
    """
    probe = (
        DockerContainer(_DIND_IMAGE)
        .with_command(["-c", str(_DNS_CONFIG_MAX_BYTES + 1), "/etc/resolv.conf"])
        .with_kwargs(
            entrypoint=["head"],
            network="bridge",
            labels={_TEST_LABEL: docker_test_run_id},
            log_config=LogConfig(type="json-file"),
        )
    )
    with _running_container(probe):
        native = probe.get_wrapped_container()
        result = native.wait(timeout=docker_test_client.api.timeout)
        if result["StatusCode"] != 0:
            raise RuntimeError("The Docker platform DNS probe exited unsuccessfully")
        return _parse_dns_servers(native.logs(stdout=True, stderr=False))


@pytest.fixture(scope="session")
def opensandbox_docker_runtime(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
    docker_platform_dns_servers: tuple[str, ...],
) -> Iterator[OpenSandboxDockerRuntime]:
    """Own a separate daemon so test Servers cannot restore or delete host Sandboxes.

    The nested daemon has no host Docker socket or host filesystem mounts. Its
    privileged container is needed for nested namespaces; its API is exposed only
    through random loopback ports and a test-owned Docker network. The Server shares
    the daemon's network namespace because its proxy resolves container-internal
    addresses. Nested Sandbox ports need no host publication.
    """
    from tinkerfin_sandbox import OpenSandboxConfig

    network_name = f"tinkerfin-sandbox-test-{docker_test_run_id}"
    with ExitStack() as resources:
        network = docker_test_client.networks.create(
            network_name, labels={_TEST_LABEL: docker_test_run_id}
        )
        resources.callback(_remove_owned_network, network)
        # Register network ownership before constructing a DockerContainer: its
        # constructor opens a separate SDK client and can fail before start().
        daemon = (
            _with_loopback_port(
                _with_loopback_port(DockerContainer(_DIND_IMAGE), 2375), 8090
            )
            .with_env("DOCKER_TLS_CERTDIR", "")
            .with_command(
                ["--tls=false", "--storage-driver=overlay2"]
                + [f"--dns={address}" for address in docker_platform_dns_servers]
            )
            .with_kwargs(
                privileged=True,
                network=network_name,
                labels={_TEST_LABEL: docker_test_run_id},
            )
            .waiting_for(ExecWaitStrategy(["docker", "info"]).with_startup_timeout(180))
        )
        resources.enter_context(_running_container(daemon))
        endpoint = f"tcp://127.0.0.1:{daemon.get_exposed_port(2375)}"
        # Negotiate no connection while constructing this client: the pinned
        # Docker 28 daemon supports API 1.51, and only this client bypasses proxies.
        runtime_client = DockerClient(base_url=endpoint, version="1.51", timeout=300)
        resources.callback(runtime_client.close)
        runtime_client.api.trust_env = False
        if runtime_client.info()["ID"] == docker_test_client.info()["ID"]:
            raise RuntimeError("The test runtime must use a separate Docker daemon")
        if runtime_client.containers.list(all=True):
            raise RuntimeError(
                "The test runtime must start with an empty Docker inventory"
            )
        for reference, repository, tag in (
            (
                os.environ.get(
                    "TINKERFIN_RUNTIME_TEST_IMAGE", OpenSandboxConfig().image
                ),
                "tinkerfin-sandbox-e2e",
                docker_test_run_id,
            ),
            (
                os.environ.get("TINKERFIN_EXECD_TEST_IMAGE", _OPENSANDBOX_EXECD_IMAGE),
                "tinkerfin-execd-e2e",
                "isolated",
            ),
        ):
            _copy_runtime_image(
                docker_test_client,
                runtime_client,
                reference=reference,
                repository=repository,
                tag=tag,
                source_archive=(
                    Path(os.environ["TINKERFIN_RUNTIME_TEST_ARCHIVE"])
                    if repository == "tinkerfin-sandbox-e2e"
                    and "TINKERFIN_RUNTIME_TEST_ARCHIVE" in os.environ
                    else None
                ),
            )
        container_id = daemon.get_wrapped_container().id
        if not isinstance(container_id, str):
            raise TypeError("The test daemon has no container ID")
        yield OpenSandboxDockerRuntime(
            client=runtime_client,
            container_id=container_id,
            domain=f"127.0.0.1:{daemon.get_exposed_port(8090)}",
            image=f"tinkerfin-sandbox-e2e:{docker_test_run_id}",
            run_id=docker_test_run_id,
        )


@pytest.fixture
def opensandbox_test_service(
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    docker_test_run_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[OpenSandboxTestService]:
    """Start a Server with access only to a test-owned Docker daemon."""
    api_key = secrets.token_urlsafe(32)
    config_path: Path = tmp_path_factory.mktemp("opensandbox") / "config.toml"
    config_path.write_text(
        _opensandbox_config(docker_host="127.0.0.1"),
        encoding="utf-8",
    )
    metadata_dir = tmp_path_factory.mktemp("opensandbox-metadata")
    container = (
        DockerContainer(
            os.environ.get("TINKERFIN_SERVER_TEST_IMAGE", _OPENSANDBOX_SERVER_IMAGE)
        )
        .with_env("OPENSANDBOX_SERVER_API_KEY", api_key)
        .with_volume_mapping(
            str(metadata_dir),
            "/root/.opensandbox/metadata",
            "rw",
        )
        .with_copy_into_container(config_path, "/etc/opensandbox/config.toml")
    )
    with _running_container(opensandbox_docker_runtime.configure_server(container)):
        yield OpenSandboxTestService(
            domain=opensandbox_docker_runtime.domain,
            api_key=api_key,
            run_id=docker_test_run_id,
        )


@pytest_asyncio.fixture
async def mysql_sandbox_url(
    mysql_test_service: MySQLTestService,
) -> AsyncIterator[str]:
    """Create one database owned only by a Sandbox integration test."""

    database_name = f"tinkerfin_sandbox_{uuid4().hex}"
    if _MYSQL_SANDBOX_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("generated Sandbox database name is outside the owned scope")
    admin_engine = create_async_engine(mysql_test_service.url("tinkerfin_test_admin"))
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
        created = True
        yield mysql_test_service.url(database_name)
    finally:
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f"DROP DATABASE IF EXISTS `{database_name}`")
                )
        await admin_engine.dispose()


__all__ = [
    "MySQLTestService",
    "OpenSandboxTestService",
    "PostgreSQLTestService",
    "RedisTestService",
]
