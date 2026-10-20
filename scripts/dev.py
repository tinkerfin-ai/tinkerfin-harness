"""Manage only this worktree's recorded Studio processes and Docker resources."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / ".cache/dev-environment"
LABEL = "io.tinkerfin.dev.owner"


class ProcessRecord(BaseModel):
    """A process identity must match both its creation time and owner argument."""

    model_config = ConfigDict(extra="forbid")
    pid: int = Field(gt=0)
    started: str
    port: int = Field(gt=0, le=65535)


class Environment(BaseModel):
    """Persistent ownership for one worktree; credentials stay in its env file."""

    model_config = ConfigDict(extra="forbid")
    root: str
    owner: str = Field(pattern=r"^[a-f0-9]{32}$")
    env_file: str
    api_port: int = Field(gt=0, le=65535)
    web_port: int = Field(gt=0, le=65535)
    processes: dict[str, ProcessRecord] = Field(default_factory=dict)
    dependencies: bool = False


def command(*arguments: str, check: bool = True) -> str:
    """Run a bounded management command, without printing environment contents."""
    result = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, check=check, timeout=120
    )
    return result.stdout.strip()


def process_identity(pid: int) -> tuple[str, str]:
    """Read OS identity without trusting a stored PID alone."""
    return (
        command("ps", "-p", str(pid), "-o", "lstart=", check=False),
        command("ps", "-p", str(pid), "-o", "command=", check=False),
    )


def owns_process(record: ProcessRecord, owner: str) -> bool:
    started, arguments = process_identity(record.pid)
    return bool(
        started
        and started == record.started
        and f"-m scripts.dev _run --owner {owner} " in arguments
    )


def save(state: Environment) -> None:
    temporary = DIRECTORY / "state.tmp"
    temporary.write_text(state.model_dump_json(indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(DIRECTORY / "state.json")


def load() -> Environment:
    state = Environment.model_validate_json((DIRECTORY / "state.json").read_bytes())
    if state.root != str(ROOT):
        raise ValueError("This state belongs to another worktree; it cannot be adopted")
    return state


def require_free_port(port: int) -> None:
    """Refuse port reuse, including services started outside this command."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ValueError(
                f"Port {port} is occupied; its process will not be stopped"
            ) from error


def resource_ids(state: Environment, kind: str) -> list[str]:
    """Discover exact owner labels, including resources from interrupted startup."""
    return command(
        "docker",
        kind,
        "ls",
        *(("--all",) if kind == "container" else ()),
        "--filter",
        f"label={LABEL}={state.owner}",
        "--format",
        "{{.Name}}" if kind == "volume" else "{{.ID}}",
    ).splitlines()


def checked_resource(state: Environment, kind: str, identifier: str) -> None:
    record = json.loads(command("docker", kind, "inspect", identifier))[0]
    labels = (
        record.get("Config", {}).get("Labels", {})
        if kind == "container"
        else record.get("Labels", {})
    )
    if (labels or {}).get(LABEL) != state.owner:
        raise ValueError(f"Refusing to modify a borrowed {kind}: {identifier}")


def stop(state: Environment, *, destroy: bool = False) -> None:
    """Stop owned process groups; destroy additionally deletes owned Docker data."""
    active = [
        record
        for record in state.processes.values()
        if owns_process(record, state.owner)
    ]
    for record in active:
        try:
            os.killpg(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    while active and time.monotonic() < deadline:
        active = [record for record in active if owns_process(record, state.owner)]
        if active:
            time.sleep(0.1)
    for record in active:
        if owns_process(record, state.owner):
            try:
                os.killpg(record.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while any(owns_process(record, state.owner) for record in active):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Process exit is unconfirmed; ownership records retained"
            )
        time.sleep(0.1)
    state.processes.clear()
    save(state)
    if not state.dependencies:
        return
    for identifier in resource_ids(state, "container"):
        checked_resource(state, "container", identifier)
        command("docker", "container", "stop", "--time", "15", identifier)
        if destroy:
            command("docker", "container", "rm", identifier)
    if destroy:
        for kind in ("network", "volume"):
            for identifier in resource_ids(state, kind):
                checked_resource(state, kind, identifier)
                command("docker", kind, "rm", identifier)
        for kind in ("container", "network", "volume"):
            if resource_ids(state, kind):
                raise RuntimeError(f"Owned {kind} resources remain")
        state.dependencies = False
        save(state)


def prepare_compose(state: Environment) -> Path:
    """Materialize isolated Compose names from the current deployment definition."""
    definition = ROOT / "apps/studio-server/deploy/docker-compose-base.yaml"
    environment = {**os.environ, "STUDIO_ENV_FILE": state.env_file}
    raw = subprocess.check_output(
        (
            "docker",
            "compose",
            "--env-file",
            state.env_file,
            "-f",
            str(definition),
            "config",
            "--format",
            "json",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        timeout=30,
    )
    config = json.loads(raw)
    project = "tinkerfin-dev-" + state.owner[:12]
    config["name"] = project
    for service in config["services"].values():
        service.pop("container_name", None)
        service.pop("profiles", None)
        service.setdefault("labels", {})[LABEL] = state.owner
        for port in service.get("ports", []):
            require_free_port(int(port["published"]))
    for section in ("volumes", "networks"):
        for name, resource in config.setdefault(section, {}).items():
            if resource.get("external"):
                raise ValueError(
                    "Managed development dependencies cannot use external resources"
                )
            resource["name"] = f"{project}_{name}"
            resource.setdefault("labels", {})[LABEL] = state.owner
    path = DIRECTORY / "compose.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)
    return path


def readiness(state: Environment) -> dict[str, str]:
    result = {}
    for name, record in state.processes.items():
        if not owns_process(record, state.owner):
            result[name] = "exited"
            continue
        suffix = "/health/ready" if name == "server" else "/"
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{record.port}{suffix}", timeout=2
            ) as response:
                result[name] = "ready" if response.status == 200 else "starting"
        except (OSError, urllib.error.URLError):
            result[name] = "starting"
    return result


def start(state: Environment, *, dependencies: bool) -> None:
    """Start an explicit environment; failed readiness closes only owned resources."""
    if any(owns_process(item, state.owner) for item in state.processes.values()):
        raise ValueError("This environment is already running; use status or stop")
    if state.api_port == state.web_port:
        raise ValueError("API and web ports must differ")
    require_free_port(state.api_port)
    require_free_port(state.web_port)
    if not Path(state.env_file).is_file():
        raise ValueError(f"Create the server configuration first: {state.env_file}")
    if not (ROOT / "apps/studio-web/node_modules/.bin/vite").exists():
        raise ValueError(
            "Install web dependencies with pnpm --dir apps/studio-web install --frozen-lockfile"
        )
    try:
        if dependencies:
            compose = prepare_compose(state)
            state.dependencies = True
            save(state)
            command(
                "docker",
                "compose",
                "-f",
                str(compose),
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "90",
            )
        elif state.dependencies:
            command(
                "docker",
                "compose",
                "-f",
                str(DIRECTORY / "compose.json"),
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "90",
            )
        for name, port in (("server", state.api_port), ("web", state.web_port)):
            with (DIRECTORY / f"{name}.log").open("ab") as log:
                child = subprocess.Popen(
                    (
                        sys.executable,
                        "-m",
                        "scripts.dev",
                        "_run",
                        "--owner",
                        state.owner,
                        "--component",
                        name,
                    ),
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            started, _arguments = process_identity(child.pid)
            state.processes[name] = ProcessRecord(
                pid=child.pid, started=started, port=port
            )
            save(state)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = readiness(state)
            if status == {"server": "ready", "web": "ready"}:
                print(json.dumps(status))
                return
            if "exited" in status.values():
                break
            time.sleep(0.2)
        raise RuntimeError(f"Services did not become ready; inspect {DIRECTORY}/*.log")
    except BaseException:
        stop(state)
        raise


def supervise(owner: str, component: str) -> None:
    """Keep the process-group owner alive until its child exits after a stop signal."""
    state = load()
    if owner != state.owner:
        raise ValueError("Process owner does not match the environment")
    signal.signal(signal.SIGTERM, lambda *_args: None)
    signal.signal(signal.SIGINT, lambda *_args: None)
    environment = {**os.environ, "STUDIO_ENV_FILE": state.env_file}
    environment.pop("VIRTUAL_ENV", None)
    if component == "server":
        arguments = (
            "uv",
            "run",
            "--locked",
            "--package",
            "tinkerfin-studio",
            "python",
            "-m",
            "tinkerfin_studio",
            "--port",
            str(state.api_port),
        )
    else:
        environment["VITE_STUDIO_API_ADDRESS"] = f"http://127.0.0.1:{state.api_port}"
        arguments = (
            "pnpm",
            "--dir",
            str(ROOT / "apps/studio-web"),
            "dev",
            "--host",
            "127.0.0.1",
            "--port",
            str(state.web_port),
            "--strictPort",
        )
    with subprocess.Popen(arguments, cwd=ROOT, env=environment) as child:
        raise SystemExit(child.wait())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="stop preserves data; destroy permanently removes this environment's owned Docker volumes.",
    )
    parser.add_argument(
        "action", choices=("start", "status", "stop", "destroy", "_run")
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--web-port", type=int)
    parser.add_argument(
        "--dependencies",
        action="store_true",
        help="Create isolated Docker dependencies using this env file's ports",
    )
    parser.add_argument("--owner", help=argparse.SUPPRESS)
    parser.add_argument(
        "--component", choices=("server", "web"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.action == "_run":
        supervise(args.owner, args.component)
        return
    if args.action == "status" and not (DIRECTORY / "state.json").exists():
        print('{"environment":"not managed","processes":{}}')
        return
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DIRECTORY / "lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exists = (DIRECTORY / "state.json").exists()
        if not exists and args.action != "start":
            raise ValueError("No recorded environment exists in this worktree")
        state = (
            load()
            if exists
            else Environment(
                root=str(ROOT),
                owner=uuid4().hex,
                env_file=str(
                    (args.env_file or ROOT / "apps/studio-server/.env").resolve()
                ),
                api_port=args.api_port if args.api_port is not None else 8090,
                web_port=args.web_port if args.web_port is not None else 5190,
            )
        )
        if args.action == "start":
            updates = {
                key: value
                for key, value in {
                    "env_file": str(args.env_file.resolve()) if args.env_file else None,
                    "api_port": args.api_port,
                    "web_port": args.web_port,
                }.items()
                if value is not None and value != getattr(state, key)
            }
            if updates:
                if state.dependencies or any(
                    owns_process(item, state.owner) for item in state.processes.values()
                ):
                    raise ValueError(
                        "Stop services and destroy owned dependencies before changing their configuration"
                    )
                state = Environment.model_validate({**state.model_dump(), **updates})
            save(state)
            start(state, dependencies=args.dependencies)
        elif args.action == "status":
            print(
                json.dumps(
                    {
                        "processes": readiness(state),
                        "dependencies": {
                            kind: resource_ids(state, kind)
                            for kind in ("container", "network", "volume")
                        }
                        if state.dependencies
                        else "external",
                    }
                )
            )
        else:
            stop(state, destroy=args.action == "destroy")
            print(
                "Destroyed owned environment data"
                if args.action == "destroy"
                else "Stopped owned services; data retained"
            )


if __name__ == "__main__":
    main()
