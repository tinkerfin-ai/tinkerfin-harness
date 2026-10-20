"""Development tools must not adopt stale PIDs, occupied ports, or borrowed data."""

import socket

import pytest
from scripts import dev


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "DIRECTORY", tmp_path)
    return dev.Environment(
        root=str(dev.ROOT),
        owner="a" * 32,
        env_file=str(tmp_path / ".env"),
        api_port=8090,
        web_port=5190,
    )


@pytest.mark.parametrize(
    "identity",
    [
        (
            "different creation",
            "-m scripts.dev _run --owner " + "a" * 32 + " --component web",
        ),
        ("created", "-m scripts.dev _run --owner " + "b" * 32 + " --component web"),
        ("", ""),
    ],
)
def test_stale_or_borrowed_pid_is_never_signalled(environment, monkeypatch, identity):
    environment.processes["web"] = dev.ProcessRecord(
        pid=123, started="created", port=5190
    )
    monkeypatch.setattr(dev, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(
        dev.os, "killpg", lambda *_args: pytest.fail("Unowned PID was signalled")
    )
    dev.stop(environment)
    assert not dev.load().processes


def test_occupied_port_fails_without_touching_its_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match="occupied"):
            dev.require_free_port(port)
        assert listener.getsockname()[1] == port


def test_docker_ownership_is_rechecked_before_any_mutation(environment, monkeypatch):
    environment.dependencies = True
    monkeypatch.setattr(dev, "resource_ids", lambda *_args: ["borrowed-container"])
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        assert args == ("docker", "container", "inspect", "borrowed-container")
        return '[{"Config":{"Labels":{"io.tinkerfin.dev.owner":"another-owner"}}}]'

    monkeypatch.setattr(dev, "command", command)
    with pytest.raises(ValueError, match="borrowed"):
        dev.stop(environment, destroy=True)
    assert len(calls) == 1


def test_state_from_another_worktree_cannot_be_adopted(environment):
    environment.root = "/different/worktree"
    dev.save(environment)
    with pytest.raises(ValueError, match="another worktree"):
        dev.load()
