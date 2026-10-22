"""本机资源等待使用设备通知，取消与设备替换不触发误执行"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import create_autospec

import pytest
from device_fakes import SOURCE, create_execution
from pydantic import SecretStr
from sqlalchemy import select
from test_agent_runtime import _Workspace

from tinkerfin_contracts import RunIdentity
from tinkerfin_remote import RemoteTools, WorkspaceRejected
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices import workspace as workspace_module
from tinkerfin_studio.devices.models import Device
from tinkerfin_studio.devices.profiles import DeviceProfiles
from tinkerfin_studio.devices.schemas import AuthorizationStart, DevicePresence
from tinkerfin_studio.devices.workspace import ComputerWorkspace
from tinkerfin_studio.skills.repository import SkillRepository


@asynccontextmanager
async def waiting_run(declaration, identity):
    opened = asyncio.Event()
    release = asyncio.Event()

    async def borrow():
        async with declaration.prepare(identity):
            opened.set()
            await release.wait()

    owner = asyncio.create_task(borrow())
    try:
        yield owner, opened, release
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("action", ["connect", "cancel", "revoke"])
async def test_offline_bound_computer_waits_without_dispatch(
    database, notifications, monkeypatch, action
):
    execution = await create_execution(database)
    async with database.session() as session:
        device = await session.get(Device, execution.binding.device_id)
        assert device is not None
        device.connection_id = None
        await session.commit()
    inspected = asyncio.Event()
    original_list = execution.devices.list_devices

    async def list_devices(user_id):
        snapshot = await original_list(user_id)
        inspected.set()
        return snapshot

    monkeypatch.setattr(execution.devices, "list_devices", list_devices)
    monkeypatch.setattr(workspace_module, "DeviceService", lambda _: execution.devices)
    remote = create_autospec(RemoteTools, instance=True)
    prepared = _Workspace()
    remote.endpoint.return_value.workspace.return_value = prepared
    declaration = ComputerWorkspace(
        database=database,
        remote_tools=remote,
        notifications=notifications,
        user_id=1,
        project_id="project",
        access_mode="write_approval",
    )
    async with waiting_run(declaration, SOURCE) as (owner, opened, release):
        await inspected.wait()
        assert not opened.is_set()
        remote.endpoint.assert_not_called()
        if action == "cancel":
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
        else:
            if action == "connect":
                await execution.devices.connect(execution.token, DevicePresence())
            else:
                await execution.devices.revoke(1, execution.binding.device_id)
            await notify_change(
                notifications,
                user_id=1,
                topic="studio.devices.changed",
                key=execution.binding.device_id,
            )
            if action == "revoke":
                with pytest.raises(WorkspaceRejected):
                    await owner
            else:
                await opened.wait()
                remote.endpoint.assert_called_once_with(
                    namespace=SOURCE.namespace, target=execution.binding.device_id
                )
                release.set()
                await owner
    assert prepared.opened == prepared.closed
    assert len(prepared.opened) == (1 if action == "connect" else 0)


async def test_unconfigured_computer_can_be_connected_while_run_waits(
    database, notifications, monkeypatch
):
    execution = await create_execution(database)
    await execution.devices.revoke(1, execution.binding.device_id)
    identity = RunIdentity(
        namespace=SOURCE.namespace, thread_id=SOURCE.thread_id, run_id="new-turn"
    )
    async with database.session() as session:
        thread = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.thread_id == SOURCE.thread_id
            )
        )
        assert thread is not None
        await ConversationRepository(session).create_run_registration(
            thread_id=thread.id,
            run_id=identity.run_id,
            parent_run_id=None,
            model_id="main",
            input_json={},
        )
        await SkillRepository(session, 1).capture(
            identity, project_id="project", selected_ids=()
        )
        await session.commit()
    waiting = asyncio.Event()
    capture = DeviceProfiles.capture

    async def observe_capture(self, **kwargs):
        try:
            return await capture(self, **kwargs)
        except BusinessException as error:
            assert error.error_code == DeviceErrorCode.NOT_FOUND
            waiting.set()
            raise

    monkeypatch.setattr(DeviceProfiles, "capture", observe_capture)
    monkeypatch.setattr(workspace_module, "DeviceService", lambda _: execution.devices)
    remote = create_autospec(RemoteTools, instance=True)
    prepared = _Workspace()
    remote.endpoint.return_value.workspace.return_value = prepared
    declaration = ComputerWorkspace(
        database=database,
        remote_tools=remote,
        notifications=notifications,
        user_id=1,
        project_id="project",
        access_mode="write_approval",
    )
    async with waiting_run(declaration, identity) as (owner, opened, release):
        await waiting.wait()
        remote.endpoint.assert_not_called()
        credential = "cd" * 32
        authorization = await execution.devices.create_authorization(
            AuthorizationStart(name="Work Mac", credential=SecretStr(credential))
        )
        await execution.devices.approve_authorization(
            1, authorization.authorization_id, authorization.browser_token, 1
        )
        status = await execution.devices.authorization_status(
            authorization.authorization_id, credential
        )
        assert status.device is not None
        await execution.devices.connect(
            f"{status.device.device_id}.{credential}", DevicePresence()
        )
        await notify_change(
            notifications,
            user_id=1,
            topic="studio.devices.changed",
            key=status.device.device_id,
        )
        await opened.wait()
        remote.endpoint.assert_called_once_with(
            namespace=SOURCE.namespace, target=status.device.device_id
        )
        release.set()
        await owner
    assert prepared.opened == prepared.closed == [identity]
