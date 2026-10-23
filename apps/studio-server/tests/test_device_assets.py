"""本机技能与文件交付的授权、内容身份和重传契约"""

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest
from device_fakes import SOURCE, create_execution
from httpx import ASGITransport, AsyncClient
from langgraph.store.memory import InMemoryStore
from sqlalchemy import select

from tinkerfin import TinkerFin
from tinkerfin_contracts import RunIdentity
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.application import create_application
from tinkerfin_studio.attachments.entity import AttachmentFile
from tinkerfin_studio.attachments.service import byte_chunks
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.devices.models import DeviceExecutionProfile
from tinkerfin_studio.devices.profiles import DeviceProfiles
from tinkerfin_studio.devices.schemas import LocalFileContent, LocalSkillPackage
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.packages import SkillFile, parse_package
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import SkillReference


@pytest.fixture
async def device_assets(database, components_database, attachments):
    execution = await create_execution(database)
    package = parse_package(
        (
            SkillFile(
                "SKILL.md",
                b"---\nname: reports\ndescription: Prepare reports\n---\nRead the inputs.",
            ),
            SkillFile("scripts/report.py", b"print('report')"),
        )
    )
    content = SkillContentStore(TinkerFin(store=InMemoryStore()))
    await content.save(1, package)
    async with database.session() as session:
        profile = await session.get(
            DeviceExecutionProfile, execution.binding.profile_id
        )
        assert profile is not None
        profile.skills = [
            SkillReference(
                installation_id="reports",
                name=package.name,
                digest=package.digest,
                selected=True,
            ).model_dump(mode="json")
        ]
        await session.commit()
    app = create_application(lifespan=None)
    app.state.resources = SimpleNamespace(
        database=database,
        components_database=components_database,
        attachments=attachments,
        skills=SimpleNamespace(content=content),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://studio.test",
        headers={"Authorization": "Bearer " + execution.token},
        params={
            "thread_id": SOURCE.thread_id,
            "run_id": SOURCE.run_id,
            "invocation_id": "deliver-one",
            "file_key": "a" * 64,
        },
    ) as client:
        yield client, execution, package


async def test_skill_download_is_bound_to_the_saved_execution_authorization(
    device_assets,
):
    client, execution, package = device_assets
    response = await client.get(
        f"/api/devices/executions/{execution.binding.profile_id}/skills/reports"
    )
    assert response.status_code == 200
    output = ApiResponse[LocalSkillPackage].model_validate_json(response.content).data
    assert output is not None and output.digest == package.digest
    assert [(file.path, base64.b64decode(file.content)) for file in output.files] == [
        (file.path, file.content) for file in package.files
    ]
    assert (
        await client.get(
            f"/api/devices/executions/{execution.binding.profile_id}/skills/another"
        )
    ).status_code == 404
    assert (
        await client.get(f"/api/devices/executions/{'c' * 64}/skills/reports")
    ).status_code == 404


async def test_file_retransmission_returns_one_published_attachment_and_conflicting_retransmission_is_rejected(
    device_assets, database, attachment_storage, monkeypatch
):
    client, execution, _package = device_assets
    entered, release = asyncio.Event(), asyncio.Event()
    put = attachment_storage.put
    stored: list[str] = []

    async def hold(key, chunks):
        entered.set()
        await release.wait()
        stored.append(key)
        await put(key, chunks)

    monkeypatch.setattr(attachment_storage, "put", hold)
    path = f"/api/devices/executions/{execution.binding.profile_id}/files"
    async with asyncio.TaskGroup() as group:
        first = group.create_task(
            client.post(path, params={"name": "report.md"}, content=b"# report")
        )
        await entered.wait()
        try:
            competing = await client.post(
                path, params={"name": "report.md"}, content=b"# report"
            )
            assert competing.status_code == 409
        finally:
            release.set()
    assert first.result().status_code == 200
    result = ApiResponse[Attachment].model_validate_json(first.result().content).data
    assert result is not None
    repeated = await client.post(
        path,
        params={"name": "report.md", "invocation_id": "retry-call"},
        content=b"# report",
    )
    assert repeated.status_code == 200
    assert repeated.json() == first.result().json()
    assert stored == [result.id]
    changed = await client.post(
        path, params={"name": "report.md"}, content=b"# updated"
    )
    assert changed.status_code == 409
    another = await client.post(
        path,
        params={
            "name": "report.md",
            "invocation_id": "deliver-two",
            "file_key": "b" * 64,
        },
        content=b"# updated",
    )
    assert another.status_code == 200 and another.json()["data"]["id"] != result.id
    async with database.session() as session:
        files = list(await session.scalars(select(AttachmentFile)))
        assert len(files) == 2
        assert len({file.message_id for file in files}) == 2
        assert all(file.message_id is not None for file in files)
    read = await client.get(path + "/" + result.id)
    output = ApiResponse[LocalFileContent].model_validate_json(read.content).data
    assert output is not None and base64.b64decode(output.content) == b"# report"
    assert len((await client.get(path)).json()["data"]) == 2


async def test_one_invocation_delivers_eleven_files_and_retries_without_duplicate_publication(
    device_assets, database
):
    client, execution, _ = device_assets
    path = f"/api/devices/executions/{execution.binding.profile_id}/files"
    contents = [
        f'编号,说明\r\n00{i:03},"中文,记录"\r\n'.encode("utf-8-sig") for i in range(11)
    ]
    identities = []
    for index, data in enumerate(contents):
        response = await client.post(
            path,
            params={
                "name": "report.csv",
                "file_key": hashlib.sha256(str(index).encode()).hexdigest(),
            },
            content=data,
        )
        assert response.status_code == 200
        identities.append(response.json()["data"]["id"])
    assert len(set(identities)) == 11
    for index, data in enumerate(contents):
        repeated = await client.post(
            path,
            params={
                "name": "report.csv",
                "file_key": hashlib.sha256(str(index).encode()).hexdigest(),
                "invocation_id": "receipt-recovery",
            },
            content=data,
        )
        assert (
            repeated.status_code == 200
            and repeated.json()["data"]["id"] == identities[index]
        )
        downloaded = await client.get(path + "/" + identities[index])
        assert base64.b64decode(downloaded.json()["data"]["content"]) == data
    async with database.session() as session:
        rows = list(await session.scalars(select(AttachmentFile)))
        assert len(rows) == 11 and len({row.message_id for row in rows}) == 1


async def test_same_device_recovers_file_receipt_in_a_new_authorized_run(
    device_assets, database
):
    client, execution, _ = device_assets
    first = await client.post(
        f"/api/devices/executions/{execution.binding.profile_id}/files",
        params={"name": "report.txt"},
        content=b"original",
    )
    assert first.status_code == 200
    next_identity = RunIdentity(
        namespace=SOURCE.namespace, thread_id=SOURCE.thread_id, run_id="next-run"
    )
    async with database.session() as session:
        thread = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.thread_id == SOURCE.thread_id
            )
        )
        assert thread is not None
        run = await ConversationRepository(session).create_run_registration(
            thread_id=thread.id,
            run_id=next_identity.run_id,
            parent_run_id=SOURCE.run_id,
            model_id="main",
            input_json={},
        )
        await SkillRepository(session, 1).capture(
            next_identity, project_id="project", selected_ids=()
        )
        binding = await DeviceProfiles(session, 1).capture(
            identity=next_identity, project_id="project", access_mode="write_approval"
        )
        assert (
            binding is not None and binding.profile_id != execution.binding.profile_id
        )
        run.status = "running"
        await session.commit()
    repeated = await client.post(
        f"/api/devices/executions/{binding.profile_id}/files",
        params={
            "name": "report.txt",
            "run_id": next_identity.run_id,
            "invocation_id": "new-call",
        },
        content=b"original",
    )
    assert repeated.status_code == 200 and repeated.json() == first.json()
    async with database.session() as session:
        assert len(list(await session.scalars(select(AttachmentFile)))) == 1


async def test_device_cannot_read_other_conversations_or_send_files_after_revocation(
    device_assets, database, attachments
):
    client, execution, _package = device_assets
    async with database.session() as session:
        await ConversationRepository(session).create_thread(
            user_id=1,
            project_id="project",
            thread_id="other",
            title="Other",
            model_id="main",
        )
        await session.commit()
    foreign = await attachments.upload(
        user_id=1,
        thread_id="other",
        name="private.md",
        chunks=byte_chunks(b"# private"),
    )
    assert (
        await client.get(
            f"/api/devices/executions/{execution.binding.profile_id}/files/{foreign.id}"
        )
    ).status_code == 404
    await execution.devices.revoke(1, execution.binding.device_id)

    async def unopened():
        pytest.fail("未授权请求不得读取上传正文")
        yield b"unused"

    denied = await client.post(
        f"/api/devices/executions/{execution.binding.profile_id}/files",
        params={"name": "report.md"},
        content=unopened(),
    )
    assert denied.status_code == 401


async def test_revocation_during_accepted_delivery_returns_its_committed_receipt(
    device_assets, attachment_storage, monkeypatch, database
):
    client, execution, _package = device_assets
    entered, release = asyncio.Event(), asyncio.Event()
    put = attachment_storage.put

    async def hold(key, chunks):
        entered.set()
        await release.wait()
        await put(key, chunks)

    monkeypatch.setattr(attachment_storage, "put", hold)
    path = f"/api/devices/executions/{execution.binding.profile_id}/files"
    async with asyncio.TaskGroup() as group:
        sending = group.create_task(
            client.post(path, params={"name": "accepted.md"}, content=b"# accepted")
        )
        await entered.wait()
        try:
            await execution.devices.revoke(1, execution.binding.device_id)
        finally:
            release.set()
    assert sending.result().status_code == 200
    attachment = (
        ApiResponse[Attachment].model_validate_json(sending.result().content).data
    )
    assert attachment is not None
    async with database.session() as session:
        saved = await session.get(AttachmentFile, attachment.id)
        assert (
            saved is not None
            and saved.status == "ready"
            and saved.message_id is not None
        )
    denied = await client.post(
        path, params={"name": "next.md", "invocation_id": "next"}, content=b"# denied"
    )
    assert denied.status_code == 401
    async with database.session() as session:
        assert len(list(await session.scalars(select(AttachmentFile)))) == 1
