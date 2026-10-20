"""Exercise exact Docker ownership with disposable resources and a borrowed image."""

import json
import os
from uuid import uuid4

import pytest
from scripts import dev

pytestmark = pytest.mark.docker_integration


def test_destroy_removes_only_environment_owned_resources(tmp_path, monkeypatch):
    image = os.environ.get("TINKERFIN_DEV_TEST_IMAGE")
    if not image:
        pytest.skip(
            "Select an existing local image containing cat; the test never pulls"
        )
    dev.command("docker", "image", "inspect", image)
    monkeypatch.setattr(dev, "DIRECTORY", tmp_path)
    run = uuid4().hex
    owner = uuid4().hex
    state = dev.Environment(
        root=str(dev.ROOT),
        owner=owner,
        env_file=str(tmp_path / ".env"),
        api_port=8090,
        web_port=5190,
        dependencies=True,
    )
    resources = []

    def create(kind, name, resource_owner):
        arguments = [
            "docker",
            kind,
            "create",
            "--label",
            f"{dev.LABEL}={resource_owner}",
            "--label",
            f"io.tinkerfin.test.run={run}",
        ]
        if kind == "container":
            arguments.extend(("--name", name, "-i", "--entrypoint", "cat", image))
        else:
            arguments.append(name)
        identifier = dev.command(*arguments)
        resources.append((kind, identifier))
        return identifier

    try:
        create("network", "tf-test-" + run, owner)
        create("volume", "tf-test-" + run, owner)
        managed = create("container", "tf-managed-" + run, owner)
        borrowed = create("container", "tf-borrowed-" + run, uuid4().hex)
        dev.command("docker", "container", "start", managed, borrowed)
        dev.stop(state)
        assert not json.loads(dev.command("docker", "container", "inspect", managed))[
            0
        ]["State"]["Running"]
        assert json.loads(dev.command("docker", "container", "inspect", borrowed))[0][
            "State"
        ]["Running"]
        assert dev.resource_ids(state, "volume")
        dev.stop(state, destroy=True)
        for kind in ("container", "network", "volume"):
            assert not dev.resource_ids(state, kind)
        assert json.loads(dev.command("docker", "container", "inspect", borrowed))[0][
            "State"
        ]["Running"]
    finally:
        for kind, identifier in reversed(resources):
            raw = dev.command("docker", kind, "inspect", identifier, check=False)
            records = json.loads(raw or "[]")
            if not records:
                continue
            record = records[0]
            labels = (
                record["Config"]["Labels"] if kind == "container" else record["Labels"]
            )
            assert labels["io.tinkerfin.test.run"] == run
            dev.command(
                "docker",
                kind,
                "rm",
                *(("--force",) if kind == "container" else ()),
                identifier,
            )
        for kind in ("container", "network", "volume"):
            assert not dev.command(
                "docker",
                kind,
                "ls",
                *(("--all",) if kind == "container" else ()),
                "--filter",
                f"label=io.tinkerfin.test.run={run}",
                "--quiet",
            )
