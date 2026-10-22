"""设备凭证、授权所有权与失效连接的确定性验证"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, insert
from sqlalchemy.exc import IntegrityError
from starlette.types import Message, Scope

from tinkerfin_contracts import RunIdentity
from tinkerfin_remote import RemoteError, RemoteErrorCode, RemoteTools
from tinkerfin_studio.api import device_router
from tinkerfin_studio.api.dependencies import get_network_user_context
from tinkerfin_studio.api.errors import BusinessException, DeviceErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.devices.models import (
    Device,
    DeviceAuthorization,
)
from tinkerfin_studio.devices.schemas import (
    AuthorizationCreated,
    AuthorizationStart,
    AuthorizationStatus,
    ConnectedDevice,
    DeviceCapabilities,
    DeviceCapabilityUpdate,
    DevicePresence,
    PairedDevice,
)
from tinkerfin_studio.devices.service import DeviceService
from tinkerfin_studio.infrastructure.database import Database

CREDENTIAL = "42" * 32


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
async def users(database: Database) -> None:
    async with database.session() as session:
        session.add_all(
            [
                User(
                    id=index,
                    username=f"user-{index}",
                    password_hash="fixture",
                    roles=[],
                    disabled=False,
                )
                for index in (1, 2)
            ]
        )
        await session.commit()


@pytest.fixture
def devices(database: Database, clock: Clock) -> DeviceService:
    return DeviceService(database, now=clock.now)


async def paired(devices: DeviceService) -> tuple[PairedDevice, str]:
    authorization = await devices.create_authorization(
        AuthorizationStart(name="My Mac", credential=SecretStr(CREDENTIAL))
    )
    await devices.approve_authorization(
        1, authorization.authorization_id, authorization.browser_token, 1
    )
    status = await devices.authorization_status(
        authorization.authorization_id, CREDENTIAL
    )
    assert status.device is not None
    return status.device, f"{status.device.device_id}.{CREDENTIAL}"


async def test_authorization_separates_capabilities_and_stores_only_hashes(
    devices, database
):
    created = await devices.create_authorization(
        AuthorizationStart(name="My Mac", credential=SecretStr(CREDENTIAL))
    )
    assert (
        await devices.authorization_status(created.authorization_id, CREDENTIAL)
    ).state == "waiting"
    preview = await devices.preview_authorization(
        1, created.authorization_id, created.browser_token
    )
    assert preview.name == "My Mac"
    for proof in (created.browser_token, "ab" * 32):
        with pytest.raises(BusinessException):
            await devices.authorization_status(created.authorization_id, proof)
    with pytest.raises(BusinessException):
        await devices.approve_authorization(1, created.authorization_id, CREDENTIAL, 1)
    with pytest.raises(BusinessException):
        await devices.approve_authorization(
            2, created.authorization_id, created.browser_token, 1
        )
    await devices.approve_authorization(
        1, created.authorization_id, created.browser_token, 1
    )
    result = await devices.authorization_status(created.authorization_id, CREDENTIAL)
    assert result.device is not None
    principal = await devices.authenticate(f"{result.device.device_id}.{CREDENTIAL}")
    assert principal.user_id == 1 and principal.namespace == "ns_1"
    async with database.session() as session:
        stored = await session.get(Device, result.device.device_id)
        record = await session.get(DeviceAuthorization, created.authorization_id)
        assert stored is not None and record is not None
        assert stored.credential_digest == record.credential_digest != CREDENTIAL
        assert (
            record.browser_token_digest != created.browser_token
            and 0 <= record.slot < 1024
        )
    output = [view.model_dump_json() for view in await devices.list_devices(1)]
    assert all(
        CREDENTIAL not in item
        and created.browser_token not in item
        and "digest" not in item
        for item in output
    )


async def test_authorization_expiry_and_cancellation_preserve_current_device(
    devices, clock
):
    current, token = await paired(devices)
    created = await devices.create_authorization(
        AuthorizationStart(name="Next Mac", credential=SecretStr("ab" * 32))
    )
    clock.value += timedelta(minutes=10)
    assert (
        await devices.authorization_status(created.authorization_id, "ab" * 32)
    ).state == "expired"
    assert (
        await devices.approve_authorization(
            1, created.authorization_id, created.browser_token, 1
        )
    ).preview.state == "expired"
    assert (await devices.authenticate(token)).device_id == current.device_id
    next_request = await devices.create_authorization(
        AuthorizationStart(name="Next Mac", credential=SecretStr("cd" * 32))
    )
    assert (
        await devices.cancel_authorization(next_request.authorization_id, "cd" * 32)
    ).state == "cancelled"
    assert (
        await devices.approve_authorization(
            1, next_request.authorization_id, next_request.browser_token, 1
        )
    ).preview.state == "cancelled"
    assert (await devices.authenticate(token)).device_id == current.device_id


async def test_latest_concurrent_approval_invalidates_old_credentials_and_replay(
    devices,
):
    old, old_token = await paired(devices)
    issued = [
        await devices.create_authorization(
            AuthorizationStart(name="Mac", credential=SecretStr(secret))
        )
        for secret in ("ab" * 32, "cd" * 32)
    ]
    await asyncio.gather(
        *(
            devices.approve_authorization(
                1, item.authorization_id, item.browser_token, 1
            )
            for item in issued
        )
    )
    states = [
        await devices.authorization_status(item.authorization_id, secret)
        for item, secret in zip(issued, ("ab" * 32, "cd" * 32), strict=True)
    ]
    assert sorted(state.state for state in states) == ["authorized", "superseded"]
    active = await devices.list_devices(1)
    assert len(active) == 1 and active[0].device_id != old.device_id
    for item, state in zip(issued, states, strict=True):
        assert (
            await devices.approve_authorization(
                1, item.authorization_id, item.browser_token, 1
            )
        ).preview.state == state.state
    assert (await devices.list_devices(1))[0].device_id == active[0].device_id
    with pytest.raises(BusinessException):
        await devices.authenticate(old_token)


async def test_competing_accounts_cannot_approve_the_same_request(devices):
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    outcomes = await asyncio.gather(
        *(
            devices.approve_authorization(
                user, created.authorization_id, created.browser_token, user
            )
            for user in (1, 2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, BusinessException) for result in outcomes) == 1
    assert len(await devices.list_devices(1)) + len(await devices.list_devices(2)) == 1


async def test_approval_winning_cancellation_is_saved_and_rename_is_authenticated(
    devices,
):
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    await devices.approve_authorization(
        1, created.authorization_id, created.browser_token, 1
    )
    result = await devices.cancel_authorization(created.authorization_id, CREDENTIAL)
    assert result.state == "authorized" and result.device is not None
    assert (
        await devices.deny_authorization(
            created.authorization_id, created.browser_token
        )
    ).state == "authorized"
    token = f"{result.device.device_id}.{CREDENTIAL}"
    assert (await devices.rename(token, "办公室 Mac")).name == "办公室 Mac"
    assert (await devices.list_devices(1))[0].name == "办公室 Mac"
    with pytest.raises(BusinessException):
        await devices.rename(token + "0", "Wrong")


async def test_anonymous_capacity_is_bounded_and_expired_slots_are_reclaimed(
    devices, database, clock
):
    now = clock.now().replace(tzinfo=None)
    async with database.session() as session:
        await session.execute(
            insert(DeviceAuthorization),
            [
                {
                    "authorization_id": str(uuid4()),
                    "device_id": str(uuid4()),
                    "name": "Pending Mac",
                    "credential_digest": "a" * 64,
                    "browser_token_digest": "b" * 64,
                    "slot": slot,
                    "created_at": now,
                    "expires_at": now + timedelta(minutes=10),
                }
                for slot in range(1024)
            ],
        )
        await session.commit()
    with pytest.raises(BusinessException) as full:
        await devices.create_authorization(
            AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
        )
    assert full.value.error_code == DeviceErrorCode.LIMIT_REACHED
    clock.value += timedelta(minutes=10)
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    assert (
        await devices.authorization_status(created.authorization_id, CREDENTIAL)
    ).state == "waiting"


async def test_failed_approval_rolls_back_old_device_revocation(devices, database):
    original, token = await paired(devices)
    created = await devices.create_authorization(
        AuthorizationStart(name="Next Mac", credential=SecretStr("ab" * 32))
    )
    async with database.session() as session:
        record = await session.get(DeviceAuthorization, created.authorization_id)
        assert record is not None
        await session.execute(
            delete(DeviceAuthorization).where(
                DeviceAuthorization.device_id == original.device_id
            )
        )
        record.device_id = original.device_id
        await session.commit()
    with pytest.raises(IntegrityError):
        await devices.approve_authorization(
            1, created.authorization_id, created.browser_token, 1
        )
    assert (await devices.authenticate(token)).device_id == original.device_id
    assert (
        await devices.authorization_status(created.authorization_id, "ab" * 32)
    ).state == "waiting"


async def test_approval_and_cancel_have_one_observable_terminal_state(devices):
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    await asyncio.gather(
        devices.approve_authorization(
            1, created.authorization_id, created.browser_token, 1
        ),
        devices.cancel_authorization(created.authorization_id, CREDENTIAL),
    )
    status = await devices.authorization_status(created.authorization_id, CREDENTIAL)
    assert status.state in {"authorized", "cancelled"}
    assert len(await devices.list_devices(1)) == int(status.state == "authorized")


async def test_approved_receipt_survives_pending_expiry_and_has_explicit_retention(
    devices, clock
):
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    clock.value += timedelta(minutes=9, seconds=59)
    await devices.approve_authorization(
        1, created.authorization_id, created.browser_token, 1
    )
    clock.value += timedelta(seconds=2)
    await devices.create_authorization(
        AuthorizationStart(name="Another", credential=SecretStr("cd" * 32))
    )
    assert (
        await devices.authorization_status(created.authorization_id, CREDENTIAL)
    ).state == "authorized"
    clock.value += timedelta(days=1)
    assert (
        await devices.authorization_status(created.authorization_id, CREDENTIAL)
    ).state == "expired"


async def test_cancelled_records_keep_capacity_until_expiry(devices, database):
    created = await devices.create_authorization(
        AuthorizationStart(name="Mac", credential=SecretStr(CREDENTIAL))
    )
    await devices.cancel_authorization(created.authorization_id, CREDENTIAL)
    async with database.session() as session:
        record = await session.get(DeviceAuthorization, created.authorization_id)
        assert record is not None and record.slot is not None


async def test_owner_revoke_and_disabled_account_are_checked_on_every_request(
    devices: DeviceService, database: Database
) -> None:
    device, token = await paired(devices)
    with pytest.raises(BusinessException) as other:
        await devices.revoke(2, device.device_id)
    assert other.value.error_code == DeviceErrorCode.NOT_FOUND
    async with database.session() as session:
        user = await session.get(User, 1)
        assert user is not None
        user.disabled = True
        await session.commit()
    with pytest.raises(BusinessException) as disabled:
        await devices.authenticate(token)
    assert disabled.value.error_code == DeviceErrorCode.UNAUTHORIZED
    await devices.revoke(1, device.device_id)
    await devices.revoke(1, device.device_id)
    with pytest.raises(BusinessException):
        await devices.authenticate(token)
    assert await devices.list_devices(1) == []
    assert await devices.list_devices(2) == []


async def test_connection_replacement_preserves_new_owner_and_directory_revocation(
    devices: DeviceService, clock: Clock
) -> None:
    device, token = await paired(devices)
    first = await devices.connect(
        token, DevicePresence(capabilities=DeviceCapabilities(browser=True))
    )
    assert (await devices.list_devices(1))[0].capabilities.browser
    second = await devices.connect(token, DevicePresence())
    await devices.disconnect(first)
    assert (await devices.list_devices(1))[0].online
    with pytest.raises(BusinessException) as replaced:
        await devices.touch(token, first)
    assert replaced.value.error_code == DeviceErrorCode.CONNECTION_REPLACED
    assert not (await devices.list_devices(1))[0].capabilities.browser
    clock.value += timedelta(seconds=31)
    assert not (await devices.list_devices(1))[0].online
    await devices.touch(token, second)
    assert (await devices.list_devices(1))[0].online
    await devices.disconnect(second)
    assert not (await devices.list_devices(1))[0].online


async def test_capability_updates_bind_to_current_connection_without_extending_presence(
    devices: DeviceService,
    clock: Clock,
) -> None:
    device, token = await paired(devices)
    first = await devices.connect(token, DevicePresence())
    before = (await devices.list_devices(1))[0]
    clock.value += timedelta(seconds=5)
    capabilities = DeviceCapabilities(
        browser=True, desktop=True, desktop_capture=False, open_url=True
    )
    principal = await devices.update_capabilities(
        token,
        DeviceCapabilityUpdate(
            connection_id=first.connection_id,
            capabilities=capabilities,
        ),
    )
    assert principal.device_id == device.device_id
    current = (await devices.list_devices(1))[0]
    assert (
        current.capabilities == capabilities
        and current.last_seen_at == before.last_seen_at
    )
    await devices.touch(token, first)
    second = await devices.connect(
        token, DevicePresence(capabilities=DeviceCapabilities(browser=True))
    )
    with pytest.raises(BusinessException) as replaced:
        await devices.update_capabilities(
            token,
            DeviceCapabilityUpdate(
                connection_id=first.connection_id,
                capabilities=capabilities,
            ),
        )
    assert replaced.value.error_code == DeviceErrorCode.CONNECTION_REPLACED
    assert (await devices.list_devices(1))[0].capabilities == DeviceCapabilities(
        browser=True
    )
    await devices.disconnect(second)
    with pytest.raises(BusinessException) as offline:
        await devices.update_capabilities(
            token,
            DeviceCapabilityUpdate(
                connection_id=second.connection_id,
                capabilities=capabilities,
            ),
        )
    assert offline.value.error_code == DeviceErrorCode.CONNECTION_REPLACED


async def test_capability_updates_require_the_current_device_credential(
    devices: DeviceService,
) -> None:
    device, token = await paired(devices)
    connection = await devices.connect(token, DevicePresence())
    report = DeviceCapabilityUpdate(
        connection_id=connection.connection_id,
        capabilities=DeviceCapabilities(open_url=True),
    )
    for credential in (None, f"{device.device_id}.{'ab' * 32}"):
        with pytest.raises(BusinessException) as rejected:
            await devices.update_capabilities(credential, report)
        assert rejected.value.error_code == DeviceErrorCode.UNAUTHORIZED
    other = await devices.create_authorization(
        AuthorizationStart(name="Other Mac", credential=SecretStr("cd" * 32))
    )
    await devices.approve_authorization(
        2, other.authorization_id, other.browser_token, 2
    )
    other_status = await devices.authorization_status(other.authorization_id, "cd" * 32)
    assert other_status.device is not None
    other_token = f"{other_status.device.device_id}.{'cd' * 32}"
    await devices.connect(other_token, DevicePresence())
    with pytest.raises(BusinessException) as foreign:
        await devices.update_capabilities(other_token, report)
    assert foreign.value.error_code == DeviceErrorCode.CONNECTION_REPLACED
    assert (await devices.list_devices(1))[0].capabilities == DeviceCapabilities()
    await devices.revoke(1, device.device_id)
    with pytest.raises(BusinessException) as revoked:
        await devices.update_capabilities(token, report)
    assert revoked.value.error_code == DeviceErrorCode.UNAUTHORIZED


@pytest.fixture
async def application(
    database: Database,
    components_database: Database,
    devices: DeviceService,
    notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI]:
    app = create_application(lifespan=None)

    async with RemoteTools(engine=components_database.engine) as remote:
        app.state.resources = SimpleNamespace(
            database=database,
            components_database=components_database,
            notifications=notifications,
            remote_tools=remote,
        )
        app.dependency_overrides[get_network_user_context] = lambda: UserContext(
            user_id=1, username="user-1", roles=(), disabled=False
        )
        monkeypatch.setattr(device_router, "DeviceService", lambda _database: devices)
        yield app


async def test_device_http_routes_return_safe_envelopes(application: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://studio.test"
    ) as client:
        response = await client.post(
            "/api/devices/authorizations",
            json={"name": "My Mac", "credential": CREDENTIAL},
        )
        assert (
            response.status_code == 200
            and "no-store" in response.headers["cache-control"]
        )
        created = (
            ApiResponse[AuthorizationCreated].model_validate_json(response.content).data
        )
        assert created is not None
        response = await client.post(
            f"/api/devices/authorizations/{created.authorization_id}/approve",
            json={"browser_token": created.browser_token, "expected_user_id": 1},
        )
        assert response.status_code == 200
        response = await client.post(
            f"/api/devices/authorizations/{created.authorization_id}/status",
            json={"credential": CREDENTIAL},
        )
        result = (
            ApiResponse[AuthorizationStatus].model_validate_json(response.content).data
        )
        assert result is not None and result.device is not None
        device = result.device
        listed = await client.get("/api/devices")
        assert CREDENTIAL not in listed.text and "credential_digest" not in listed.text
        assert (
            await client.delete(f"/api/devices/{device.device_id}")
        ).status_code == 200
        workspace = application.state.resources.remote_tools.endpoint(
            namespace="ns_1", target=device.device_id
        ).workspace(authorization="grant")
        with pytest.raises(RemoteError) as denied:
            async with workspace.prepare(
                RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
            ):
                pytest.fail("撤销设备不能准备新工作区")
        assert denied.value.code == RemoteErrorCode.REJECTED


async def test_device_replacement_revokes_old_remote_endpoint(application, devices):
    old, _token = await paired(devices)
    created = await devices.create_authorization(
        AuthorizationStart(name="Next Mac", credential=SecretStr("cd" * 32))
    )
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://studio.test"
    ) as client:
        response = await client.post(
            f"/api/devices/authorizations/{created.authorization_id}/approve",
            json={"browser_token": created.browser_token, "expected_user_id": 1},
        )
        assert response.status_code == 200
    workspace = application.state.resources.remote_tools.endpoint(
        namespace="ns_1", target=old.device_id
    ).workspace(authorization="grant")
    with pytest.raises(RemoteError) as denied:
        async with workspace.prepare(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
        ):
            pytest.fail("设备替换必须封闭旧设备的执行入口")
    assert denied.value.code == RemoteErrorCode.REJECTED


@pytest.mark.parametrize("committed_before_request", [False, True])
async def test_current_device_revoke_is_recoverable_and_rejects_other_access(
    application: FastAPI, devices: DeviceService, committed_before_request: bool
) -> None:
    device, token = await paired(devices)
    await devices.connect(token, DevicePresence())
    if committed_before_request:
        principal = await devices.revoke_current(token)
        assert principal.device_id == device.device_id
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://studio.test"
    ) as client:
        for _ in range(2):
            response = await client.delete(
                "/api/devices/current", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
            assert response.json() == {"code": 0, "message": "success", "data": None}
            assert "no-store" in response.headers["cache-control"]
            assert "set-cookie" not in response.headers
            assert token not in response.text
        assert (await client.get("/api/devices")).status_code == 200
        response = await client.get(
            "/api/devices/projects", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
    with pytest.raises(BusinessException) as unauthorized:
        await devices.authenticate(token)
    assert unauthorized.value.error_code == DeviceErrorCode.UNAUTHORIZED
    assert await devices.list_devices(1) == []
    endpoint = application.state.resources.remote_tools.endpoint(
        namespace="ns_1", target=device.device_id
    )
    with pytest.raises(RemoteError) as rejected:
        async with endpoint.workspace(authorization="grant").prepare(
            RunIdentity(namespace="ns_1", thread_id="thread", run_id="run")
        ):
            pytest.fail("设备自撤销后不能接受新执行")
    assert rejected.value.code == RemoteErrorCode.REJECTED


async def test_current_device_revoke_uses_only_the_calling_device_credential(
    application: FastAPI, devices: DeviceService
) -> None:
    first, first_token = await paired(devices)
    authorization = await devices.create_authorization(
        AuthorizationStart(name="Other Mac", credential=SecretStr("cd" * 32))
    )
    await devices.approve_authorization(
        2, authorization.authorization_id, authorization.browser_token, 2
    )
    result = await devices.authorization_status(
        authorization.authorization_id, "cd" * 32
    )
    assert result.device is not None
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://studio.test"
    ) as client:
        for credential in (None, "invalid", f"{first.device_id}.{'cd' * 32}"):
            response = await client.delete(
                "/api/devices/current",
                headers={}
                if credential is None
                else {"Authorization": f"Bearer {credential}"},
            )
            assert response.status_code == 401
            assert "no-store" in response.headers["cache-control"]
            assert "credential" not in response.text
        # 网页账号是用户1；用户2的设备密钥只撤销用户2的调用设备
        response = await client.delete(
            "/api/devices/current",
            headers={"Authorization": f"Bearer {result.device.device_id}.{'cd' * 32}"},
        )
        assert response.status_code == 200
    assert (await devices.authenticate(first_token)).device_id == first.device_id
    assert await devices.list_devices(2) == []


async def test_malformed_authorization_requests_are_not_cached(application):
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://studio.test"
    ) as client:
        for body in ("{", '{"name":"Mac","credential":"x"}'):
            response = await client.post(
                "/api/devices/authorizations",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 422
            assert "no-store" in response.headers["cache-control"]


@asynccontextmanager
async def socket(
    application: FastAPI, token: str
) -> AsyncGenerator[tuple[asyncio.Queue[Message], asyncio.Queue[Message]]]:
    incoming: asyncio.Queue[Message] = asyncio.Queue(8)
    outgoing: asyncio.Queue[Message] = asyncio.Queue(8)
    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "scheme": "wss",
        "path": "/api/devices/connect",
        "raw_path": b"/api/devices/connect",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"authorization", ("Bearer " + token).encode())],
        "client": ("127.0.0.1", 1000),
        "server": ("studio.test", 443),
        "subprotocols": [],
        "state": {},
    }
    await incoming.put({"type": "websocket.connect"})
    running = asyncio.create_task(application(scope, incoming.get, outgoing.put))
    try:
        yield incoming, outgoing
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await running


async def test_device_socket_authenticates_and_rechecks_revocation(
    application: FastAPI, devices: DeviceService
) -> None:
    _device, token = await paired(devices)
    async with socket(application, token) as (incoming, outgoing):
        assert (await outgoing.get())["type"] == "websocket.accept"
        await incoming.put(
            {"type": "websocket.receive", "text": '{"capabilities": {}}'}
        )
        hello = await outgoing.get()
        assert hello["type"] == "websocket.send" and token not in hello["text"]
        principal = ConnectedDevice.model_validate_json(hello["text"])
        await devices.revoke(1, principal.device_id)
        await incoming.put({"type": "websocket.receive", "text": "{}"})
        closed = await outgoing.get()
        while closed["type"] == "websocket.send":
            closed = await outgoing.get()
        assert closed["type"] == "websocket.close" and closed["code"] == 1008
    async with socket(application, token) as (_incoming, outgoing):
        assert (await outgoing.get())["type"] == "websocket.close"


async def test_presence_http_update_keeps_the_authenticated_socket_and_private_response(
    application: FastAPI,
    devices: DeviceService,
) -> None:
    device, token = await paired(devices)
    async with socket(application, token) as (incoming, outgoing):
        assert (await outgoing.get())["type"] == "websocket.accept"
        await incoming.put(
            {"type": "websocket.receive", "text": '{"capabilities": {}}'}
        )
        hello = await outgoing.get()
        connection = ConnectedDevice.model_validate_json(hello["text"])
        capabilities = DeviceCapabilities(browser=True, open_url=True)
        payload = DeviceCapabilityUpdate(
            connection_id=connection.connection_id, capabilities=capabilities
        ).model_dump(mode="json")
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="https://studio.test"
        ) as client:
            response = await client.put(
                "/api/devices/presence",
                json=payload,
                headers={"Authorization": "Bearer " + token},
            )
            assert (
                response.status_code == 200
                and "no-store" in response.headers["cache-control"]
            )
            assert (
                ApiResponse[DeviceCapabilities]
                .model_validate_json(response.content)
                .data
                == capabilities
            )
            assert (await devices.list_devices(1))[0].capabilities == capabilities
            assert (await devices.list_devices(1))[0].device_id == device.device_id
            denied = await client.put("/api/devices/presence", json=payload)
            assert (
                denied.status_code == 401
                and "no-store" in denied.headers["cache-control"]
            )
        assert outgoing.empty()
