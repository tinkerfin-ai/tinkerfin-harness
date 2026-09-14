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
from sqlalchemy.engine import make_url

APP_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = APP_ROOT / "deploy"
PASSWORDS = (
    "mysql_root_password",
    "mysql_password",
    "redis_runtime_password",
    "opensandbox_api_key",
)


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "workspace with spaces"
    deploy = root / "apps/studio/server/deploy"
    shutil.copytree(
        DEPLOY_DIR, deploy, ignore=shutil.ignore_patterns(".env", "secrets")
    )
    schema = deploy.parent / "database/mysql/schema.sql"
    schema.parent.mkdir(parents=True)
    shutil.copyfile(APP_ROOT / "database/mysql/schema.sql", schema)
    environment = os.environ.copy()
    for line in (deploy / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            environment.pop(line.split("=", 1)[0], None)
    for key in ("SECRETS_DIR", "STUDIO_ENV_FILE", "COMPOSE_PROJECT_NAME"):
        environment.pop(key, None)
    return root, deploy, environment


def setup(deploy, environment):
    return subprocess.run(
        ["bash", str(deploy / "setup.sh")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def configure(deploy, **values):
    path = deploy / ".env"
    content = path.read_text()
    for key, value in values.items():
        content, count = re.subn(
            rf"(?m)^{key}=.*$", lambda _: f"{key}={value}", content
        )
        assert count == 1
    path.write_text(content)


def compose_config(deploy, environment, *, base=False):
    command = [
        "docker",
        "compose",
        "--env-file",
        str(deploy / ".env"),
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


def test_setup_preserves_credentials_and_restricts_host_directory(deployment):
    _, deploy, environment = deployment
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    credentials = {name: (deploy / "secrets" / name).read_bytes() for name in PASSWORDS}
    assert len(set(credentials.values())) == len(PASSWORDS)
    for value in credentials.values():
        assert re.fullmatch(rb"[a-f0-9]{64}\n", value)
        assert value.decode().strip() not in result.stdout + result.stderr
    assert stat.S_IMODE((deploy / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((deploy / ".env").stat().st_mode) == 0o600
    for path in (deploy / "secrets").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    configure(deploy, STUDIO_PORT="19090")
    assert setup(deploy, environment).returncode == 0
    assert "STUDIO_PORT=19090" in (deploy / ".env").read_text()
    assert all(
        (deploy / "secrets" / name).read_bytes() == value
        for name, value in credentials.items()
    )


@pytest.mark.parametrize("missing_directory", [False, True])
def test_setup_does_not_replace_missing_existing_credentials(
    deployment, missing_directory
):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    secrets = deploy / "secrets"
    retained = (secrets / "mysql_password").read_bytes()
    if missing_directory:
        shutil.rmtree(secrets)
    else:
        (secrets / "redis_runtime_password").unlink()
    result = setup(deploy, environment)
    assert result.returncode != 0
    if missing_directory:
        assert not secrets.exists()
    else:
        assert not (secrets / "redis_runtime_password").exists()
        assert (secrets / "mysql_password").read_bytes() == retained


def test_mysql_configuration_and_password_changes_update_connection_secret(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    configure(
        deploy,
        MYSQL_HOST="::1",
        MYSQL_PORT="3307",
        MYSQL_DATABASE="example-db",
        MYSQL_USER='"user name"',
        MYSQL_PUBLISHED_PORT="23306",
    )
    password = "special:@/#% 中文"
    (deploy / "secrets/mysql_password").write_text(password + "\n")
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    url = make_url((deploy / "secrets/database_url").read_text().strip())
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
    assert mysql["environment"]["MYSQL_DATABASE"] == "example-db"
    assert password not in result.stdout + result.stderr


def test_setup_does_not_execute_environment_values(deployment):
    root, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    marker = root / "executed"
    configure(deploy, MYSQL_USER=f"$(touch '{marker}')")
    result = setup(deploy, environment)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (
        make_url((deploy / "secrets/database_url").read_text().strip()).username
        == f"$(touch '{marker}')"
    )


def test_compose_groups_backend_and_supports_base_and_external_services(deployment):
    _, deploy, environment = deployment
    assert setup(deploy, environment).returncode == 0
    full = compose_config(deploy, environment)
    dependencies = {"mysql", "redis-runtime", "opensandbox"}
    assert full["name"] == "tinkerfin-studio"
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
    assert server["environment"]["OPEN_SANDBOX_WARM_POOL_SIZE"] == "0"
    assert server["logging"] == {
        "driver": "local",
        "options": {"max-size": "50m", "max-file": "3"},
    }
    assert all(volume["target"] != "/app/logs" for volume in server["volumes"])
    assert (
        full["services"]["mysql"]["environment"]["MYSQL_PASSWORD_FILE"]
        == "/run/secrets/mysql_password"
    )
    assert any(
        volume["target"] == "/docker-entrypoint-initdb.d/10-studio-business.sql"
        for volume in full["services"]["mysql"]["volumes"]
    )
    assert (
        full["volumes"]["studio-attachments"]["name"]
        == "tinkerfin-studio_studio-attachments"
    )
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
    script = str(deploy / "deploy.sh")
    if location == "cdpath":
        environment["CDPATH"] = str(root)
        script = "apps/studio/server/deploy/deploy.sh"
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
    assert all(
        flag in up
        for flag in ("--force-recreate", "--remove-orphans", "--no-build", "--wait")
    )
    assert up[up.index("--pull") + 1] == "never"
    assert (deploy / ".env").exists()
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
        ["bash", str(deploy / "deploy.sh"), "--build"],
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
    expected = (deploy / "secrets/mysql_password").read_bytes()
    environment, calls = fake_docker(tmp_path, environment)
    environment["FAIL_COMMAND"] = failure
    args = ["bash", str(deploy / "deploy.sh")]
    if failure == "build":
        args.append("--build")
    result = subprocess.run(
        args, env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 42
    assert "已就绪" not in result.stdout
    assert (deploy / "secrets/mysql_password").read_bytes() == expected
    operations = read_calls(calls)
    if failure == "up":
        assert "isolated service diagnostics" in result.stdout
    else:
        assert not any("up" in item["args"] for item in operations)


def test_external_deploy_uses_explicit_environment_file_outside_checkout(
    deployment, tmp_path
):
    _, deploy, environment = deployment
    config = tmp_path / "custom config/.env"
    result = subprocess.run(
        ["bash", str(deploy / "setup.sh"), str(config)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    environment, calls = fake_docker(tmp_path, environment)
    result = subprocess.run(
        [
            "bash",
            str(deploy / "deploy.sh"),
            "--external",
            "--env-file",
            "custom config/.env",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (deploy / ".env").exists()
    assert all(
        str(config) in item["args"]
        for item in read_calls(calls)
        if item["args"][0] == "compose" and "version" not in item["args"]
    )


def test_first_setup_can_retry_after_invalid_configuration(deployment):
    _, deploy, environment = deployment
    result = setup(deploy, {**environment, "MYSQL_PORT": "0"})
    assert result.returncode != 0
    assert not (deploy / ".env").exists()
    assert not (deploy / "secrets").exists()
    assert setup(deploy, environment).returncode == 0
