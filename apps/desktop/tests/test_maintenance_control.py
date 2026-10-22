"""本机更新请求的取消、重复回执和关闭顺序，使用真实空闲宿主及隔离状态"""

import asyncio
from uuid import uuid4

import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from tinkerfin_desktop.configuration import CompanionConfiguration
from tinkerfin_desktop.host import CompanionHost
from tinkerfin_desktop.maintenance import MaintenanceControl
from tinkerfin_desktop.native import (
    MaintenanceCancel,
    MaintenanceCommit,
    MaintenancePrepare,
    incoming,
)
from tinkerfin_remote import RemoteError, RemoteErrorCode


@pytest.fixture
async def host(tmp_path):
    async def report(_status):
        return

    configuration = CompanionConfiguration(
        server="https://studio.example.test",
        device_id=str(uuid4()),
        namespace="ns_1",
        credential=SecretStr("ab" * 32),
        workspace_root=tmp_path / "projects",
    )
    async with CompanionHost(
        configuration,
        state_directory=tmp_path / "state",
        sandbox_executable=tmp_path / "unused-sandbox",
        report=report,
    ) as opened:
        yield opened


@pytest.mark.parametrize("ready_before_cancel", [False, True])
async def test_cancelled_update_wait_releases_admission_and_replays_receipt(
    host: CompanionHost, ready_before_cancel: bool
):
    replies: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue(maxsize=8)
    control = MaintenanceControl(host, replies.put)
    request = "a" * 32
    try:
        assert not await control.receive(
            MaintenancePrepare(kind="maintenance_prepare", request_id=request)
        )
        if ready_before_cancel:
            assert (await replies.get())["state"] == "ready"
            with pytest.raises(RemoteError) as overlap:
                async with host.maintenance():
                    pytest.fail("维护已被本次更新持有")
            assert overlap.value.code == RemoteErrorCode.CONFLICT
        assert not await control.receive(
            MaintenanceCancel(kind="maintenance_cancel", request_id=request)
        )
        assert await replies.get() == {
            "kind": "maintenance",
            "request_id": request,
            "state": "cancelled",
        }
        async with host.maintenance():
            pass
        assert not await control.receive(
            MaintenancePrepare(kind="maintenance_prepare", request_id=request)
        )
        assert (await replies.get())["state"] == "cancelled"
    finally:
        await control.close()


async def test_commit_keeps_maintenance_owned_until_the_host_has_closed(
    host: CompanionHost,
):
    replies: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue(maxsize=8)
    control = MaintenanceControl(host, replies.put)
    request = "a" * 32
    try:
        await control.receive(
            MaintenancePrepare(kind="maintenance_prepare", request_id=request)
        )
        assert (await replies.get())["state"] == "ready"
        assert not await control.receive(
            MaintenanceCancel(kind="maintenance_cancel", request_id="b" * 32)
        )
        assert await replies.get() == {
            "kind": "maintenance",
            "request_id": "b" * 32,
            "state": "failed",
        }
        await control.receive(
            MaintenancePrepare(kind="maintenance_prepare", request_id=request)
        )
        assert (await replies.get())["state"] == "ready"
        assert await control.receive(
            MaintenanceCommit(kind="maintenance_commit", request_id=request)
        )
        assert (await replies.get())["state"] == "committed"
        with pytest.raises(RemoteError) as held:
            async with host.maintenance():
                pytest.fail("提交安装后提前恢复了接纳")
        assert held.value.code == RemoteErrorCode.CONFLICT
        await host.aclose()
        await control.close()
        with pytest.raises(RuntimeError, match="未打开"):
            async with host.maintenance():
                pytest.fail("关闭后恢复了宿主")
    finally:
        await host.aclose()
        await control.close()


async def test_failed_preparation_has_a_stable_receipt_and_cannot_commit(
    host: CompanionHost,
):
    replies: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue(maxsize=8)
    control = MaintenanceControl(host, replies.put)
    request = "c" * 32
    await host.aclose()
    try:
        await control.receive(
            MaintenancePrepare(kind="maintenance_prepare", request_id=request)
        )
        assert (await replies.get())["state"] == "failed"
        assert not await control.receive(
            MaintenanceCommit(kind="maintenance_commit", request_id=request)
        )
        assert (await replies.get())["state"] == "failed"
    finally:
        await control.close()


@pytest.mark.parametrize(
    "kind", ["maintenance_prepare", "maintenance_cancel", "maintenance_commit"]
)
def test_maintenance_control_accepts_only_bound_request_identities(kind: str):
    assert (
        incoming.validate_python({"kind": kind, "request_id": "a" * 32}).request_id
        == "a" * 32
    )
    for payload in (
        {"kind": kind},
        {"kind": kind, "request_id": "other"},
        {"kind": kind, "request_id": "a" * 32, "version": 1},
    ):
        with pytest.raises(ValidationError):
            incoming.validate_python(payload)
