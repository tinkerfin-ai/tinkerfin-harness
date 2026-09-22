"""在独占 Docker 依赖上验证本机和容器后端的启动、健康及登录"""

import os
from uuid import uuid4

import httpx
import pytest
from test_deployment import (
    bundled_mysql as bundled_mysql,
)
from test_deployment import (
    deployment as deployment,
)
from test_minio_integration import (
    minio_settings as minio_settings,
)
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy, HttpWaitStrategy
from tests.support.docker_services import (
    _DIND_IMAGE,
    _OPENSANDBOX_SERVER_IMAGE,
    OpenSandboxTestService,
    _opensandbox_config,
    _running_container,
    _SandboxServerWait,
    _with_loopback_port,
)

from tinkerfin_studio import resources as resources_module
from tinkerfin_studio.application import create_application
from tinkerfin_studio.config.settings import Settings

pytestmark = pytest.mark.docker_integration


@pytest.fixture
def opensandbox_control(docker_test_client, docker_test_run_id, tmp_path):
    """启动独占控制面；健康和登录验证不需要下载或复制执行镜像"""
    del docker_test_client
    labels = {"tinkerfin.test/run": docker_test_run_id}
    daemon = _with_loopback_port(DockerContainer(_DIND_IMAGE), 8090)
    daemon.with_env("DOCKER_TLS_CERTDIR", "")
    daemon.with_command("--tls=false --storage-driver=overlay2")
    daemon.with_kwargs(privileged=True, labels=labels)
    daemon.waiting_for(ExecWaitStrategy(["docker", "info"]).with_startup_timeout(180))
    with _running_container(daemon):
        domain = f"127.0.0.1:{daemon.get_exposed_port(8090)}"
        config = tmp_path / "opensandbox.toml"
        config.write_text(_opensandbox_config(docker_host="127.0.0.1"))
        server = DockerContainer(_OPENSANDBOX_SERVER_IMAGE)
        server.with_env("DOCKER_HOST", "tcp://127.0.0.1:2375")
        server.with_env("OPENSANDBOX_SERVER_API_KEY", "isolated-test-key")
        server.with_copy_into_container(config, "/etc/opensandbox/config.toml")
        server.with_kwargs(
            network_mode=f"container:{daemon.get_wrapped_container().id}", labels=labels
        )
        server.waiting_for(_SandboxServerWait(domain))
        with _running_container(server):
            yield OpenSandboxTestService(
                domain=domain, api_key="isolated-test-key", run_id=docker_test_run_id
            )


@pytest.fixture
def backend_environment(
    bundled_mysql, redis_test_service, opensandbox_control, minio_settings
):
    return {
        "BUSINESS_DATABASE_URL": bundled_mysql["business"].render_as_string(
            hide_password=False
        ),
        "COMPONENTS_DATABASE_URL": bundled_mysql["components"].render_as_string(
            hide_password=False
        ),
        "REDIS_RUNTIME_HOST": redis_test_service.host,
        "REDIS_RUNTIME_PORT": str(redis_test_service.port),
        "OPEN_SANDBOX_DOMAIN": opensandbox_control.domain,
        "OPEN_SANDBOX_API_KEY": opensandbox_control.api_key,
        "OPEN_SANDBOX_STATE_NAMESPACE": "deployment-" + uuid4().hex,
        "S3_STORAGE_BUCKET": minio_settings.bucket,
        "S3_STORAGE_ENDPOINT": minio_settings.endpoint,
        "S3_STORAGE_PUBLIC_ENDPOINT": minio_settings.public_endpoint,
        "S3_STORAGE_ACCESS_KEY": minio_settings.access_key.get_secret_value(),
        "S3_STORAGE_SECRET_KEY": minio_settings.secret_key.get_secret_value(),
    }


def assert_ready(response):
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "components": dict.fromkeys(
            (
                "business_database",
                "components_database",
                "redis",
                "opensandbox",
                "automation",
                "attachments",
            ),
            True,
        ),
    }


async def test_host_backend_starts_with_separate_databases(
    backend_environment, monkeypatch
):
    settings = Settings.model_validate(
        {key.lower(): value for key, value in backend_environment.items()}
    )
    monkeypatch.setattr(resources_module, "get_settings", lambda: settings)
    application = create_application(lifespan=resources_module.build_lifespan())
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://studio"
        ) as client,
    ):
        assert_ready(await client.get("/health/ready"))
        login = await client.post(
            "/api/auth/login", json={"username": "tinkerfin", "password": "123456"}
        )
        assert login.status_code == 200, login.text
        token = login.json()["data"]["access_token"]
        me = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200
        assert me.json()["data"]["user"]["username"] == "tinkerfin"
    assert not hasattr(application.state, "resources")


@pytest.mark.skipif(
    not os.environ.get("STUDIO_TEST_IMAGE"),
    reason="设置 STUDIO_TEST_IMAGE 为本次构建的后端镜像以验证容器启动",
)
@pytest.mark.parametrize("file_logging", [False, True])
def test_container_backend_starts_with_external_dependencies(
    backend_environment, docker_test_client, docker_test_run_id, tmp_path, file_logging
):
    image = os.environ["STUDIO_TEST_IMAGE"]
    # 测试镜像由调用方持有，Fixture 只清理本次创建的容器
    docker_test_client.images.get(image)
    container = _with_loopback_port(DockerContainer(image), 8090)
    for key, value in backend_environment.items():
        if key in {
            "BUSINESS_DATABASE_URL",
            "COMPONENTS_DATABASE_URL",
            "OPEN_SANDBOX_DOMAIN",
            "S3_STORAGE_ENDPOINT",
            "REDIS_RUNTIME_HOST",
        }:
            value = value.replace("127.0.0.1", "host.docker.internal").replace(
                "localhost", "host.docker.internal"
            )
        container.with_env(key, value)
    logs = tmp_path / "logs"
    if file_logging:
        logs.mkdir()
        # 独占测试目录允许镜像中的非 root 用户写入
        logs.chmod(0o777)
        container.with_volume_mapping(str(logs), "/app/logs", mode="rw")
    container.with_env("LOG_FILE_ENABLED", str(file_logging).lower())
    container.with_env("LOG_FILE_PATH", "/app/logs/studio.log")
    container.with_kwargs(
        labels={"tinkerfin.test/run": docker_test_run_id},
        extra_hosts={"host.docker.internal": "host-gateway"},
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        init=True,
    )
    container.with_tmpfs_mount("/tmp")
    container.waiting_for(
        HttpWaitStrategy(8090, "/health/ready").with_startup_timeout(180)
    )
    with (
        _running_container(container),
        httpx.Client(
            base_url=f"http://127.0.0.1:{container.get_exposed_port(8090)}",
            trust_env=False,
        ) as client,
    ):
        assert_ready(client.get("/health/ready"))
        response = client.post(
            "/api/auth/login",
            json={"username": "tinkerfin", "password": "123456"},
            headers={"Origin": "https://any.example"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["access-control-allow-origin"] == "*"
        # 等待进程正常退出，让已入队日志写完后再检查输出
        container.get_wrapped_container().stop()
        stdout, stderr = container.get_logs()
        assert b"/api/auth/login" in stdout + stderr
    if file_logging:
        assert "/api/auth/login" in (logs / "studio.log").read_text()
    else:
        assert not logs.exists()
