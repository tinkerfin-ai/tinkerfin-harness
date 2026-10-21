"""设备测试共用的已授权云端运行与本机工作区"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import SecretStr

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices.profiles import DeviceProfileBinding, DeviceProfiles
from tinkerfin_studio.devices.schemas import (
    AuthorizationStart,
    DevicePresence,
)
from tinkerfin_studio.devices.service import DeviceService
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.skills.repository import SkillRepository

DEVICE_SECRET = "ab" * 32
SOURCE = RunIdentity(namespace="ns_1", thread_id="cloud-thread", run_id="cloud-run")


@dataclass(frozen=True, slots=True)
class LocalExecution:
    token: str
    binding: DeviceProfileBinding
    devices: DeviceService


async def create_execution(database: Database) -> LocalExecution:
    async with database.session() as session:
        session.add(
            Project(
                id="project",
                user_id=1,
                name="Project",
                created_at=datetime(2030, 1, 1),
                updated_at=datetime(2030, 1, 1),
            )
        )
        session.add(
            User(
                id=1,
                username="owner",
                password_hash="fixture",
                roles=[],
                disabled=False,
            )
        )
        await session.commit()
    devices = DeviceService(database, now=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    authorization = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(DEVICE_SECRET))
    )
    await devices.approve_authorization(
        1, authorization.authorization_id, authorization.browser_token, 1
    )
    status = await devices.authorization_status(
        authorization.authorization_id, DEVICE_SECRET
    )
    assert status.device is not None
    device = status.device
    token = f"{device.device_id}.{DEVICE_SECRET}"
    await devices.connect(
        token,
        DevicePresence(),
    )
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=1,
            project_id="project",
            thread_id=SOURCE.thread_id,
            title="Local execution",
            model_id="main",
        )
        run = await repository.create_run_registration(
            thread_id=thread.id,
            run_id=SOURCE.run_id,
            parent_run_id=None,
            model_id="main",
            input_json={},
        )
        await SkillRepository(session, 1).capture(
            SOURCE, project_id="project", selected_ids=()
        )
        binding = await DeviceProfiles(session, 1).capture(
            identity=SOURCE,
            project_id="project",
            access_mode="write_approval",
        )
        assert binding is not None
        run.local_profile_id = binding.profile_id
        run.status = "running"
        await session.commit()
    return LocalExecution(token, binding, devices)
