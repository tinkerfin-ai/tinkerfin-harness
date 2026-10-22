"""网页设备管理与本机主动建立的工具执行连接"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine

from anyio.to_thread import run_sync
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import select
from starlette.responses import Response

from tinkerfin_contracts.media import Attachment
from tinkerfin_notifications import Notifications
from tinkerfin_remote import RemoteError
from tinkerfin_studio.api.dependencies import NetworkUserDep, RawTokenDep, get_raw_token
from tinkerfin_studio.api.errors import (
    BusinessException,
    DeviceErrorCode,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.devices.executions import DeviceExecutions
from tinkerfin_studio.devices.schemas import (
    AuthorizationApproval,
    AuthorizationCreated,
    AuthorizationPreview,
    AuthorizationProof,
    AuthorizationStart,
    AuthorizationStatus,
    BrowserAuthorizationProof,
    ConnectedDevice,
    DeviceCapabilities,
    DeviceCapabilityUpdate,
    DeviceName,
    DevicePresence,
    DeviceProject,
    DeviceProjectPage,
    DeviceView,
    LocalExecutionConfiguration,
    LocalFileContent,
    LocalSkillFile,
    LocalSkillPackage,
    PairedDevice,
)
from tinkerfin_studio.devices.service import DeviceConnection, DeviceService
from tinkerfin_studio.projects.models import Project
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.resources import get_resources


class PrivateDeviceRoute(APIRoute):
    """在 JSON 解析和认证之前标记私有响应，错误也不能缓存"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def private(request: Request) -> Response:
            request.state.private_no_store = True
            response = await handler(request)
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return private


router = APIRouter(prefix="/devices", tags=["本机设备"], route_class=PrivateDeviceRoute)
logger = logging.getLogger(__name__)


@router.get(
    "/executions/{execution_id}/skills/{installation_id}",
    response_model=ApiResponse[LocalSkillPackage],
)
async def execution_skill(
    execution_id: str,
    installation_id: str,
    request: Request,
    token: RawTokenDep,
    thread_id: str = Query(min_length=1, max_length=128),
    run_id: str = Query(min_length=1, max_length=128),
) -> Response:
    """只下载当前执行已固定的技能包，保留原文件字节与摘要"""
    resources = get_resources(request.app)
    authorized = await DeviceExecutions(resources.database).authorize(
        token, execution_id, thread_id=thread_id, run_id=run_id
    )
    reference = next(
        (
            skill
            for skill in authorized.configuration.skills
            if skill.installation_id == installation_id
        ),
        None,
    )
    if reference is None:
        raise BusinessException(DeviceErrorCode.EXECUTION_UNAVAILABLE)
    package = await resources.skills.content.load(
        authorized.principal.user_id, reference.digest
    )
    if package.name != reference.name:
        raise BusinessException(DeviceErrorCode.PROFILE_CHANGED)

    def encode() -> bytes:
        payload = LocalSkillPackage(
            name=package.name,
            digest=package.digest,
            files=tuple(
                LocalSkillFile(
                    path=file.path,
                    content=base64.b64encode(file.content).decode("ascii"),
                )
                for file in package.files
            ),
        )
        return ApiResponse.success(payload).model_dump_json().encode()

    # 大包编码使用 AnyIO 的有界共享线程池，响应阶段不再同步序列化正文
    return Response(
        await run_sync(encode),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/executions/{execution_id}/files", response_model=ApiResponse[Attachment])
async def deliver_execution_file(
    execution_id: str,
    request: Request,
    token: RawTokenDep,
    name: str = Query(min_length=1, max_length=255),
    thread_id: str = Query(min_length=1, max_length=128),
    run_id: str = Query(min_length=1, max_length=128),
    invocation_id: str = Query(min_length=1, max_length=512),
) -> ApiResponse[Attachment]:
    """把当前工具调用的本机产物保存为会话附件，重传不重复发布"""
    resources = get_resources(request.app)
    executions = DeviceExecutions(resources.database)
    authorized = await executions.authorize(
        token, execution_id, thread_id=thread_id, run_id=run_id
    )
    delivery = hashlib.sha256(
        json.dumps([execution_id, invocation_id], separators=(",", ":")).encode()
    ).hexdigest()
    attachment = await resources.attachments.upload(
        user_id=authorized.principal.user_id,
        thread_id=thread_id,
        name=name,
        chunks=request.stream(),
        source="tool",
        delivery_scope="local:" + delivery,
    )
    # 上传一旦受理就按该次授权结清；撤销不能把已保存文件伪装成失败
    async with resources.database.session() as session:
        await resources.attachments.bind(
            session,
            [attachment.id],
            user_id=authorized.principal.user_id,
            thread_id=thread_id,
            message_id="local-file-" + delivery,
        )
        await session.commit()
    return ApiResponse.success(attachment)


@router.get(
    "/executions/{execution_id}/files", response_model=ApiResponse[list[Attachment]]
)
async def list_execution_files(
    execution_id: str,
    request: Request,
    token: RawTokenDep,
    thread_id: str = Query(min_length=1, max_length=128),
    run_id: str = Query(min_length=1, max_length=128),
) -> ApiResponse[list[Attachment]]:
    """只列出当前云端运行所属会话中可读取的附件"""
    resources = get_resources(request.app)
    authorized = await DeviceExecutions(resources.database).authorize(
        token, execution_id, thread_id=thread_id, run_id=run_id
    )
    return ApiResponse.success(
        await resources.attachments.list_thread(
            user_id=authorized.principal.user_id,
            thread_id=thread_id,
        )
    )


@router.get(
    "/executions/{execution_id}/files/{attachment_id}",
    response_model=ApiResponse[LocalFileContent],
)
async def read_execution_file(
    execution_id: str,
    attachment_id: str,
    request: Request,
    token: RawTokenDep,
    thread_id: str = Query(min_length=1, max_length=128),
    run_id: str = Query(min_length=1, max_length=128),
) -> Response:
    """按当前会话归属向本机返回附件原始字节"""
    resources = get_resources(request.app)
    authorized = await DeviceExecutions(resources.database).authorize(
        token, execution_id, thread_id=thread_id, run_id=run_id
    )
    attachment, data = await resources.attachments.read(
        attachment_id,
        user_id=authorized.principal.user_id,
        thread_id=thread_id,
        variant="original",
    )

    def encode() -> bytes:
        payload = LocalFileContent(
            mime_type=attachment.mime_type,
            content=base64.b64encode(data).decode("ascii"),
        )
        return ApiResponse.success(payload).model_dump_json().encode()

    return Response(
        await run_sync(encode),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/projects", response_model=ApiResponse[DeviceProjectPage])
async def device_projects(
    request: Request, token: RawTokenDep, after: str = Query(default="", max_length=64)
) -> ApiResponse[DeviceProjectPage]:
    """只向有效设备分页返回其所属账号的项目"""
    database = get_resources(request.app).database
    principal = await DeviceService(database).authenticate(token)
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(Project)
                .where(
                    Project.user_id == principal.user_id,
                    Project.id > after,
                )
                .order_by(Project.id)
                .limit(129)
            )
        )
        items = tuple(DeviceProject(project_id=p.id, name=p.name) for p in rows[:128])
        owner = await session.get(User, principal.user_id)
        assert owner is not None
        return ApiResponse.success(
            DeviceProjectPage(
                account_name=owner.username,
                items=items,
                next_cursor=items[-1].project_id if len(rows) > 128 else None,
            )
        )


@router.get("/projects/{project_id}", response_model=ApiResponse[DeviceProject])
async def device_project(
    project_id: str, request: Request, token: RawTokenDep
) -> ApiResponse[DeviceProject]:
    """打开目录前独立复核项目，清单缓存不授予项目权限"""
    database = get_resources(request.app).database
    principal = await DeviceService(database).authenticate(token)
    async with database.session() as session:
        item = await ProjectRepository(session, principal.user_id).require(project_id)
        return ApiResponse.success(DeviceProject(project_id=item.id, name=item.name))


@router.get(
    "/executions/{execution_id}/configuration",
    response_model=ApiResponse[LocalExecutionConfiguration],
)
async def execution_configuration(
    execution_id: str,
    request: Request,
    token: RawTokenDep,
    thread_id: str = Query(min_length=1, max_length=128),
    run_id: str = Query(min_length=1, max_length=128),
) -> ApiResponse[LocalExecutionConfiguration]:
    """向已认证本机提供云端运行已固定的工作区与技能"""
    resources = get_resources(request.app)
    authorized = await DeviceExecutions(resources.database).authorize(
        token, execution_id, thread_id=thread_id, run_id=run_id
    )
    return ApiResponse.success(authorized.configuration)


def _private_response(
    payload: AuthorizationCreated
    | AuthorizationPreview
    | AuthorizationStatus
    | PairedDevice,
) -> Response:
    return Response(
        ApiResponse.success(payload).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/authorizations", response_model=ApiResponse[AuthorizationCreated])
async def create_authorization(
    payload: AuthorizationStart, request: Request
) -> Response:
    """客户端发起浏览器授权，凭证不进入 URL 或浏览器"""
    return _private_response(
        await DeviceService(get_resources(request.app).database).create_authorization(
            payload
        )
    )


@router.post(
    "/authorizations/{authorization_id}/preview",
    response_model=ApiResponse[AuthorizationPreview],
)
async def preview_authorization(
    authorization_id: str,
    payload: BrowserAuthorizationProof,
    request: Request,
    user: NetworkUserDep,
) -> Response:
    """已登录浏览器核对本次授权的设备名称与状态"""
    return _private_response(
        await DeviceService(get_resources(request.app).database).preview_authorization(
            user.user_id, authorization_id, payload.browser_token.get_secret_value()
        )
    )


@router.post(
    "/authorizations/{authorization_id}/approve",
    response_model=ApiResponse[AuthorizationPreview],
)
async def approve_authorization(
    authorization_id: str,
    payload: AuthorizationApproval,
    request: Request,
    user: NetworkUserDep,
) -> Response:
    """批准完成后通知本人设备变化，首次使用与更换电脑使用同一操作"""
    resources = get_resources(request.app)
    result = await DeviceService(resources.database).approve_authorization(
        user.user_id,
        authorization_id,
        payload.browser_token.get_secret_value(),
        payload.expected_user_id,
    )
    for device_id in result.revoked_device_ids:
        await resources.remote_tools.endpoint(
            namespace=f"ns_{user.user_id}", target=device_id
        ).revoke()
    await notify_change(
        resources.notifications,
        user_id=user.user_id,
        topic="studio.devices.changed",
        key=authorization_id,
    )
    return _private_response(result.preview)


@router.post(
    "/authorizations/{authorization_id}/status",
    response_model=ApiResponse[AuthorizationStatus],
)
async def authorization_status(
    authorization_id: str, payload: AuthorizationProof, request: Request
) -> Response:
    """只有客户端凭证可以领取批准后的设备身份"""
    return _private_response(
        await DeviceService(get_resources(request.app).database).authorization_status(
            authorization_id, payload.credential.get_secret_value()
        )
    )


@router.post(
    "/authorizations/{authorization_id}/cancel",
    response_model=ApiResponse[AuthorizationStatus],
)
async def cancel_authorization(
    authorization_id: str, payload: AuthorizationProof, request: Request
) -> Response:
    """取消客户端未完成的申请，已批准时返回真实结果"""
    return _private_response(
        await DeviceService(get_resources(request.app).database).cancel_authorization(
            authorization_id, payload.credential.get_secret_value()
        )
    )


@router.post(
    "/authorizations/{authorization_id}/deny",
    response_model=ApiResponse[AuthorizationPreview],
)
async def deny_authorization(
    authorization_id: str,
    payload: BrowserAuthorizationProof,
    request: Request,
) -> Response:
    """浏览器取消当前入口的待批准申请"""
    return _private_response(
        await DeviceService(get_resources(request.app).database).deny_authorization(
            authorization_id, payload.browser_token.get_secret_value()
        )
    )


@router.patch("/current", response_model=ApiResponse[PairedDevice])
async def rename_device(
    payload: DeviceName, request: Request, token: RawTokenDep
) -> Response:
    """当前设备更新显示名称并通知所属网页"""
    resources = get_resources(request.app)
    service = DeviceService(resources.database)
    device = await service.rename(token, payload.name)
    principal = await service.authenticate(token)
    await notify_change(
        resources.notifications,
        user_id=principal.user_id,
        topic="studio.devices.changed",
        key=device.device_id,
    )
    return _private_response(device)


@router.delete("/current", response_model=ApiResponse[None])
async def revoke_current_device(
    request: Request, token: RawTokenDep
) -> ApiResponse[None]:
    """撤销调用设备并停止其后续执行接入，不修改浏览器账号会话"""
    resources = get_resources(request.app)
    principal = await DeviceService(resources.database).revoke_current(token)
    await resources.remote_tools.endpoint(
        namespace=principal.namespace, target=principal.device_id
    ).revoke()
    await notify_change(
        resources.notifications,
        user_id=principal.user_id,
        topic="studio.devices.changed",
        key=principal.device_id,
    )
    return ApiResponse.success()


@router.get("", response_model=ApiResponse[list[DeviceView]])
async def list_devices(
    request: Request, user: NetworkUserDep
) -> ApiResponse[list[DeviceView]]:
    """列出本人设备和本机已经授予的工作区"""
    return ApiResponse.success(
        await DeviceService(get_resources(request.app).database).list_devices(
            user.user_id
        )
    )


@router.delete("/{device_id}", response_model=ApiResponse[None])
async def revoke_device(
    device_id: str, request: Request, user: NetworkUserDep
) -> ApiResponse[None]:
    """撤销本人设备凭证，阻止后续接入和工具执行"""
    resources = get_resources(request.app)
    await DeviceService(resources.database).revoke(user.user_id, device_id)
    await resources.remote_tools.endpoint(
        namespace=f"ns_{user.user_id}", target=device_id
    ).revoke()
    await notify_change(
        resources.notifications,
        user_id=user.user_id,
        topic="studio.devices.changed",
        key=device_id,
    )
    return ApiResponse.success()


async def _disconnect(
    service: DeviceService, connection: DeviceConnection, notifications: Notifications
) -> None:
    # 连接退出时结算在线标记；重复取消不能让此写入成为无人等待的后台任务
    async def settle() -> None:
        await service.disconnect(connection)
        await notify_change(
            notifications,
            user_id=connection.principal.user_id,
            topic="studio.devices.changed",
            key=connection.principal.device_id,
        )

    closing = asyncio.create_task(settle())
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
    try:
        closing.result()
    except BaseException as error:
        if cancelled and not isinstance(error, asyncio.CancelledError):
            raise asyncio.CancelledError from error
        raise
    if cancelled:
        raise asyncio.CancelledError


@router.put("/presence", response_model=ApiResponse[DeviceCapabilities])
async def update_device_capabilities(
    payload: DeviceCapabilityUpdate, request: Request, token: RawTokenDep
) -> ApiResponse[DeviceCapabilities]:
    """已认证客户端更新当前连接的真实能力，不打断工具传输"""
    resources = get_resources(request.app)
    principal = await DeviceService(resources.database).update_capabilities(
        token, payload
    )
    await notify_change(
        resources.notifications,
        user_id=principal.user_id,
        topic="studio.devices.changed",
        key=principal.device_id,
    )
    return ApiResponse.success(payload.capabilities)


@router.websocket("/connect")
async def connect_device(websocket: WebSocket) -> None:
    """认证本机连接后，将工具调用传输交给框架管理

    凭证仅从 Authorization 请求头读取。第一条消息是 DevicePresence，
    返回 ConnectedDevice 后使用远端工具协议；应用不解释确认、分块或序号。
    每次交换都复核撤销与连接身份，空闲连接不占用数据库连接。
    """
    resources = get_resources(websocket.app)
    service = DeviceService(resources.database)
    token = await get_raw_token(websocket.headers.get("authorization"))
    try:
        principal = await service.authenticate(token)
    except BusinessException:
        await websocket.close(code=1008, reason="设备凭证无效或已撤销")
        return
    assert token is not None
    await websocket.accept()
    connection: DeviceConnection | None = None
    try:
        async with asyncio.timeout(10):
            initial = await websocket.receive_text()
        if len(initial.encode()) > 64 * 1024:
            await websocket.close(code=1009, reason="工作区信息过大")
            return
        connection = await service.connect(
            token, DevicePresence.model_validate_json(initial)
        )
        await notify_change(
            resources.notifications,
            user_id=principal.user_id,
            topic="studio.devices.changed",
            key=principal.device_id,
        )
        endpoint = resources.remote_tools.endpoint(
            namespace=principal.namespace, target=principal.device_id
        )
        await websocket.send_text(
            ConnectedDevice(
                device_id=principal.device_id,
                namespace=principal.namespace,
                name=principal.name,
                connection_id=connection.connection_id,
            ).model_dump_json()
        )

        async def receive() -> str:
            async with asyncio.timeout(35):
                payload = await websocket.receive_text()
            assert connection is not None
            await service.touch(token, connection)
            return payload

        await endpoint.serve(send=websocket.send_text, receive=receive)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        await websocket.close(code=1001, reason="设备连接超时")
    except (BusinessException, ValidationError):
        await websocket.close(code=1008, reason="设备授权或连接信息已失效")
    except RemoteError as error:
        logger.warning("设备工具传输失败，分类=%s", error.code)
        await websocket.close(code=1011, reason="工具传输未完成，请重新连接")
    finally:
        if connection is not None:
            await _disconnect(service, connection, resources.notifications)
