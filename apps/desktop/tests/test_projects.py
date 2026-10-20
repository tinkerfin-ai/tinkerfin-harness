"""自动项目工作区的路径隔离、目录身份及恢复权限"""

from uuid import uuid4

import pytest
from test_cloud import configuration

from tinkerfin_desktop.projects import ManagedProjects


def projects(tmp_path, identity, suffix="state"):
    state = tmp_path / suffix
    state.mkdir()
    return ManagedProjects(identity, state)


async def test_projects_are_independent_and_stable_across_reconnection(tmp_path):
    identity = configuration(tmp_path)
    manager = projects(tmp_path, identity)
    first, second = str(uuid4()), str(uuid4())
    one, two = await manager.prepare(first), await manager.prepare(second)
    (one / "report.txt").write_text("first")
    assert not (two / "report.txt").exists()
    assert await manager.prepare(first) == one
    assert one.stat().st_mode & 0o777 == 0o700
    replacement = projects(
        tmp_path, identity.model_copy(update={"device_id": str(uuid4())}), "reconnected"
    )
    assert await replacement.prepare(first) == one
    for changes in ({"server": "https://other.test"}, {"namespace": "ns_2"}):
        another = projects(tmp_path, identity.model_copy(update=changes), str(uuid4()))
        assert await another.prepare(first) != one


async def test_workspace_replacement_symlinks_and_path_input_are_rejected(tmp_path):
    identity = configuration(tmp_path)
    manager = projects(tmp_path, identity)
    project = str(uuid4())
    directory = await manager.prepare(project)
    moved = directory.with_name("moved")
    directory.rename(moved)
    with pytest.raises(ValueError, match="移除"):
        await manager.prepare(project)
    directory.mkdir()
    with pytest.raises(ValueError, match="身份"):
        await manager.prepare(project)
    directory.rmdir()
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="链接"):
        await manager.prepare(project)
    for invalid in ("../settings.json", "project", project.upper()):
        with pytest.raises(ValueError):
            await manager.prepare(invalid)


async def test_permissions_are_frozen_for_workspace_scope_and_revocation_blocks_resume(
    tmp_path,
):
    identity = configuration(tmp_path)
    manager = projects(tmp_path, identity)
    project, scope = str(uuid4()), "a" * 64
    original = await manager.permissions_for(project, scope)
    expanded = identity.model_copy(
        update={"permissions": original.model_copy(update={"browser": True})}
    )
    reopened = ManagedProjects(expanded, tmp_path / "state")
    assert await reopened.permissions_for(project, scope) == original
    assert (await reopened.permissions_for(project, "b" * 64)).browser
    with pytest.raises(ValueError, match="撤销"):
        await manager.permissions_for(project, "b" * 64)
    with pytest.raises(ValueError, match="项目"):
        await reopened.permissions_for(str(uuid4()), scope)


async def test_network_grants_keep_restricted_scope_and_reject_downgraded_normal(
    tmp_path,
):
    from tinkerfin_desktop.configuration import WorkspacePermissions

    identity = configuration(tmp_path)
    manager = projects(tmp_path, identity)
    project = str(uuid4())
    assert (await manager.permissions_for(project, "a" * 64)).network == "normal"
    limited = identity.model_copy(
        update={
            "permissions": WorkspacePermissions(
                network="restricted", allowed_domains=("example.com",)
            )
        }
    )
    restricted = ManagedProjects(limited, tmp_path / "state")
    with pytest.raises(ValueError, match="撤销"):
        await restricted.permissions_for(project, "a" * 64)
    grant = await restricted.permissions_for(project, "b" * 64)
    assert grant.network == "restricted"
    assert await manager.permissions_for(project, "b" * 64) == grant


async def test_browser_profiles_preserve_login_data_and_account_project_isolation(
    tmp_path,
):
    identity = configuration(tmp_path)
    manager = projects(tmp_path, identity)
    project = str(uuid4())
    directory = await manager.prepare(project)
    profile = await manager.browser_profile(project, "thread")
    assert profile.parent == directory / ".tinkerfin" / "browser-profiles"
    assert await manager.browser_profile(project, "another-thread") != profile
    (profile / "login-fixture").write_bytes(b"account-one")
    profile.chmod(0o755)
    profile.parent.chmod(0o755)
    assert await manager.browser_profile(project, "thread") == profile
    assert (profile / "login-fixture").read_bytes() == b"account-one"
    assert profile.stat().st_mode & 0o777 == 0o700
    assert profile.parent.stat().st_mode & 0o777 == 0o700
    assert await manager.browser_profile(str(uuid4()), "thread") != profile
    other = projects(
        tmp_path, identity.model_copy(update={"namespace": "ns_2"}), "other-state"
    )
    assert await other.browser_profile(project, "thread") != profile
    reconnected = projects(
        tmp_path, identity.model_copy(update={"device_id": str(uuid4())}), "new-device"
    )
    assert await reconnected.browser_profile(project, "thread") == profile
    assert (profile / "login-fixture").read_bytes() == b"account-one"


@pytest.mark.parametrize("component", [".tinkerfin", ".tinkerfin/browser-profiles"])
async def test_browser_profile_rejects_symlinked_components_without_touching_target(
    tmp_path, component
):
    manager = projects(tmp_path, configuration(tmp_path))
    project = str(uuid4())
    directory = await manager.prepare(project)
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    (target / "proof").write_bytes(b"untouched")
    link = directory / component
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="链接"):
        await manager.browser_profile(project, "thread")
    assert (target / "proof").read_bytes() == b"untouched"
    assert target.stat().st_mode & 0o777 == 0o755


async def test_browser_profile_refuses_another_directory_owner(tmp_path, monkeypatch):
    import os

    manager = projects(tmp_path, configuration(tmp_path))
    project = str(uuid4())
    directory = await manager.prepare(project)
    actual = os.fstat

    def foreign_owner(descriptor):
        fields = list(actual(descriptor))
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(ValueError, match="归属"):
        await manager.browser_profile(project, "thread")
    assert not (directory / ".tinkerfin" / "browser-profiles").exists()
