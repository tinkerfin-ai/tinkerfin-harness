"""部署配置、初始化及命令行可观察行为"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values, set_key
from sqlalchemy.engine import make_url

from tinkerfin_studio.config.settings import load_settings

APP_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = APP_ROOT / "deploy"
PASSWORDS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_BUSINESS_PASSWORD",
    "MYSQL_COMPONENTS_PASSWORD",
    "REDIS_RUNTIME_PASSWORD",
    "OPEN_SANDBOX_API_KEY",
    "S3_STORAGE_ACCESS_KEY",
    "S3_STORAGE_SECRET_KEY",
)


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "workspace with spaces"
    deploy = root / "apps/studio/server/deploy"
    shutil.copytree(
        DEPLOY_DIR, deploy, ignore=shutil.ignore_patterns(".env", "secrets")
    )
    shutil.copyfile(
        APP_ROOT.parent / ".env.example", deploy.parents[1] / ".env.example"
    )
    schema = deploy.parent / "database/mysql/schema.sql"
    schema.parent.mkdir(parents=True)
    shutil.copyfile(APP_ROOT / "database/mysql/schema.sql", schema)
    environment = os.environ.copy()
    for line in (deploy.parents[1] / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            environment.pop(line.split("=", 1)[0], None)
    for key in ("STUDIO_ENV_FILE", "COMPOSE_PROJECT_NAME"):
        environment.pop(key, None)
    environment["S3_STORAGE_BUCKET"] = "deployment-test"
    return root, deploy, environment


def setup(deploy, environment):
    return subprocess.run(
        ["bash", str(deploy / "init-env.sh")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def configure(deploy, **values):
    path = deploy.parents[1] / ".env"
    for key, value in values.items():
        set_key(path, key, value, quote_mode="always")


def compose_config(deploy, environment, *, base=False):
    command = [
        "docker",
        "compose",
        "--env-file",
        str(deploy.parents[1] / ".env"),
        "-f",
        str(deploy / ("docker-compose-base.yaml" if base else "docker-compose.yaml")),
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=20, check=True
    )
    return json.loads(result.stdout)


def fake_docker(tmp_path, environment):
    """只替代会操作服务的 Docker 命令，配置解析仍使用真实 Compose"""
    executable = shutil.which("docker")
    assert executable
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls.jsonl"
    docker = commands / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as stream:
    stream.write(json.dumps({'args':args, 'image':os.getenv('STUDIO_IMAGE')}) + '\\n')
if args[0] == 'info':
    sys.exit(0)
if args[:2] == ['container', 'inspect']:
    if os.getenv('CONFLICT_OWNER'):
        print(os.environ['CONFLICT_OWNER'])
        sys.exit(0)
    sys.exit(1)
if args[0] != 'compose':
    sys.exit(70)
if 'config' in args or 'version' in args:
    sys.exit(subprocess.call([os.environ['REAL_DOCKER'], *args]))
if os.getenv('FAIL_COMMAND') in args:
    print('isolated docker failure', file=sys.stderr)
    sys.exit(42)
if 'logs' in args:
    print('isolated service diagnostics')
"""
    )
    docker.chmod(0o700)
    return {
        **environment,
        "PATH": str(commands) + os.pathsep + environment["PATH"],
        "REAL_DOCKER": executable,
        "CALLS": str(calls),
    }, calls


def read_calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_setup_preserves_credentials_and_restricts_configuration(deployment):
    _, deploy, environment = deployment
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    config = deploy.parents[1] / ".env"
    values = dotenv_values(config)
    credentials = {key: values[key] for key in PASSWORDS}
    assert len(set(credentials.values())) == len(PASSWORDS)
    for value in credentials.values():
        assert value is not None and re.fullmatch(r"[a-f0-9]{64}", value)
        assert value not in result.stdout + result.stderr
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not (deploy / "secrets").exists()
    assert not (deploy / ".env").exists()
    assert not (deploy.parent / ".env").exists()
    configure(
        deploy,
        STUDIO_PORT="19090",
        MYSQL_ROOT_PASSWORD="operator-root-password",
        REDIS_RUNTIME_PASSWORD="operator-redis-password",
    )
    previous = config.read_bytes()
    assert setup(deploy, environment).returncode == 0
    assert config.read_bytes() == previous


def test_setup_does_not_replace_missing_existing_credentials(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    configure(deploy, REDIS_RUNTIME_PASSWORD="")
    config = deploy.parents[1] / ".env"
    retained = config.read_bytes()
    result = setup(deploy, environment)
    assert result.returncode != 0
    assert config.read_bytes() == retained


def test_mysql_configuration_builds_connections_without_manual_url_escaping(
    deployment, monkeypatch
):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    password = "special:@/#% 中文 ${STUDIO_PASSWORD_LITERAL}"
    monkeypatch.setenv("STUDIO_PASSWORD_LITERAL", "must-not-expand")
    environment["STUDIO_PASSWORD_LITERAL"] = "must-not-expand"
    configure(
        deploy,
        MYSQL_HOST="::1",
        MYSQL_PORT="3307",
        MYSQL_BUSINESS_DATABASE="example-db",
        MYSQL_BUSINESS_USER="user name",
        MYSQL_PUBLISHED_PORT="23306",
        MYSQL_BUSINESS_PASSWORD=password,
    )
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    settings = load_settings(env_file=deploy.parents[1] / ".env")
    url = make_url(settings.business_database_url)
    assert (url.host, url.port, url.database, url.username, url.password) == (
        "::1",
        3307,
        "example-db",
        "user name",
        password,
    )
    mysql = compose_config(deploy, environment)["services"]["mysql"]
    assert mysql["ports"][0]["published"] == "23306"
    assert mysql["ports"][0]["target"] == 3306
    assert mysql["environment"]["MYSQL_BUSINESS_DATABASE"] == "example-db"
    assert (
        mysql["environment"]["MYSQL_BUSINESS_PASSWORD"].replace("$$", "$") == password
    )
    assert password not in result.stdout + result.stderr


def test_setup_does_not_execute_environment_values(deployment):
    root, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    marker = root / "executed"
    username = f"$(touch '{marker}')"
    configure(deploy, MYSQL_BUSINESS_USER=username)
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    settings = load_settings(env_file=deploy.parents[1] / ".env")
    assert make_url(settings.business_database_url).username == username


def test_environment_credentials_match_bundled_services_and_backend(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    for key in PASSWORDS:
        environment[key] = "operator-${literal}-" + key.lower()
    assert setup(deploy, environment).returncode == 0
    services = compose_config(deploy, environment)["services"]
    backend = services["server"]["environment"]
    for service, keys in (
        ("mysql", ("MYSQL_BUSINESS_PASSWORD", "MYSQL_COMPONENTS_PASSWORD")),
        ("redis-runtime", ("REDIS_RUNTIME_PASSWORD",)),
    ):
        for key in keys:
            assert services[service]["environment"][key] == backend[key]
            assert backend[key].replace("$$", "$") == environment[key]
    for service, component_key, app_key in (
        ("minio", "MINIO_ROOT_USER", "S3_STORAGE_ACCESS_KEY"),
        ("minio", "MINIO_ROOT_PASSWORD", "S3_STORAGE_SECRET_KEY"),
        ("opensandbox", "OPENSANDBOX_SERVER_API_KEY", "OPEN_SANDBOX_API_KEY"),
    ):
        assert services[service]["environment"][component_key] == backend[app_key]
        assert backend[app_key].replace("$$", "$") == environment[app_key]
    assert backend["MYSQL_ROOT_PASSWORD"] == ""


def test_compose_groups_backend_and_supports_base_and_external_services(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    full = compose_config(deploy, environment)
    dependencies = {"mysql", "redis-runtime", "opensandbox", "minio"}
    assert full["name"] == "tinkerfin"
    assert set(full["services"]) == dependencies | {"server"}
    assert (
        set(compose_config(deploy, environment, base=True)["services"]) == dependencies
    )
    external = compose_config(deploy, {**environment, "COMPOSE_PROFILES": ""})
    assert set(external["services"]) == {"server"}
    partial = compose_config(
        deploy,
        {**environment, "COMPOSE_PROFILES": "redis-runtime,opensandbox"},
    )
    assert set(partial["services"]) == {
        "server",
        "redis-runtime",
        "opensandbox",
    }
    control = full["services"]["opensandbox"]
    assert float(control["cpus"]) == 0.5
    assert int(control["mem_limit"]) == 512 * 1024 * 1024
    server = full["services"]["server"]
    assert server["environment"]["MYSQL_ROOT_PASSWORD"] == ""
    assert "secrets" not in full
    assert server["logging"] == {
        "driver": "local",
        "options": {"max-size": "50m", "max-file": "3"},
    }
    assert all(volume["target"] != "/app/logs" for volume in server.get("volumes", []))
    assert full["services"]["mysql"]["environment"]["MYSQL_BUSINESS_PASSWORD"]
    assert any(
        volume["target"] == "/opt/studio/schema.sql"
        for volume in full["services"]["mysql"]["volumes"]
    )
    assert full["volumes"]["minio-data"]["name"] == "tinkerfin_minio-data"
    assert any(
        volume["target"] == "/root/.opensandbox/metadata"
        for volume in full["services"]["opensandbox"]["volumes"]
    )


@pytest.mark.parametrize("location", ["root", "deploy", "outside", "cdpath"])
def test_default_deploy_bootstraps_and_pulls_from_any_working_directory(
    deployment, tmp_path, location
):
    root, deploy, environment = deployment
    environment, calls = fake_docker(tmp_path, environment)
    outside = tmp_path / "caller directory"
    outside.mkdir()
    cwd = {"root": root, "deploy": deploy, "outside": outside, "cdpath": root}[location]
    script = str(deploy / "start.sh")
    if location == "cdpath":
        environment["CDPATH"] = str(root)
        script = "apps/studio/server/deploy/start.sh"
    result = subprocess.run(
        ["bash", script],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    operations = read_calls(calls)
    assert any("pull" in item["args"] for item in operations)
    assert not any("build" in item["args"] for item in operations)
    up = next(item["args"] for item in operations if "up" in item["args"])
    assert all(flag in up for flag in ("--no-build", "--wait"))
    assert up[up.index("--pull") + 1] == "never"
    assert (deploy.parents[1] / ".env").exists()
    assert not (cwd / "secrets").exists() if cwd != deploy else True
    assert "http://127.0.0.1:8090/api" in result.stdout


@pytest.mark.parametrize(
    "custom_image",
    [None, "registry.example/custom:dev", "ghcr.io/tinkerfin-ai/studio-server:custom"],
)
def test_source_build_uses_local_or_custom_image_without_pulling_server(
    deployment, tmp_path, custom_image
):
    root, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    if custom_image:
        configure(deploy, STUDIO_IMAGE=custom_image)
    artifact = root / "dist/caller-owned.whl"
    artifact.parent.mkdir()
    artifact.write_text("retained")
    environment, calls = fake_docker(tmp_path, environment)
    result = subprocess.run(
        ["bash", str(deploy / "start.sh"), "--container"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    operations = read_calls(calls)
    build = next(item for item in operations if "build" in item["args"])
    assert build["image"] == (custom_image or "tinkerfin-studio-server:local")
    assert build["args"][-1] == "server"
    for item in operations:
        if "pull" in item["args"]:
            assert "server" not in item["args"]
            assert "mysql" in item["args"]
    assert artifact.read_text() == "retained"


@pytest.mark.parametrize("failure", ["pull", "build", "up"])
def test_failed_deployment_reports_failure_and_keeps_credentials(
    deployment, tmp_path, failure
):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    expected = (deploy.parents[1] / ".env").read_bytes()
    environment, calls = fake_docker(tmp_path, environment)
    environment["FAIL_COMMAND"] = failure
    args = ["bash", str(deploy / "start.sh")]
    if failure == "build":
        args.append("--container")
    result = subprocess.run(
        args, env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 42
    assert "已就绪" not in result.stdout
    assert (deploy.parents[1] / ".env").read_bytes() == expected
    operations = read_calls(calls)
    if failure == "up":
        assert "isolated service diagnostics" in result.stdout
    else:
        assert not any("up" in item["args"] for item in operations)


@pytest.mark.parametrize("mode", [None, "--container", "--local"])
def test_external_services_use_explicit_configuration_without_bundled_credentials(
    deployment, tmp_path, mode
):
    _, deploy, environment = deployment
    config = tmp_path / "custom config/.env"
    config.parent.mkdir()
    config.write_text(
        "COMPOSE_PROFILES=\n"
        "BUSINESS_DATABASE_URL=mysql+asyncmy://business:password@db/business\n"
        "COMPONENTS_DATABASE_URL=mysql+asyncmy://components:password@db/components\n"
        "REDIS_RUNTIME_HOST=redis.example.com\n"
        "OPEN_SANDBOX_DOMAIN=sandbox.example.com:8090\n"
        "OPEN_SANDBOX_API_KEY=external-sandbox-key\n"
        "S3_STORAGE_ENDPOINT=https://storage.example.com\n"
        "S3_STORAGE_PUBLIC_ENDPOINT=https://storage.example.com\n"
        "S3_STORAGE_BUCKET=external-bucket\n"
        "S3_STORAGE_ACCESS_KEY=external-account\n"
        "S3_STORAGE_SECRET_KEY=external-secret\n"
    )
    expected = config.read_bytes()
    environment, calls = fake_docker(tmp_path, environment)
    command = ["bash", str(deploy / "start.sh")]
    if mode:
        command.append(mode)
    result = subprocess.run(
        [*command, "--env-file", "custom config/.env"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == expected
    assert not (deploy.parents[1] / ".env").exists()
    operations = read_calls(calls)
    assert all(
        str(config) in item["args"]
        for item in operations
        if item["args"][0] == "compose" and "version" not in item["args"]
    )
    assert not any(item["args"][:2] == ["container", "inspect"] for item in operations)
    assert any("build" in item["args"] for item in operations) == (
        mode == "--container"
    )
    assert any("up" in item["args"] for item in operations) == (mode != "--local")
    assert any("pull" in item["args"] for item in operations) == (mode is None)


def test_local_uses_existing_minio_and_starts_only_selected_services(
    deployment, tmp_path, monkeypatch
):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    configure(
        deploy,
        COMPOSE_PROFILES="mysql,redis-runtime,opensandbox",
        S3_STORAGE_ENDPOINT="https://storage.example.com",
        S3_STORAGE_PUBLIC_ENDPOINT="https://files.example.com",
        S3_STORAGE_ACCESS_KEY="existing-access-key",
        S3_STORAGE_SECRET_KEY="existing-secret",
    )
    environment, calls = fake_docker(tmp_path, environment)
    result = subprocess.run(
        ["bash", str(deploy / "start.sh"), "--local"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    base = compose_config(deploy, environment, base=True)
    assert set(base["services"]) == {"mysql", "redis-runtime", "opensandbox"}
    container = compose_config(deploy, environment)["services"]["server"]["environment"]
    for key in ("S3_STORAGE_ACCESS_KEY", "S3_STORAGE_SECRET_KEY", "S3_STORAGE_BUCKET"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(env_file=deploy.parents[1] / ".env")
    assert settings.s3_storage.endpoint == container["S3_STORAGE_ENDPOINT"]
    assert container["S3_STORAGE_ENDPOINT"] == "https://storage.example.com"
    assert settings.s3_storage.public_endpoint == "https://files.example.com"
    assert (
        settings.s3_storage.secret_key.get_secret_value()
        == container["S3_STORAGE_SECRET_KEY"]
    )
    assert not any(
        item["args"][:2] == ["container", "inspect"] and item["args"][-1] == "minio"
        for item in read_calls(calls)
    )


def test_setup_preserves_generated_credentials_when_validation_fails(deployment):
    _, deploy, environment = deployment
    result = setup(deploy, {**environment, "MYSQL_PUBLISHED_PORT": "0"})
    assert result.returncode != 0
    config = deploy.parents[1] / ".env"
    retained = config.read_bytes()
    assert setup(deploy, environment).returncode == 0
    assert config.read_bytes() == retained


@pytest.mark.parametrize("bucket", ["", "A-Bucket", "a..b", "127.0.0.1"])
def test_deploy_rejects_missing_or_invalid_bucket_before_starting_services(
    deployment, tmp_path, bucket
):
    _, deploy, environment = deployment
    environment, calls = fake_docker(tmp_path, environment)
    environment["S3_STORAGE_BUCKET"] = bucket
    result = subprocess.run(
        ["bash", str(deploy / "start.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "S3_STORAGE_BUCKET" in result.stderr
    assert not any(
        "up" in item["args"] or "pull" in item["args"] for item in read_calls(calls)
    )


@pytest.mark.parametrize("arguments", [("--local", "--container"), ("--unsupported",)])
def test_invalid_arguments_fail_before_changing_configuration(deployment, arguments):
    _, deploy, environment = deployment
    result = subprocess.run(
        ["bash", str(deploy / "start.sh"), *arguments],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (deploy.parents[1] / ".env").exists()


def test_local_initializes_host_configuration_and_reuses_it(
    deployment, tmp_path, monkeypatch
):
    _, deploy, environment = deployment
    environment.pop("S3_STORAGE_BUCKET", None)
    environment, calls = fake_docker(tmp_path, environment)
    command = ["bash", str(deploy / "start.sh"), "--local"]
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    config = deploy.parents[1] / ".env"
    # 解析真实产物，验证本机地址、密钥和双库，不执行本机服务
    settings = load_settings(env_file=config)
    for database, expected in (
        (settings.business_database, "tinkerfin"),
        (settings.components_database, "tinkerfin_components"),
    ):
        url = make_url(database.url)
        assert (url.host, url.port, url.database) == ("127.0.0.1", 13306, expected)
    assert settings.redis_runtime.host == "127.0.0.1"
    assert settings.sandbox.domain == "127.0.0.1:8091"
    assert dotenv_values(config)["S3_STORAGE_BUCKET"] == "tinkerfin"
    # 同一配置交给容器时使用服务名和内部端口，账号和密码保持一致
    container_environment = compose_config(deploy, environment)["services"]["server"][
        "environment"
    ]
    with monkeypatch.context() as container_context:
        for key, value in container_environment.items():
            container_context.setenv(key, value)
        container = load_settings(env_file=None)
    for host_database, container_database in (
        (settings.business_database_url, container.business_database_url),
        (settings.components_database_url, container.components_database_url),
    ):
        host_url, container_url = make_url(host_database), make_url(container_database)
        assert host_url.set(host="mysql", port=3306) == container_url
    assert container.redis_runtime.host == "redis-runtime"
    assert container.redis_runtime.port == 6379
    assert container.sandbox.domain == "opensandbox:8090"
    assert container.s3_storage.endpoint == "http://minio:9000"
    configure(deploy, LOG_FILE_ENABLED="true")
    previous = config.read_bytes()
    assert subprocess.run(command, env=environment, capture_output=True).returncode == 0
    assert config.read_bytes() == previous
    for operation in read_calls(calls):
        args = operation["args"]
        if "up" in args or "pull" in args:
            assert "server" not in args
            assert str(deploy / "docker-compose-base.yaml") in args
            assert "--force-recreate" not in args
            assert "--remove-orphans" not in args
    assert settings.redis_runtime.password is not None
    assert (
        settings.redis_runtime.password.get_secret_value()
        not in result.stdout + result.stderr
    )


def test_local_rejects_borrowed_container_without_starting_services(
    deployment, tmp_path
):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    config = deploy.parents[1] / ".env"
    previous = config.read_bytes()
    environment, calls = fake_docker(tmp_path, environment)
    environment["CONFLICT_OWNER"] = "unrelated/mysql"
    result = subprocess.run(
        ["bash", str(deploy / "start.sh"), "--local"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert config.read_bytes() == previous
    assert not any(
        "up" in item["args"] or "pull" in item["args"] for item in read_calls(calls)
    )


def test_compose_exposes_fixed_names_separate_accounts_and_sandbox_host(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    config = compose_config(
        deploy, {**environment, "OPEN_SANDBOX_PUBLIC_HOST": "127.0.0.1"}
    )
    assert {
        name: config["services"][name]["container_name"]
        for name in ("mysql", "redis-runtime", "minio", "opensandbox")
    } == {
        "mysql": "mysql8",
        "redis-runtime": "redis-runtime",
        "minio": "minio",
        "opensandbox": "opensandbox",
    }
    mysql = config["services"]["mysql"]["environment"]
    assert mysql["MYSQL_BUSINESS_DATABASE"] != mysql["MYSQL_COMPONENTS_DATABASE"]
    assert mysql["MYSQL_BUSINESS_USER"] != mysql["MYSQL_COMPONENTS_USER"]
    assert mysql["MYSQL_BUSINESS_PASSWORD"] != mysql["MYSQL_COMPONENTS_PASSWORD"]
    assert 'host_ip = "127.0.0.1"' in config["configs"]["opensandbox-config"]["content"]


@pytest.fixture
def bundled_mysql(deployment, docker_test_client, docker_test_run_id, tmp_path):
    """用独占项目运行实际初始化脚本，所有容器、网络和卷均按项目清理"""
    from uuid import uuid4

    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    # 同时验证 SQL 密码引号和连接地址转义，不使用真实运行凭据
    configure(deploy, MYSQL_COMPONENTS_PASSWORD="quote'\\slash:@/#%")
    assert setup(deploy, environment).returncode == 0
    config = compose_config(deploy, environment, base=True)
    project = f"tinkerfin-deploy-test-{uuid4().hex}"
    label = {"tinkerfin.test/run": docker_test_run_id}
    mysql = config["services"]["mysql"]
    mysql["container_name"] = f"{project}-mysql"
    mysql["labels"] = label
    mysql["ports"][0]["published"] = "0"
    mysql["restart"] = "no"
    mysql.pop("profiles", None)
    config["name"] = project
    config["services"] = {"mysql": mysql}
    config["volumes"] = {"mysql-data": {"name": f"{project}-data", "labels": label}}
    config["networks"] = {"default": {"name": f"{project}-network", "labels": label}}
    spec = tmp_path / "mysql-compose.json"
    spec.write_text(json.dumps(config))
    command = ["docker", "compose", "-p", project, "-f", str(spec)]
    owned = {"label": f"com.docker.compose.project={project}"}
    assert not docker_test_client.containers.list(all=True, filters=owned)
    try:
        result = subprocess.run(
            [*command, "up", "-d", "--wait", "--wait-timeout", "180", "mysql"],
            capture_output=True,
            text=True,
            timeout=210,
        )
        if result.returncode:
            logs = subprocess.run(
                [*command, "logs", "--no-color", "mysql"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            pytest.fail(result.stderr + logs.stdout + logs.stderr)
        container = docker_test_client.containers.get(mysql["container_name"])
        port = int(
            container.attrs["NetworkSettings"]["Ports"]["3306/tcp"][0]["HostPort"]
        )
        settings = load_settings(env_file=deploy.parents[1] / ".env")
        yield {
            "business": make_url(settings.business_database_url).set(
                host="127.0.0.1", port=port
            ),
            "components": make_url(settings.components_database_url).set(
                host="127.0.0.1", port=port
            ),
        }
    finally:
        subprocess.run(
            [*command, "down", "--volumes"], check=True, capture_output=True, timeout=60
        )
        assert not docker_test_client.containers.list(all=True, filters=owned)
        assert not docker_test_client.networks.list(filters=owned)
        assert not docker_test_client.volumes.list(filters=owned)


@pytest.mark.docker_integration
async def test_bundled_redis_accepts_literal_passwords(
    deployment, docker_test_client, docker_test_run_id
):
    """实际容器配置支持密码特殊字符，测试结束清理本轮独占容器"""
    from redis.asyncio import Redis
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import ExecWaitStrategy
    from tests.support.docker_services import _running_container, _with_loopback_port

    del docker_test_client
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    password = "space '\"\\$literal:@/#%"
    configure(deploy, REDIS_RUNTIME_PASSWORD=password)
    service = compose_config(deploy, environment, base=True)["services"][
        "redis-runtime"
    ]
    container = _with_loopback_port(DockerContainer(service["image"]), 6379)
    for key, value in service["environment"].items():
        container.with_env(key, value.replace("$$", "$"))
    container.with_kwargs(
        entrypoint=service["entrypoint"],
        labels={"tinkerfin.test/run": docker_test_run_id},
    )
    # Docker API 不执行 Compose 的美元符号还原
    container.with_command(
        [command.replace("$$", "$") for command in service["command"]]
    )
    container.waiting_for(
        ExecWaitStrategy(
            ["sh", "-c", service["healthcheck"]["test"][1].replace("$$", "$")]
        ).with_startup_timeout(60)
    )
    with _running_container(container):
        async with Redis(
            host="127.0.0.1",
            port=int(container.get_exposed_port(6379)),
            password=password,
        ) as client:
            assert await client.execute_command("PING") is True


@pytest.mark.docker_integration
async def test_bundled_mysql_initializes_separate_databases_and_restricts_accounts(
    bundled_mysql,
):
    """真实 MySQL 上业务建表与组件建表互不混用，双方账号均不能跨库访问"""
    from contextlib import AsyncExitStack

    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError
    from test_mysql_schema_alignment import _BUSINESS_TABLES

    from tinkerfin_automation import SqlAlchemyAutomationStore
    from tinkerfin_langgraph_store import SqlAlchemyStore
    from tinkerfin_sandbox import SQLAlchemyOpenSandboxState
    from tinkerfin_studio.infrastructure.database import Database
    from tinkerfin_tracing import SqlAlchemyTraceStore

    async with AsyncExitStack() as stack:
        databases = {
            kind: await stack.enter_async_context(
                Database(
                    url.render_as_string(hide_password=False),
                    pool_size=5,
                    max_overflow=5,
                )
            )
            for kind, url in bundled_mysql.items()
        }
        business, components = databases["business"], databases["components"]
        for database in databases.values():
            verified = await database.verify_connection_budget(
                total_pool_capacity=20, configured_budget=20, management_reserve=10
            )
            assert verified.sqlalchemy_pool_capacity == 20
            async with database.engine.connect() as connection:
                assert (
                    await connection.execute(
                        text("SELECT @@character_set_database, @@collation_database")
                    )
                ).one() == ("utf8mb4", "utf8mb4_0900_ai_ci")
        async with business.engine.connect() as connection:
            assert (
                set(
                    await connection.run_sync(
                        lambda conn: inspect(conn).get_table_names()
                    )
                )
                == _BUSINESS_TABLES
            )
            assert (
                await connection.scalar(text("SELECT username FROM users"))
                == "tinkerfin"
            )
        async with components.engine.connect() as connection:
            assert (
                await connection.run_sync(lambda conn: inspect(conn).get_table_names())
                == []
            )
        await stack.enter_async_context(SqlAlchemyStore(components.engine))
        await SqlAlchemyTraceStore(components.engine).setup()
        state = SQLAlchemyOpenSandboxState(engine=components.engine)
        stack.push_async_callback(state.aclose)
        await state.start(warm_pool_size=0)
        automation = SqlAlchemyAutomationStore(components.engine)
        stack.push_async_callback(automation.close)
        await automation.setup()
        async with components.engine.connect() as connection:
            tables = set(
                await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            )
            assert tables
            assert not tables.intersection(_BUSINESS_TABLES)
            assert all(name.startswith("tinkerfin_") for name in tables)
        for kind, database in databases.items():
            other = "components" if kind == "business" else "business"
            async with database.engine.connect() as connection:
                with pytest.raises(DBAPIError):
                    await connection.exec_driver_sql(
                        f"USE `{bundled_mysql[other].database}`"
                    )
        async with components.engine.connect() as connection:
            # 授权中的下划线必须按字面匹配，未创建的相似库也应先被权限拒绝
            with pytest.raises(DBAPIError) as denied:
                await connection.exec_driver_sql("USE `tinkerfinXcomponents`")
            assert denied.value.orig is not None
            assert denied.value.orig.args[0] == 1044
